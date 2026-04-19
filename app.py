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
import sqlite3
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

# 環境変数 QIITA_TOKEN が設定されていればサーバー側で使用し、UIの入力欄を非表示にする
_SERVER_QIITA_TOKEN = os.environ.get("QIITA_TOKEN", "").strip()

# SQLite のパス。Railway は /tmp が書き込み可能。
DB_PATH = os.environ.get("DB_PATH", "/tmp/jobs.db")

# キャンセル用 Fetcher オブジェクトのみメモリ管理（DB に入れられないため）
_fetchers: dict = {}
_fetchers_lock = threading.Lock()

# DB への同時書き込みを直列化するロック
_db_write_lock = threading.Lock()


# ---------- DB 初期化 ----------
def _init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id      TEXT PRIMARY KEY,
                status      TEXT NOT NULL DEFAULT 'running',
                request_count INTEGER NOT NULL DEFAULT 0,
                created_at  REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      TEXT NOT NULL,
                source      TEXT,
                handle      TEXT,
                author      TEXT,
                article_title TEXT,
                article_url TEXT,
                detected_by TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_logs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id  TEXT NOT NULL,
                message TEXT
            )
        """)
        conn.commit()


_init_db()


# ---------- DB ヘルパー ----------
def _db_create_job(job_id: str):
    with _db_write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, status, request_count, created_at) VALUES (?, 'running', 0, ?)",
                (job_id, time.time()),
            )
            conn.commit()


def _db_log(job_id: str, msg: str):
    with _db_write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT INTO job_logs (job_id, message) VALUES (?, ?)", (job_id, msg))
            conn.commit()


def _db_add_result(job_id: str, row: dict):
    with _db_write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO job_results (job_id, source, handle, author, article_title, article_url, detected_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    row.get("source", ""),
                    row.get("handle", ""),
                    row.get("author", ""),
                    row.get("article_title", ""),
                    row.get("article_url", ""),
                    row.get("detected_by", ""),
                ),
            )
            conn.commit()


def _db_update_request_count(job_id: str, count: int):
    with _db_write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE jobs SET request_count = ? WHERE job_id = ?", (count, job_id))
            conn.commit()


def _db_set_status(job_id: str, status: str):
    with _db_write_lock:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (status, job_id))
            conn.commit()


def _db_get_job(job_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        results = [
            dict(r) for r in conn.execute(
                "SELECT source, handle, author, article_title, article_url, detected_by"
                " FROM job_results WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall()
        ]
        logs = [
            r["message"] for r in conn.execute(
                "SELECT message FROM job_logs WHERE job_id = ? ORDER BY id",
                (job_id,),
            ).fetchall()
        ]
        return {
            "status": row["status"],
            "request_count": row["request_count"],
            "results": results,
            "logs": logs,
        }


# ---------- 収集ロジック ----------
def _run_collection(job_id: str, tag: str, qiita_count: int, zenn_count: int,
                    qiita_token: str, scan_body: bool, max_requests: int):
    try:
        f = Fetcher(qiita_token, max_requests=max_requests)
        with _fetchers_lock:
            _fetchers[job_id] = f

        def log(msg):
            _db_log(job_id, msg)

        f.retry_logger = log

        seen = set()

        # ----- Qiita -----
        try:
            q_items = f.qiita_tag_items(tag, qiita_count, log)
            _db_update_request_count(job_id, f.request_count)
            log(f"[Qiita] 記事 {len(q_items)} 件取得")
            user_to_items: dict = {}
            for it in q_items:
                uid = it.get("user", {}).get("id")
                if uid:
                    user_to_items.setdefault(uid, []).append(it)
            for uid, its in user_to_items.items():
                handle = its[0].get("user", {}).get("twitter_screen_name") or ""
                if not handle:
                    u = f.qiita_user(uid, log)
                    _db_update_request_count(job_id, f.request_count)
                    if u:
                        handle = u.get("twitter_screen_name") or ""
                sample = its[0]
                if handle:
                    key = ("qiita", handle.lower())
                    if key not in seen:
                        seen.add(key)
                        _db_add_result(job_id, {
                            "source": "Qiita",
                            "handle": handle,
                            "author": sample.get("user", {}).get("id", ""),
                            "article_title": sample.get("title", ""),
                            "article_url": sample.get("url", ""),
                            "detected_by": "プロフィール",
                        })
                if scan_body:
                    for it in its:
                        body = it.get("body", "") or it.get("rendered_body", "")
                        for h in extract_handles_from_text(body):
                            key = ("qiita", h)
                            if key not in seen:
                                seen.add(key)
                                _db_add_result(job_id, {
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

        # ----- Zenn -----
        if not f._cancelled and f.request_count < f.max_requests:
            try:
                z_items = f.zenn_topic_articles(tag.lower(), zenn_count, log)
                _db_update_request_count(job_id, f.request_count)
                log(f"[Zenn] 記事 {len(z_items)} 件取得")
                user_to_items = {}
                for it in z_items:
                    uname = it.get("user", {}).get("username")
                    if uname:
                        user_to_items.setdefault(uname, []).append(it)
                for uname, its in user_to_items.items():
                    user = f.zenn_user(uname, log)
                    _db_update_request_count(job_id, f.request_count)
                    handle = (user or {}).get("twitter_username") or ""
                    sample = its[0]
                    if handle:
                        key = ("zenn", handle.lower())
                        if key not in seen:
                            seen.add(key)
                            _db_add_result(job_id, {
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
                            _db_update_request_count(job_id, f.request_count)
                            for h in extract_handles_from_text(body):
                                key = ("zenn", h)
                                if key not in seen:
                                    seen.add(key)
                                    _db_add_result(job_id, {
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

        job = _db_get_job(job_id)
        result_count = len(job["results"]) if job else 0
        log(f"=== 完了: Xアカウント {result_count} 件 / APIリクエスト {f.request_count} 回 ===")
        _db_update_request_count(job_id, f.request_count)
        _db_set_status(job_id, "completed")
    except Exception as e:
        _db_log(job_id, f"予期しないエラー: {e}")
        _db_set_status(job_id, "error")
    finally:
        with _fetchers_lock:
            _fetchers.pop(job_id, None)


# ---------- ルート ----------
@app.route("/")
def index():
    return render_template("index.html", has_server_token=bool(_SERVER_QIITA_TOKEN))


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True)
    tag = str(data.get("tag", "Unity")).strip() or "Unity"
    qiita_count = int(data.get("qiita_count", 100))
    zenn_count = int(data.get("zenn_count", 100))
    qiita_token = _SERVER_QIITA_TOKEN or str(data.get("qiita_token", ""))
    scan_body = bool(data.get("scan_body", False))
    max_requests = int(data.get("max_requests", 300))

    job_id = uuid.uuid4().hex[:12]
    _db_create_job(job_id)

    t = threading.Thread(
        target=_run_collection,
        args=(job_id, tag, qiita_count, zenn_count, qiita_token, scan_body, max_requests),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id}), 202


@app.route("/api/status/<job_id>", methods=["GET"])
def api_status(job_id):
    job = _db_get_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    job = _db_get_job(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    with _fetchers_lock:
        fetcher = _fetchers.get(job_id)
    if fetcher is not None:
        fetcher.cancel()
        _db_log(job_id, "中断を要求しました...")
        return jsonify({"message": "Cancel requested"})
    return jsonify({"message": "Job is not running"}), 400


@app.route("/api/csv/<job_id>", methods=["GET"])
def api_csv(job_id):
    job = _db_get_job(job_id)
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
