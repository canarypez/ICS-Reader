"""每门课一个 SQLite FTS5 索引（courses/<id>/index.db）。

为什么是两张表：

* `pages_tri` / `code_tri` 用 trigram 分词，中文按「连续三个字」建索引，能直接
  匹配任意子串（「量化误差」「cache line」都行），不需要外部分词库；
* `pages_tok` / `code_tok` 存我们自己切的 token（英文词 + 中文二元组），用于
  1–2 个字的中文短查询和 `NEAR()` 邻近查询。trigram 只收 ≥3 字的查询，短词会
  直接查不到。

查不到或 FTS5 编译不进来时，`Library` 会退回旧的关键词打分，功能不中断。
"""
import os
import re
import sqlite3
import threading

import courses
from courses import lang_of

SCHEMA = "3"

#: 代码索引只收这些扩展名；单文件超过 200 KB 的（压缩过的 js、生成的 c）跳过。
_CODE_EXTS = courses.CODE_EXTS
_SKIP_DIRS = {".git", "node_modules", "build", "dist", "__pycache__", ".venv",
              "venv", "target", "out", "bin", "obj", ".idea", ".vscode"}
_MAX_CODE = 200 * 1024

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(cid):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(cid, threading.Lock())


def tokenize(text: str):
    """英文/数字词 + 中文重叠二元组。索引和查询必须用同一套规则。"""
    low = str(text or "").lower()
    out = re.findall(r"[a-z0-9_]{2,}", low)
    for run in re.findall(r"[㐀-鿿]+", low):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _fts_quote(s: str) -> str:
    return '"' + str(s or "").replace('"', '""') + '"'


def _trigram_query(q: str) -> str:
    """trigram 表只收 ≥3 字的词；多词之间是 AND，而不是一个含空格的短语。"""
    parts = [p for p in re.split(r"\s+", str(q or "").strip()) if len(p) >= 3]
    if not parts:
        return ""
    return " AND ".join(_fts_quote(p) for p in parts)


def _token_query(toks):
    if not toks:
        return ""
    if len(toks) == 1:
        return _fts_quote(toks[0])
    # 最多拼 6 个词，避免一条超长 NEAR 在 FTS5 里解析失败
    toks = toks[:6]
    return "NEAR(" + " ".join(_fts_quote(t) for t in toks) + ", 12)"


class CourseIndex:
    def __init__(self, registry):
        self.registry = registry

    # ------------------------------------------------------------------ 建库
    def db_path(self, cid):
        return os.path.join(courses.course_dir(cid), "index.db")

    def _connect(self, path):
        con = sqlite3.connect(path, timeout=20)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    @staticmethod
    def _create(con):
        con.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_tri USING fts5(
            lid UNINDEXED, page UNINDEXED, title, text, tokenize='trigram');
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_tok USING fts5(
            lid UNINDEXED, page UNINDEXED, title, body UNINDEXED,
            tok, tokenize='unicode61');
        CREATE VIRTUAL TABLE IF NOT EXISTS code_tri USING fts5(
            path UNINDEXED, lang UNINDEXED, text, tokenize='trigram');
        CREATE VIRTUAL TABLE IF NOT EXISTS code_tok USING fts5(
            path UNINDEXED, lang UNINDEXED, body UNINDEXED,
            tok, tokenize='unicode61');
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)

    def up_to_date(self, cid):
        path = self.db_path(cid)
        if not os.path.isfile(path):
            return False
        try:
            con = sqlite3.connect(path, timeout=5)
            try:
                row = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                return bool(row) and row[0] == SCHEMA
            finally:
                con.close()
        except Exception:
            return False

    def rebuild(self, cid, store, log=lambda s: None):
        """整门课重建。先写 .tmp 再原子替换，读请求不会看到半成品。"""
        if not self.registry.load(cid):
            return False
        lock = _lock_for(cid)
        with lock:
            path = self.db_path(cid)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(tmp + suffix)
                except OSError:
                    pass
            con = self._connect(tmp)
            try:
                self._create(con)
                n_pages = n_code = 0
                for lid in store.lecture_ids():
                    lec = store.lectures[lid]
                    if lec.get("course_id") != cid:
                        continue
                    for pid, text in lec["page_text"].items():
                        title = lec["page_title"].get(pid) or ""
                        con.execute(
                            "INSERT INTO pages_tri(lid,page,title,text) VALUES(?,?,?,?)",
                            (lid, pid, title, text))
                        con.execute(
                            "INSERT INTO pages_tok(lid,page,title,body,tok) "
                            "VALUES(?,?,?,?,?)",
                            (lid, pid, title, text,
                             " ".join(tokenize(title + "\n" + text))))
                        n_pages += 1
                for full, rel in self._code_files(cid):
                    try:
                        if os.path.getsize(full) > _MAX_CODE:
                            continue
                        with open(full, "r", encoding="utf-8", errors="replace") as f:
                            text = f.read()
                    except OSError:
                        continue
                    if not text.strip():
                        continue
                    lang = lang_of(rel)
                    con.execute(
                        "INSERT INTO code_tri(path,lang,text) VALUES(?,?,?)",
                        (rel, lang, text))
                    con.execute(
                        "INSERT INTO code_tok(path,lang,body,tok) VALUES(?,?,?,?)",
                        (rel, lang, text, " ".join(tokenize(text))))
                    n_code += 1
                con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema',?)",
                            (SCHEMA,))
                con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('pages',?)",
                            (str(n_pages),))
                con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('code',?)",
                            (str(n_code),))
                con.commit()
            finally:
                con.close()
            os.replace(tmp, path)
            log(f"索引 {cid}：{n_pages} 页 / {n_code} 个代码文件")
            return True

    def _code_files(self, cid):
        course = self.registry.load(cid) or {}
        try:
            source = self.registry.source_path(course) or ""
        except Exception:
            source = ""
        for root in self.registry.code_roots(cid):
            # 索引用「相对源码根」的路径，和代码树的写法一致（如
            # ICS-PPT/lectures/1-intro/lecture.py）；源码根在仓库外时才退回
            # 根目录名，避免露绝对路径。
            try:
                root_label = (os.path.relpath(root, source).replace("\\", "/")
                              if source else os.path.basename(root))
            except ValueError:
                root_label = os.path.basename(root)
            # 根在源码目录之外时 relpath 会给出 ../...；别把仓库外的相对路径写进索引。
            if not root_label or root_label == "." or root_label.startswith(".."):
                root_label = os.path.basename(root)
            for base, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
                for name in files:
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in _CODE_EXTS and name.lower() not in (
                            "makefile", "dockerfile"):
                        continue
                    full = os.path.join(base, name)
                    rel = os.path.relpath(full, root).replace("\\", "/")
                    yield full, (root_label + "/" + rel).strip("/")

    def ensure(self, cid, store, log=lambda s: None):
        if not self.up_to_date(cid):
            return self.rebuild(cid, store, log)
        return True

    def ensure_all(self, store, log=lambda s: None):
        for cid in self.registry.course_ids():
            try:
                self.ensure(cid, store, log)
            except Exception as e:
                log(f"索引 {cid} 失败：{e}")

    # ------------------------------------------------------------------ 查询
    def _query_table(self, db, table, cols, match, limit):
        if not match:
            return []
        sql = (f"SELECT {cols}, bm25({table}) AS rank FROM {table} "
               f"WHERE {table} MATCH ? ORDER BY rank LIMIT ?")
        try:
            return db.execute(sql, (match, limit)).fetchall()
        except sqlite3.Error:
            return []

    def pages(self, cid, query, limit=30):
        """返回候选页：{lid,page,title,text,score}，score 越大越相关。"""
        path = self.db_path(cid)
        if not os.path.isfile(path):
            return []
        q = " ".join(str(query or "").split())
        toks = tokenize(q)
        tri = _trigram_query(q)
        tok = _token_query(toks)
        out = {}
        try:
            con = sqlite3.connect(path, timeout=10)
            try:
                for weight, rows in (
                    (1.0, self._query_table(
                        con, "pages_tri", "lid,page,title,text", tri, limit)),
                    (0.75, self._query_table(
                        con, "pages_tok", "lid,page,title,body", tok, limit)),
                ):
                    # SQL 已经按 bm25 升序排好（bm25 是负数，越负越相关），
                    # 这里按名次归一，避免两张表的 bm25 量级不可比，也避免把
                    # 符号搞反 —— 早先 `1/(1+abs(rank))` 实际把最差的排到了最前。
                    total = max(1, len(rows))
                    for i, (lid, page, title, text, _rank) in enumerate(rows):
                        key = (lid, page)
                        s = weight * (1.0 - i / total)
                        if key not in out or s > out[key]["score"]:
                            out[key] = {"lid": lid, "page": page, "title": title or "",
                                        "text": text or "", "score": s}
            finally:
                con.close()
        except sqlite3.Error:
            return []
        return sorted(out.values(), key=lambda x: -x["score"])[:limit]

    def code(self, cid, query, limit=8):
        path = self.db_path(cid)
        if not os.path.isfile(path):
            return []
        q = " ".join(str(query or "").split())
        toks = tokenize(q)
        tri = _trigram_query(q)
        tok = _token_query(toks)
        out = {}
        try:
            con = sqlite3.connect(path, timeout=10)
            try:
                for weight, rows in (
                    (1.0, self._query_table(con, "code_tri",
                                            "path,lang,text", tri, limit)),
                    (0.8, self._query_table(con, "code_tok",
                                            "path,lang,body", tok, limit)),
                ):
                    total = max(1, len(rows))
                    for i, (p, lang, text, _rank) in enumerate(rows):
                        s = weight * (1.0 - i / total)
                        if p not in out or s > out[p]["score"]:
                            out[p] = {"path": p, "lang": lang or "",
                                      "text": text or "", "score": s}
            finally:
                con.close()
        except sqlite3.Error:
            return []
        return sorted(out.values(), key=lambda x: -x["score"])[:limit]
