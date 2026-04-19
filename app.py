"""
Unity X Finder - Web版
======================
Flask Webアプリケーション。unity_x_finder.py の収集ロジックをWeb UIから利用できる。

使い方:
    pip install flask requests
    python app.py

ブラウザで http://localhost:5000 にアクセス。
"""

import csv
import io
import os
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request, Response

from unity_x_finder import (
    CancelledError,
    RequestLimitError,
    RateLimitError,
    Fetcher,
    extract_handles_from_text,
)

app = Flask(__name__)

DEFAULT_QIITA_TOKEN = os.environ.get("QIITA_TOKEN", "8a3637134c130a379c9fd4e2aa4adf9389719451")

# ---------- ジョブ管理 ----------
# { job_id: { "status": str, "results": list, "logs": list,
#              "request_count": int, "fetcher": Fetcher|None } }
jobs: dict = {}
jobs_lock = threading.Lock()


def _create_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {
            "status": "running",
            "results": [],
            "logs": [],
            "request_count": 0,
            "fetcher": None,
        }
    return job_id


def _log(job_id: str, msg: str):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["logs"].append(msg)


def _add_result(job_id: str, row: dict):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["results"].append(row)


def _update_request_count(job_id: str, count: int):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["request_count"] = count


def _set_status(job_id: str, status: str):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["status"] = status


# ---------- 収集ロジック (tkinter版の _run を移植) ----------
def _run_collection(job_id: str, tag: str, qiita_count: int, zenn_count: int,
                    qiita_token: str, scan_body: bool, max_requests: int):
    try:
        f = Fetcher(qiita_token, max_requests=max_requests)
        with jobs_lock:
            jobs[job_id]["fetcher"] = f

        def log(msg):
            _log(job_id, msg)

        # タイムアウト等で自動リトライが走ったときもユーザー画面に通知する
        f.retry_logger = log

        seen = set()  # (source, handle) の重複除去

        # ----- Qiita -----
        try:
            q_items = f.qiita_tag_items(tag, qiita_count, log)
            _update_request_count(job_id, f.request_count)
            log(f"[Qiita] 記事 {len(q_items)} 件取得")
            # ユーザー単位でまとめてから個別取得
            user_to_items: dict = {}
            for it in q_items:
                uid = it.get("user", {}).get("id")
                if uid:
                    user_to_items.setdefault(uid, []).append(it)
            for uid, its in user_to_items.items():
                # 記事一覧のuserオブジェクトにtwitter_screen_nameがあればそれを使う
                handle = its[0].get("user", {}).get("twitter_screen_name") or ""
                if not handle:
                    # 念のためユーザー詳細も見る
                    u = f.qiita_user(uid, log)
                    _update_request_count(job_id, f.request_count)
                    if u:
                        handle = u.get("twitter_screen_name") or ""
                sample = its[0]
                if handle:
                    key = ("qiita", handle.lower())
                    if key not in seen:
                        seen.add(key)
                        _add_result(job_id, {
                            "source": "Qiita",
                            "handle": handle,
                            "author": sample.get("user", {}).get("id", ""),
                            "article_title": sample.get("title", ""),
                            "article_url": sample.get("url", ""),
                            "detected_by": "プロフィール",
                        })
                # 本文中スキャン
                if scan_body:
                    for it in its:
                        body = it.get("body", "") or it.get("rendered_body", "")
                        for h in extract_handles_from_text(body):
                            key = ("qiita", h)
                            if key not in seen:
                                seen.add(key)
                                _add_result(job_id, {
                                    "source": "Qiita",
                                    "handle": h,
                                    "author": it.get("user", {}).get("id", ""),
                                    "article_title": it.get("title", ""),
                                    "article_url": it.get("url", ""),
                                    "detected_by": "本文",
                                })
                time.sleep(0.3)
        except (CancelledError, RequestLimitError, RateLimitError) as e:
            log(f"[Qiita] {e}")
        except Exception as e:
            log(f"[Qiita] エラー: {e}")

        # ----- Zenn（キャンセル・上限到達時はスキップ） -----
        if not f._cancelled and f.request_count < f.max_requests:
            try:
                z_items = f.zenn_topic_articles(tag.lower(), zenn_count, log)
                _update_request_count(job_id, f.request_count)
                log(f"[Zenn] 記事 {len(z_items)} 件取得")
                user_to_items = {}
                for it in z_items:
                    uname = it.get("user", {}).get("username")
                    if uname:
                        user_to_items.setdefault(uname, []).append(it)
                for uname, its in user_to_items.items():
                    user = f.zenn_user(uname, log)
                    _update_request_count(job_id, f.request_count)
                    handle = (user or {}).get("twitter_username") or ""
                    sample = its[0]
                    if handle:
                        key = ("zenn", handle.lower())
                        if key not in seen:
                            seen.add(key)
                            _add_result(job_id, {
                                "source": "Zenn",
                                "handle": handle,
                                "author": uname,
                                "article_title": sample.get("title", ""),
                                "article_url": f"https://zenn.dev{sample.get('path', '')}",
                                "detected_by": "プロフィール",
                            })
                    if scan_body:
                        for it in its:
                            body = f.zenn_article_body(uname, it.get("slug", ""), log)
                            _update_request_count(job_id, f.request_count)
                            for h in extract_handles_from_text(body):
                                key = ("zenn", h)
                                if key not in seen:
                                    seen.add(key)
                                    _add_result(job_id, {
                                        "source": "Zenn",
                                        "handle": h,
                                        "author": uname,
                                        "article_title": it.get("title", ""),
                                        "article_url": f"https://zenn.dev{it.get('path', '')}",
                                        "detected_by": "本文",
                                    })
                            time.sleep(0.5)
                    time.sleep(0.3)
            except (CancelledError, RequestLimitError, RateLimitError) as e:
                log(f"[Zenn] {e}")
            except Exception as e:
                log(f"[Zenn] エラー: {e}")

        result_count = len(jobs[job_id]["results"])
        log(f"=== 完了: Xアカウント {result_count} 件 / APIリクエスト {f.request_count} 回 ===")
        _update_request_count(job_id, f.request_count)
        _set_status(job_id, "completed")
    except Exception as e:
        _log(job_id, f"予期しないエラー: {e}")
        _set_status(job_id, "error")
    finally:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["fetcher"] = None


# ---------- ルート ----------
@app.route("/")
def index():
    return render_template("index.html", default_qiita_token=DEFAULT_QIITA_TOKEN)


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True)
    tag = str(data.get("tag", "Unity")).strip() or "Unity"
    qiita_count = int(data.get("qiita_count", 100))
    zenn_count = int(data.get("zenn_count", 100))
    qiita_token = str(data.get("qiita_token", ""))
    scan_body = bool(data.get("scan_body", False))
    max_requests = int(data.get("max_requests", 300))

    job_id = _create_job()
    t = threading.Thread(
        target=_run_collection,
        args=(job_id, tag, qiita_count, zenn_count, qiita_token, scan_body, max_requests),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id}), 202


@app.route("/api/status/<job_id>", methods=["GET"])
def api_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "results": job["results"],
        "logs": job["logs"],
        "request_count": job["request_count"],
    })


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    fetcher = job.get("fetcher")
    if fetcher is not None:
        fetcher.cancel()
        _log(job_id, "中断を要求しました...")
        return jsonify({"message": "Cancel requested"})
    return jsonify({"message": "Job is not running"}), 400


@app.route("/api/csv/<job_id>", methods=["GET"])
def api_csv(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404

    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["source", "x_handle", "x_url", "author", "article_title", "article_url", "detected_by"])
    for r in job["results"]:
        w.writerow([
            r["source"],
            r["handle"],
            f"https://x.com/{r['handle']}",
            r["author"],
            r["article_title"],
            r["article_url"],
            r["detected_by"],
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=unity_x_accounts.csv"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
