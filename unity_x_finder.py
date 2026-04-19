"""
Unity X Finder
==============
QiitaとZennで「Unity」記事を書いている人のXアカウントをリストアップするGUIツール。

使い方:
    pip install requests
    python unity_x_finder.py

機能:
  - QiitaタグAPI / Zenn非公式API から記事を収集
  - 著者プロフィールに登録されたTwitter/Xアカウントを抽出
  - (オプション)記事本文中の @username / x.com リンクを抽出
  - CSV出力

注意:
  - Qiitaは認証なしだと 60req/h。アクセストークンを入力欄に入れると 1000req/h。
  - Zenn APIは非公式。急激なアクセスは避けてください。
"""

import csv
import json
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------- 定数 ----------
QIITA_TAG_ITEMS_URL = "https://qiita.com/api/v2/tags/{tag}/items"
QIITA_USER_URL = "https://qiita.com/api/v2/users/{user_id}"
ZENN_ARTICLES_URL = "https://zenn.dev/api/articles"
ZENN_USER_URL = "https://zenn.dev/api/users/{username}"
ZENN_ARTICLE_DETAIL_URL = "https://zenn.dev/api/articles/{slug}"  # ?username=xxx
DEFAULT_UA = "UnityXFinder/1.0 (+personal research tool)"
DEFAULT_MAX_REQUESTS = 300  # 1回の実行でのAPI呼び出し上限

# タイムアウト設定 (connect_timeout, read_timeout) 秒
# read_timeout は Qiita/Zenn のレスポンスが遅い場合を考慮し長めに設定
DEFAULT_TIMEOUT = (15, 60)
# ネットワーク一時障害に対するリトライ回数 (タイムアウト時もこの回数まで再試行)
DEFAULT_RETRIES = 3
# リトライ間隔の基準 (秒)。 実際の待機は backoff_factor * (2 ** (retry_count - 1))
DEFAULT_BACKOFF = 1.5

# 本文中の @username / x.com URL 抽出用正規表現
# 連続した @ や通常のメール風は除外し、Twitterの表記に合うものだけ拾う
MENTION_RE = re.compile(r"(?:(?<=^)|(?<=[\s\(\[>「『（【]))@([A-Za-z0-9_]{1,15})\b")
XURL_RE = re.compile(
    r"https?://(?:www\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]{1,15})(?:/|\b)"
)

# X以外の誤検出を避けるための除外ワード
IGNORE_HANDLES = {
    "gmail",
    "yahoo",
    "hotmail",
    "outlook",
    "icloud",
    "example",
    "mail",
    "sample",
    "test",
}


# ---------- カスタム例外 ----------
class CancelledError(Exception):
    pass


class RequestLimitError(Exception):
    pass


class RateLimitError(Exception):
    pass


# ---------- データ取得 ----------
class Fetcher:
    def __init__(self, qiita_token: str = "", ua: str = DEFAULT_UA,
                 max_requests: int = DEFAULT_MAX_REQUESTS,
                 timeout=DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES,
                 backoff_factor: float = DEFAULT_BACKOFF):
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": ua})
        self.qiita_token = qiita_token.strip()
        if self.qiita_token:
            self.sess.headers.update({"Authorization": f"Bearer {self.qiita_token}"})
        self.request_count = 0
        self.max_requests = max_requests
        self._cancelled = False
        self.timeout = timeout
        self.retries = retries
        self.backoff_factor = backoff_factor
        # リトライ発生を通知したいときに外部から差し込むロガー (省略可)
        self.retry_logger = None

        # urllib3 の Retry で接続系エラー (ConnectionError 等) を自動リトライ。
        # ※ ReadTimeout は urllib3 の Retry では捕捉しにくいため _get 内で個別処理する。
        retry_cfg = Retry(
            total=retries,
            connect=retries,
            read=0,  # read系は自前でハンドリング
            status=retries,
            backoff_factor=backoff_factor,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_cfg, pool_connections=10, pool_maxsize=10)
        self.sess.mount("https://", adapter)
        self.sess.mount("http://", adapter)

    def cancel(self):
        self._cancelled = True

    def _check(self):
        """リクエスト前にキャンセル・上限チェック"""
        if self._cancelled:
            raise CancelledError("ユーザーによりキャンセルされました")
        if self.request_count >= self.max_requests:
            raise RequestLimitError(
                f"リクエスト上限 ({self.max_requests}) に達しました。"
                "設定のリクエスト上限を増やすか、取得件数を減らしてください。"
            )

    def _get(self, url, **kwargs):
        """共通GETメソッド: カウント・上限チェック・レート制限・タイムアウトリトライ対応"""
        self._check()
        self.request_count += 1

        # タイムアウトは呼び出し側で上書き可能
        kwargs.setdefault("timeout", self.timeout)

        last_exc = None
        for attempt in range(self.retries + 1):
            if self._cancelled:
                raise CancelledError("ユーザーによりキャンセルされました")
            try:
                r = self.sess.get(url, **kwargs)
                if r.status_code == 429:
                    raise RateLimitError(
                        f"レート制限 (429)。しばらく時間を置いてから再実行してください。"
                    )
                return r
            except (requests.exceptions.ReadTimeout,
                    requests.exceptions.ConnectTimeout,
                    requests.exceptions.ConnectionError) as e:
                # ネットワーク一時障害の可能性。指数バックオフで再試行する。
                last_exc = e
                if attempt >= self.retries:
                    break
                wait = self.backoff_factor * (2 ** attempt)
                if callable(self.retry_logger):
                    try:
                        self.retry_logger(
                            f"通信エラー ({type(e).__name__})。"
                            f"{wait:.1f} 秒後にリトライします "
                            f"({attempt + 1}/{self.retries})... URL={url}"
                        )
                    except Exception:
                        pass
                # キャンセル反応性を上げるため細かく sleep
                slept = 0.0
                while slept < wait:
                    if self._cancelled:
                        raise CancelledError("ユーザーによりキャンセルされました")
                    step = min(0.5, wait - slept)
                    time.sleep(step)
                    slept += step

        # リトライし切っても失敗
        raise requests.exceptions.RequestException(
            f"接続/読み込みに {self.retries + 1} 回失敗しました: {last_exc}"
        )

    # ----- Qiita -----
    def qiita_tag_items(self, tag: str, max_items: int, log):
        """指定タグの記事一覧を最新順で取得"""
        per_page = 100  # Qiita API最大
        items = []
        page = 1
        while len(items) < max_items:
            self._check()
            url = QIITA_TAG_ITEMS_URL.format(tag=quote(tag))
            params = {"page": page, "per_page": min(per_page, max_items - len(items))}
            log(f"[Qiita] GET page={page} (リクエスト#{self.request_count + 1})")
            r = self._get(url, params=params)
            if r.status_code == 403:
                log(f"[Qiita] レート上限に到達しました ({r.text[:120]})")
                break
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            items.extend(batch)
            if len(batch) < params["per_page"]:
                break
            page += 1
            time.sleep(1.0)
        return items[:max_items]

    def qiita_user(self, user_id: str, log):
        url = QIITA_USER_URL.format(user_id=quote(user_id))
        r = self._get(url)
        if r.status_code == 404:
            return None
        if r.status_code == 403:
            log(f"[Qiita] user取得レート制限: {user_id}")
            return None
        r.raise_for_status()
        return r.json()

    # ----- Zenn -----
    def zenn_topic_articles(self, topic: str, max_items: int, log):
        """指定トピックの記事一覧"""
        items = []
        page = 1
        while len(items) < max_items:
            self._check()
            params = {"topicname": topic, "order": "latest", "page": page}
            log(f"[Zenn] GET page={page} (リクエスト#{self.request_count + 1})")
            r = self._get(ZENN_ARTICLES_URL, params=params)
            if r.status_code == 403:
                log(f"[Zenn] レート制限に到達しました ({r.text[:120]})")
                break
            r.raise_for_status()
            data = r.json()
            batch = data.get("articles", [])
            if not batch:
                break
            items.extend(batch)
            if not data.get("next_page"):
                break
            page = data["next_page"]
            time.sleep(1.0)
        return items[:max_items]

    def zenn_user(self, username: str, log):
        url = ZENN_USER_URL.format(username=quote(username))
        r = self._get(url)
        if r.status_code == 404:
            return None
        if r.status_code == 403:
            log(f"[Zenn] user取得レート制限: {username}")
            return None
        r.raise_for_status()
        return r.json().get("user")

    def zenn_article_body(self, username: str, slug: str, log):
        """本文Markdownを取得"""
        url = ZENN_ARTICLE_DETAIL_URL.format(slug=quote(slug))
        r = self._get(url, params={"username": username})
        if not r.ok:
            return ""
        data = r.json()
        return data.get("article", {}).get("body_html", "") or data.get(
            "article", {}
        ).get("body", "")


# ---------- 抽出ロジック ----------
def extract_handles_from_text(text: str) -> set:
    if not text:
        return set()
    handles = set()
    for m in MENTION_RE.finditer(text):
        h = m.group(1).lower()
        if h not in IGNORE_HANDLES:
            handles.add(h)
    for m in XURL_RE.finditer(text):
        handles.add(m.group(1).lower())
    return handles


# ---------- GUI ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Unity X Finder")
        self.geometry("980x640")

        self.results = []  # 蓄積結果
        self._fetcher = None  # キャンセル用
        self._build_ui()

    def _build_ui(self):
        # --- 設定フレーム ---
        top = ttk.LabelFrame(self, text="設定")
        top.pack(fill="x", padx=8, pady=6)

        ttk.Label(top, text="タグ:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.tag_var = tk.StringVar(value="Unity")
        ttk.Entry(top, textvariable=self.tag_var, width=14).grid(row=0, column=1, padx=4)

        ttk.Label(top, text="Qiita取得件数:").grid(row=0, column=2, padx=4)
        self.qiita_n_var = tk.IntVar(value=100)
        ttk.Spinbox(top, from_=10, to=1000, increment=10, textvariable=self.qiita_n_var, width=6).grid(row=0, column=3)

        ttk.Label(top, text="Zenn取得件数:").grid(row=0, column=4, padx=4)
        self.zenn_n_var = tk.IntVar(value=100)
        ttk.Spinbox(top, from_=10, to=1000, increment=10, textvariable=self.zenn_n_var, width=6).grid(row=0, column=5)

        ttk.Label(top, text="Qiitaトークン(任意):").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.token_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.token_var, width=50, show="•").grid(row=1, column=1, columnspan=3, sticky="we", padx=4)

        self.scan_body_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="記事本文中の@/x.comも抽出(遅い)", variable=self.scan_body_var).grid(row=1, column=4, columnspan=2, sticky="w", padx=4)

        ttk.Label(top, text="リクエスト上限:").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        self.max_req_var = tk.IntVar(value=DEFAULT_MAX_REQUESTS)
        ttk.Spinbox(top, from_=50, to=2000, increment=50, textvariable=self.max_req_var, width=6).grid(row=2, column=1, padx=4)
        ttk.Label(top, text="(1回の実行での最大API呼び出し回数)").grid(row=2, column=2, columnspan=4, sticky="w", padx=4)

        # --- 実行ボタン ---
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=8)
        self.run_btn = ttk.Button(btns, text="収集開始", command=self.start_run)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="中断", command=self.cancel_run, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="CSV保存", command=self.save_csv).pack(side="left", padx=4)
        ttk.Button(btns, text="クリア", command=self.clear).pack(side="left")
        self.req_label = ttk.Label(btns, text="リクエスト: 0")
        self.req_label.pack(side="right", padx=8)
        self.progress = ttk.Progressbar(btns, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        # --- 結果テーブル ---
        cols = ("source", "handle", "author", "article_title", "article_url", "detected_by")
        headers = ("ソース", "Xハンドル", "著者", "記事タイトル", "記事URL", "検出元")
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=18)
        for c, h in zip(cols, headers):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=150 if c != "article_title" else 260, anchor="w")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # --- ログ ---
        log_frame = ttk.LabelFrame(self, text="ログ")
        log_frame.pack(fill="x", padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=7, wrap="none")
        self.log_text.pack(fill="both", expand=True)

    # -------- 操作 --------
    def log(self, msg: str):
        self.after(0, self._log_main, msg)

    def _log_main(self, msg: str):
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _update_req_count(self, count: int):
        self.after(0, lambda: self.req_label.config(text=f"リクエスト: {count}"))

    def clear(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.log_text.delete("1.0", "end")
        self.results.clear()
        self.req_label.config(text="リクエスト: 0")

    def cancel_run(self):
        if self._fetcher:
            self._fetcher.cancel()
            self.log("中断を要求しました...")

    def start_run(self):
        # 入力値チェック
        try:
            qn = self.qiita_n_var.get()
            zn = self.zenn_n_var.get()
            max_req = self.max_req_var.get()
        except tk.TclError:
            messagebox.showerror("エラー", "取得件数またはリクエスト上限に無効な値が入力されています。")
            return

        # 本文スキャン時の確認
        if self.scan_body_var.get():
            est = qn + zn + (qn // 2) + (zn // 2) + zn  # 粗い見積もり
            ok = messagebox.askyesno(
                "確認",
                f"本文スキャンが有効です。\n"
                f"推定リクエスト数: 最大約{est}件\n"
                f"(リクエスト上限: {max_req}件)\n\n"
                f"Zennの記事本文取得は1記事ごとにAPIを呼びます。\n"
                f"大量のリクエストを避けるため、取得件数を減らすことを推奨します。\n\n"
                f"続行しますか？"
            )
            if not ok:
                return

        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress.start(10)
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self):
        try:
            tag = self.tag_var.get().strip() or "Unity"
            qn = self.qiita_n_var.get()
            zn = self.zenn_n_var.get()
            max_req = self.max_req_var.get()
            scan_body = self.scan_body_var.get()
            f = Fetcher(self.token_var.get(), max_requests=max_req)
            f.retry_logger = self.log
            self._fetcher = f

            seen = set()  # (source, handle) の重複除去

            # Qiita
            try:
                q_items = f.qiita_tag_items(tag, qn, self.log)
                self._update_req_count(f.request_count)
                self.log(f"[Qiita] 記事 {len(q_items)} 件取得")
                # ユーザー単位でまとめてから個別取得
                user_to_items = {}
                for it in q_items:
                    uid = it.get("user", {}).get("id")
                    if uid:
                        user_to_items.setdefault(uid, []).append(it)
                for uid, its in user_to_items.items():
                    # 記事一覧のuserオブジェクトにtwitter_screen_nameがあればそれを使う
                    handle = its[0].get("user", {}).get("twitter_screen_name") or ""
                    if not handle:
                        # 念のためユーザー詳細も見る
                        u = f.qiita_user(uid, self.log)
                        self._update_req_count(f.request_count)
                        if u:
                            handle = u.get("twitter_screen_name") or ""
                    sample = its[0]
                    if handle:
                        key = ("qiita", handle.lower())
                        if key not in seen:
                            seen.add(key)
                            self._add_row(
                                "Qiita",
                                handle,
                                sample.get("user", {}).get("id", ""),
                                sample.get("title", ""),
                                sample.get("url", ""),
                                "プロフィール",
                            )
                    # 本文中スキャン
                    if scan_body:
                        for it in its:
                            body = it.get("body", "") or it.get("rendered_body", "")
                            for h in extract_handles_from_text(body):
                                key = ("qiita", h)
                                if key not in seen:
                                    seen.add(key)
                                    self._add_row(
                                        "Qiita",
                                        h,
                                        it.get("user", {}).get("id", ""),
                                        it.get("title", ""),
                                        it.get("url", ""),
                                        "本文",
                                    )
                    time.sleep(0.3)
            except (CancelledError, RequestLimitError, RateLimitError) as e:
                self.log(f"[Qiita] {e}")
            except Exception as e:
                self.log(f"[Qiita] エラー: {e}")

            # Zenn（キャンセル・上限到達時はスキップ）
            if not f._cancelled and f.request_count < f.max_requests:
                try:
                    z_items = f.zenn_topic_articles(tag.lower(), zn, self.log)
                    self._update_req_count(f.request_count)
                    self.log(f"[Zenn] 記事 {len(z_items)} 件取得")
                    user_to_items = {}
                    for it in z_items:
                        uname = it.get("user", {}).get("username")
                        if uname:
                            user_to_items.setdefault(uname, []).append(it)
                    for uname, its in user_to_items.items():
                        user = f.zenn_user(uname, self.log)
                        self._update_req_count(f.request_count)
                        handle = (user or {}).get("twitter_username") or ""
                        sample = its[0]
                        if handle:
                            key = ("zenn", handle.lower())
                            if key not in seen:
                                seen.add(key)
                                self._add_row(
                                    "Zenn",
                                    handle,
                                    uname,
                                    sample.get("title", ""),
                                    f"https://zenn.dev{sample.get('path', '')}",
                                    "プロフィール",
                                )
                        if scan_body:
                            for it in its:
                                body = f.zenn_article_body(uname, it.get("slug", ""), self.log)
                                self._update_req_count(f.request_count)
                                for h in extract_handles_from_text(body):
                                    key = ("zenn", h)
                                    if key not in seen:
                                        seen.add(key)
                                        self._add_row(
                                            "Zenn",
                                            h,
                                            uname,
                                            it.get("title", ""),
                                            f"https://zenn.dev{it.get('path','')}",
                                            "本文",
                                        )
                                time.sleep(0.5)
                        time.sleep(0.3)
                except (CancelledError, RequestLimitError, RateLimitError) as e:
                    self.log(f"[Zenn] {e}")
                except Exception as e:
                    self.log(f"[Zenn] エラー: {e}")

            self.log(f"=== 完了: Xアカウント {len(self.results)} 件 / APIリクエスト {f.request_count} 回 ===")
        except Exception as e:
            self.log(f"予期しないエラー: {e}")
        finally:
            self._fetcher = None
            self.after(0, self._finish_run)

    def _finish_run(self):
        self.progress.stop()
        self.run_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")

    def _add_row(self, source, handle, author, title, url, detected_by):
        row = (source, handle, author, title, url, detected_by)
        self.results.append(row)
        self.after(0, self._add_row_main, row)

    def _add_row_main(self, row):
        self.tree.insert("", "end", values=row)

    def save_csv(self):
        if not self.results:
            messagebox.showinfo("情報", "保存するデータがありません")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile="unity_x_accounts.csv",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["source", "x_handle", "x_url", "author", "article_title", "article_url", "detected_by"])
            for source, handle, author, title, url, by in self.results:
                w.writerow([source, handle, f"https://x.com/{handle}", author, title, url, by])
        messagebox.showinfo("完了", f"保存しました:\n{path}")


if __name__ == "__main__":
    App().mainloop()
