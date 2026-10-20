"""本地 HTTP 服务：静态资源（前端壳 + 课件 bundle）+ JSON API。

只绑定 127.0.0.1，供本机窗口使用。API：
  GET  /api/course           课程列表 + 大纲 + 是否有 key
  POST /api/chat             {lecture, page, question} -> {answer}
  POST /api/test_api         {api_base, model, api_key} -> 连接自检结果
  POST /api/models           {api_base, api_key} -> 该站可用的模型 id 列表
  POST /api/search           {query, lecture|null} -> {results}
  POST /api/export_pdf       {lecture, dest} -> 启动后台导出
  GET  /api/export/status    导出进度
  POST /api/reveal           {path} -> 在资源管理器里定位该文件
  GET  /api/wizard           环境检测结果
  POST /api/wizard/install   {key, dir?} -> 启动后台安装
  GET  /api/wizard/status    安装进度
  GET  /api/config           当前配置（key 打码）
  POST /api/config           保存配置
  POST /api/update           触发后台更新
  GET  /api/update/status    更新进度
  POST /api/chat/stream      {lecture, page, question, cid, history} -> SSE 流式回答
  POST /api/history/list     {lecture} -> 本讲的对话列表
  POST /api/history/new      {lecture, cid?} -> 新对话 id
  POST /api/history/get      {lecture, id} -> 整条对话
  POST /api/history/delete   {lecture, id} -> 删掉一条对话
"""
import json
import os
import mimetypes
import re
import secrets
import select
import socket
import subprocess
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

import agent
import config as cfgmod
import courses
import docs
import history as hist
import lecturekit_bridge as lecturekit
import search
import sync
import wizard

#: 浏览器直传（没有原生文件对话框时）的单文件上限。再大就建议用桌面版的对话框，
#: 那条路只传路径、不读进内存。
MAX_UPLOAD = 400 * 1024 * 1024


class Job:
    """一个后台任务的状态：running + 逐行日志 + 结果 / 错误。

    更新、导出 PDF、装依赖三件事的形状完全一样（慢、要报进度、要能轮询），
    共用这一个类，别再抄三份。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.lines = []
        self.error = None
        self.result = None

    def snapshot(self):
        with self._lock:
            return {
                "running": self.running,
                "lines": list(self.lines),
                "error": self.error,
                "result": self.result,
            }

    def _log(self, line):
        with self._lock:
            self.lines.append(str(line))

    def start(self, work, name="job"):
        """work(log) 在后台线程里跑；已在跑则返回 False（不排队、不重入）。"""
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.lines = []
            self.error = None
            self.result = None

        def run():
            try:
                result = work(self._log)
                with self._lock:
                    self.result = result
            except Exception as e:
                traceback.print_exc()
                with self._lock:
                    self.error = str(e)
            finally:
                with self._lock:
                    self.running = False

        threading.Thread(target=run, daemon=True, name=name).start()
        return True


class App:
    def __init__(self, config):
        self.config = config
        # 课程注册表负责迁移旧 documents/、维护 course.json / 进度 / 标注。
        self.registry = courses.CourseRegistry(
            config, cfgmod.build_root(), docs.documents_root())
        self.registry.ensure()
        # Library = 所有课程合并成的知识库。检索、上下文、历史共用一套接口。
        self.store = docs.Library(self.registry)
        self.index = search.CourseIndex(self.registry)
        self.store.index = self.index
        self.update_job = Job()
        self.export_job = Job()
        self.install_job = Job()
        self.course_job = Job()
        self._auto_last = {}

        # 索引在后台建，不挡窗口首绘。第一次启动会扫一遍仓库，可能要几秒。
        threading.Thread(
            target=lambda: self.index.ensure_all(
                self.store, lambda s: print(s, flush=True)),
            daemon=True, name="index").start()

        # 仓库课程的定时拉取。只在有 .git 的仓库上跑：本地普通目录没有「变更」
        # 可拉，反复重新渲染只会白烧 CPU。和用户手动导入共用 course_job，忙就跳过。
        threading.Thread(target=self._auto_update_loop, daemon=True,
                         name="course-auto").start()

    # ---- 定时更新 ----
    def _auto_update_loop(self):
        time.sleep(15)      # 先让首屏、迁移和索引线程安顿下来
        while True:
            try:
                self._auto_update_once()
            except Exception as e:
                print("课程定时更新失败：", e, flush=True)
            time.sleep(60)

    def _auto_update_once(self):
        """一次最多起一门课，和导入/手动更新共用 course_job。"""
        if self.course_job.running or self.update_job.running:
            return
        now = time.time()
        for card in self.registry.cards():
            if card.get("kind") != "repo" or not card.get("auto_update"):
                continue
            mins = int(card.get("update_minutes") or 0)
            src = (card.get("source") or {}).get("path") or ""
            if mins <= 0 or not src or not os.path.isdir(os.path.join(src, ".git")):
                continue
            cid = card["id"]
            # 导入/上次手动更新刚写过 course.json，updated 就是一个现成的基准，
            # 否则导入完 15 秒就会再拉一次。
            last = self._auto_last.get(cid) or float(card.get("updated") or 0)
            if now - last < mins * 60:
                continue
            self._auto_last[cid] = now
            self.start_course_update(cid)
            return          # 下一轮再看别的课

    # ---- 更新 ----
    def start_update(self):
        def work(log):
            sync.update(self.config, cfgmod.build_root(), cfgmod.resource("stubs"), log)
            self.store.reload()   # 渲染完重读 corpus
            self.index.rebuild("ics", self.store, log)

        return self.update_job.start(work, name="update")

    # ---- 课程导入 / 更新 ----
    def _course_dir(self, cid):
        return courses.course_dir(cid)

    def _resolve_repo_source(self, source: str, title: str, log):
        """本地路径直接登记；URL 浅克隆到课程目录。返回 (course, path)。"""
        source = (source or "").strip()
        is_url = bool(re.match(r"^(https?://|git@|ssh://)", source))
        if is_url:
            course = self.registry.create(title or "导入的课程", kind="repo",
                                          source={"type": "git", "path": None,
                                                  "url": source})
            course["auto_update"] = True      # 仓库课程默认定时 git pull
            course["update_minutes"] = 30
            dst = os.path.join(self._course_dir(course["id"]), "source")
            sync.clone(source, dst, log)
            course["source"]["path"] = dst
            self.registry.save(course["id"], course)
            return course, dst
        path = os.path.abspath(source)
        if not os.path.isdir(path):
            raise RuntimeError(f"本地仓库目录不存在：{path}")
        course = self.registry.create(
            title or os.path.basename(path) or "导入的课程", kind="repo",
            source={"type": "local", "path": path, "url": None})
        course["auto_update"] = True          # 本地 git 仓库同样定时 pull
        course["update_minutes"] = 30
        return course, path

    def start_repo_import(self, source, title="", with_decks=True):
        def work(log):
            course, path = self._resolve_repo_source(source, title, log)
            cid = course["id"]
            log(f"扫描仓库：{path}")
            info = courses.scan_repo(path)
            course["code_roots"] = info.get("code_roots") or []
            self.registry.save(cid, course)
            log(f"发现 {len(info['lectures'])} 个 lecture.py、"
                f"{len(info['decks'])} 份 PDF/PPTX/MD、"
                f"{len(info['code_roots'])} 个代码根")

            rendered = 0
            if info["lectures"]:
                kit = sync.find_lecturekit_root(path)
                if not kit:
                    log("没找到 lecturekit 包，跳过课件渲染（仍导入静态资料与代码）。")
                else:
                    python = (self.config.get("python") or "python").strip()
                    out_root = self.registry.render_root(cid)
                    os.makedirs(out_root, exist_ok=True)
                    for lec in info["lectures"]:
                        src = os.path.join(path, lec["path"])
                        out = os.path.join(out_root, lec["name"])
                        log(f"渲染 {lec['name']} ...")
                        ok, detail = sync.render_lecture(
                            src, out, python, cfgmod.resource("stubs"), kit, log)
                        if ok:
                            rendered += 1
                            log(f"  {lec['name']} 成功")
                        else:
                            log(f"  {lec['name']} 失败：{detail[-400:]}")
            # 静态资料：只加还没导入过的同名文件
            existing = {(self.store.docs.get(d) or {}).get("name") or ""
                        for d in self.store.docs.lectures}
            added = 0
            if with_decks:
                for deck in info["decks"]:
                    full = os.path.join(path, deck["path"])
                    if deck["name"] in existing:
                        log(f"跳过已导入的资料：{deck['name']}")
                        continue
                    try:
                        log(f"导入资料 {deck['name']} ...")
                        self.store.docs.add(
                            full, deck["name"], log=log, course_id=cid,
                            out_root=self.registry.items_root(cid))
                        added += 1
                    except Exception as e:
                        log(f"  导入失败：{e}")
            self.store.reload()
            self.index.rebuild(cid, self.store, log)
            log(f"完成：渲染 {rendered} 讲，导入 {added} 份资料")
            return {"course_id": cid, "title": course["title"],
                    "lectures": rendered, "documents": added}

        return self.course_job.start(work, name="course-import")

    def start_course_update(self, cid):
        self._auto_last[cid] = time.time()   # 手动更新也算一次，别马上又自动跑

        def work(log):
            course = self.registry.load(cid)
            if not course:
                raise RuntimeError("课程不存在")
            if cid == "ics":
                sync.update(self.config, cfgmod.build_root(),
                            cfgmod.resource("stubs"), log)
                self.store.reload()
                self.index.rebuild(cid, self.store, log)
                return {"course_id": cid}
            path = self.registry.source_path(course) or ""
            if os.path.isdir(os.path.join(path, ".git")):
                sync.pull(path, log)
            kit = sync.find_lecturekit_root(path) if path else None
            root = self.registry.render_root(cid)
            if kit and os.path.isdir(root):
                python = (self.config.get("python") or "python").strip()
                info = courses.scan_repo(path)
                for lec in info["lectures"]:
                    src = os.path.join(path, lec["path"])
                    ok, detail = sync.render_lecture(
                        src, os.path.join(root, lec["name"]), python,
                        cfgmod.resource("stubs"), kit, log)
                    log(("  " if ok else "  失败：") + lec["name"] +
                        ("" if ok else " " + detail[-200:]))
            self.store.reload()
            self.index.rebuild(cid, self.store, log)
            log("更新完成")
            return {"course_id": cid}

        return self.course_job.start(work, name="course-update")

    def start_course_delete(self, cid):
        course = self.registry.load(cid)
        if not course:
            raise RuntimeError("课程不存在")
        lids = {it["id"] for it in self.registry.items(cid)}
        # 只靠 registry.items 会漏掉迁移失败、只存在于 documents/ 的旧资料；
        # Library 里 course_id 已归一化，用它补齐。
        lids.update(lid for lid, entry in self.store.lectures.items()
                    if entry.get("course_id") == cid)
        doc_ids = [lid for lid in lids if docs.DOC_ID_RE.match(lid)]
        if not self.registry.delete(cid, doc_ids=doc_ids):
            raise RuntimeError("课程目录或旧资料原件被占用，没有删干净，请稍后重试")
        for lid in lids:
            hist.clear(lid)
        self.store.reload()
        return {"course_id": cid}

    # ---- 导出 PDF ----
    def start_export(self, lid, dest):
        bundle = os.path.join(cfgmod.build_root(), lid)
        if not os.path.isdir(bundle):
            return False
        if not dest:
            # 浏览器直开（没有 pywebview 的原生「另存为」）时的落点
            dest = os.path.join(cfgmod.DATA_DIR, "exports", lid + ".pdf")
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        repo = self.config.get("repo") or ""
        ppt = os.path.join(repo, "ICS-PPT")
        python = (self.config.get("python") or "python").strip()
        script = cfgmod.resource("pdf_export.py")

        def work(log):
            env = sync.render_env(ppt, cfgmod.resource("stubs"))
            env["PYTHONIOENCODING"] = "utf-8"   # 目标路径可能含中文
            log("正在生成 PDF（marp 重排 + 合并目录，需要几十秒）…")
            r = subprocess.run(
                [python, script, bundle, dest],
                cwd=ppt, env=env, capture_output=True, text=True, encoding="utf-8",
                errors="replace", creationflags=sync.CREATE_NO_WINDOW,
            )
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-6:]
            for line in tail:
                log(line)
            if r.returncode != 0:
                raise RuntimeError(self._export_error(r.returncode, tail))
            if not os.path.isfile(dest):
                raise RuntimeError("导出结束但没找到文件：" + dest)
            log("导出完成")
            return {"path": dest}

        return self.export_job.start(work, name="export")

    @staticmethod
    def _export_error(code, tail):
        text = " ".join(tail)
        if "Chrome" in text or "chrome" in text or code == 1 and "Browser" in text:
            return "找不到 Chrome，无法排版目录页。可在「环境检测」里安装 Chrome，或用 Edge 兜底。"
        if "marp" in text.lower() or "npx" in text:
            return "marp 不可用（课件排版工具）。请在「环境检测」里安装 marp-cli。"
        if "lecturekit" in text or "ModuleNotFoundError" in text:
            return "渲染环境不完整（lecturekit 未装好）。请打开「环境检测」逐项补全。"
        return f"导出失败（退出码 {code}）：{text[-200:]}"

    # ---- 环境检测 / 安装 ----
    def wizard_payload(self):
        items = wizard.detect(self.config)
        s = wizard.summary(items)
        dismissed = set(self.config.get("wizard_dismissed") or [])
        return {
            "items": items,
            "ok": s["ok"],
            "missing": s["missing"],
            # 启动时自动弹窗的条件：确实缺东西，且没有一项是用户说过不再提示的
            "should_prompt": bool(s["missing"]) and not all(m in dismissed for m in s["missing"]),
        }

    def start_install(self, key, target_dir=None):
        def work(log):
            res = wizard.install(key, self.config, log, target_dir=target_dir)
            if res.get("config"):
                # 装完把解析到的绝对路径（python / repo）写回配置，否则下次还是找不到
                self.config = cfgmod.save_config(res["config"])
            if not res.get("ok"):
                raise RuntimeError("安装未成功，详见上方输出。")
            return res

        return self.install_job.start(work, name="install")

    # ---- 课程列表 ----
    def course_payload(self, cid=None):
        cid = cid or None
        lectures, documents = [], []
        for lid in self.store.lecture_ids():
            lec = self.store.lectures[lid]
            if cid and lec.get("course_id") != cid:
                continue
            if lec.get("kind") == "doc":
                documents.append({
                    "id": lid, "vid": lec["vid"], "title": lec["title"],
                    "name": lec.get("name") or "", "kind": "doc",
                    "type": lec.get("doc_type") or "pdf",
                    "mode": lec.get("mode") or "pdf",
                    "course_id": lec.get("course_id") or "",
                    "page_count": len(lec["pages"]),
                    "has_text": lec.get("has_text"),
                    "base": lec.get("base") or f"/docview.html?doc={lid}",
                    "added": lec.get("added") or 0,
                })
                continue
            lectures.append({
                "id": lid,
                "vid": lec["vid"],
                "title": lec["title"],
                "kind": lec.get("kind") or "ics",
                "course_id": lec.get("course_id") or "ics",
                "base": lec.get("base") or f"/lectures/{lid}/",
                "page_count": len(lec["pages"]),
                "outline": "\n".join(self.store._outline_text(lec["tree"])),
            })
        # 旧的「ICS 讲次」顺序依赖目录名排序；资料仍按加入时间倒序
        documents.sort(key=lambda x: x.get("added") or 0, reverse=True)
        out = {
            "lectures": lectures,
            "documents": documents,
            "has_key": bool(self.config.get("api_key")),
            "model": self.config.get("model"),
            "auto_update": bool(self.config.get("auto_update")),
        }
        if cid:
            card = self.registry.card(cid)
            out["course"] = card
            out["last"] = (card or {}).get("last")
            out["code_roots"] = (card or {}).get("code_roots") or []
        return out

    def course_cards(self):
        return {"courses": self.registry.cards(),
                "has_key": bool(self.config.get("api_key"))}

    def target_course(self, cid="", title=""):
        """上传落点：明确指定的课程 > 新课程标题 > 内置「我的资料」。"""
        cid = str(cid or "").strip()
        if cid and self.registry.load(cid):
            return cid
        title = str(title or "").strip()
        if title:
            return self.registry.create(title, kind="files")["id"]
        if self.registry.load(courses.MYDOCS_ID):
            return courses.MYDOCS_ID
        # 全新安装第一次上传：用固定的 mydocs id，后续上传都落在这里
        data = self.registry.create(courses.MYDOCS_TITLE, kind="files",
                                    cid=courses.MYDOCS_ID)
        return data["id"]


def make_server(app: App, port: int):
    app_ref = app
    web_dir = cfgmod.resource("web")
    # 正在运行的交互式示例：run token -> lecturekit.demo.Terminal
    terminals = {}
    terminals_lock = threading.Lock()
    # (course id, 讲目录名) -> 源码目录；课件目录不会天天改名，缓存省得每个
    # 源码请求都把仓库走一遍。
    source_dirs = {}
    source_dirs_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # 静默访问日志

        # ---- helpers ----
        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sse(self, obj):
            """发一帧 SSE。写失败（客户端断开）由调用方接住。

            wbufsize 是 0，wfile.write 直接就是 sendall，写入路径上没有缓冲；
            flush 留着当保险。
            """
            self.wfile.write(
                ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        def _chat_stream(self):
            """流式问答。

            整个分支待在 do_POST 的 try **之外**：一旦发过 200 和一堆帧，再让那个
            `except -> _json(500)` 去 send_response，就是二次发状态行，报出来的东西
            比原始错误还难看。所以这里自己兜住一切。
            """
            try:
                b = self._read_json()
            except Exception:
                return self._json({"error": "请求体不是合法 JSON"}, 400)

            lid = b.get("lecture")
            pid = b.get("page")
            q = (b.get("question") or "").strip()
            cid = str(b.get("cid") or "").strip()
            history = b.get("history") or []
            course = str(b.get("course") or "").strip()
            code = b.get("code") if isinstance(b.get("code"), dict) else None
            if not q:
                return self._json({"error": "问题不能为空"}, 400)
            if not course and lid:
                course = app_ref.registry.course_of_item(lid) or ""
            route = agent.route_question(q, code)

            # 先建生成器并拉第一帧，把「没填 key」「地址不对」「401」这些在**发响应头
            # 之前**逼出来 —— 那会儿还能规规矩矩回一个 JSON 错误。
            gen = agent.stream_chat(app_ref.config, app_ref.store, lid, pid, q, history,
                                    course_id=course or None, code=code, route=route)
            try:
                first = next(gen)
            except StopIteration:
                first = None
            except Exception as e:
                return self._json({"error": str(e)}, 400)

            # 到这儿请求已经通了，发头。之后不写 Content-Length —— 靠连接关闭定界，
            # 这正是 SSE 要的。实测 WebView2 会逐块交给 fetch 的 ReadableStream。
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            tracked = bool(lid) and bool(cid)      # 没有当前讲就没处显示，不记
            if tracked:
                hist.ensure_conversation(lid, cid, q, page=pid)

            parts = []
            aborted = False
            try:
                self._sse({"type": "meta", "cid": cid, "course": course,
                           "route": route})
                if first:
                    parts.append(first)
                    self._sse({"type": "delta", "content": first})

                for text in gen:
                    parts.append(text)
                    self._sse({"type": "delta", "content": text})
            except OSError:
                aborted = True                 # 客户端没了：关窗口 / 切讲 / 点了停止
            except Exception as e:
                aborted = True
                try:
                    self._sse({"type": "error", "error": str(e)})
                except OSError:
                    pass

            answer = "".join(parts)
            if tracked and answer:
                hist.finish_turn(lid, cid, answer, stopped=aborted)

            if aborted:
                return                         # 连接已经没了，再写只是继续抛

            self._sse({"type": "done"})

            # 标题帧在 done **之后**，前端必须一直读到 EOF 才收得到。生成失败就不发，
            # 列表里那条用问题前 10 字的兜底标题照样能用。
            title = agent.generate_title(app_ref.config, q, answer)
            if title and tracked:
                hist.set_title(lid, cid, title)
            if title:
                try:
                    self._sse({"type": "title", "title": title})
                except OSError:
                    pass


        def _read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _qs(self):
            """把查询串读成 {key: 解码后的第一个值}。"""
            out = {}
            for kv in urlparse(self.path).query.split("&"):
                if not kv:
                    continue
                k, _, v = kv.partition("=")
                out.setdefault(unquote(k), unquote(v))
            return out

        def _course_of(self, body=None):
            """从请求体/查询串里取课程 id；没有就用 item 反查。"""
            body = body or {}
            cid = str(body.get("course") or "").strip()
            if not cid:
                cid = str(self._qs().get("course") or "").strip()
            if not cid:
                lid = str(body.get("lecture") or "").strip()
                if lid:
                    cid = app_ref.registry.course_of_item(lid) or ""
            return cid

        def _probe_config(self):
            """用界面上「当前填着」的值去探测，不必先保存。

            测试连接 / 列模型都吃这一套，所以放一处。
            """
            b = self._read_json()
            probe = dict(app_ref.config)
            # base / model 原样照收：界面清空了就是清空了，不能偷偷回退到存着的值，
            # 否则用户会看到「地址」和「实际打的地址」对不上
            for k in ("api_base", "model"):
                if k in b:
                    probe[k] = str(b[k]).strip()
            # key 不同：界面上回显的是打码后的（••••1234），没重填就用存着的那把
            key = str(b.get("api_key") or "").strip()
            if key and not cfgmod.is_masked(key):
                probe["api_key"] = key
            return probe

        def _static_path(self, path):
            """静态 URL -> 磁盘文件，返回 (full, 错误状态)。只允许 web/ 与
            build/render 之下的文件，防目录穿越。"""
            if path == "/":
                path = "/index.html"
            rel = path.lstrip("/")
            allowed_roots = [os.path.normpath(web_dir),
                             os.path.normpath(cfgmod.build_root())]
            if rel.startswith("lectures/"):
                rest = rel[len("lectures/"):]
                if "/" not in rest or ".." in rest or "\\" in rest:
                    return None, 400
                full = os.path.join(cfgmod.build_root(), rest)
            elif rel.startswith("course/"):
                # /course/<cid>/lectures/<dir>/...
                bits = rel.split("/")
                if len(bits) < 4 or bits[2] != "lectures" or ".." in rel or "\\" in rel:
                    return None, 400
                cid = bits[1]
                if not courses.COURSE_ID_RE.match(cid):
                    return None, 400
                root = os.path.normpath(app_ref.registry.render_root(cid))
                rest = "/".join(bits[3:])
                full = os.path.normpath(os.path.join(root, rest))
                allowed_roots.append(root)
            else:
                full = os.path.join(web_dir, rel)
                if os.path.isdir(full):
                    full = os.path.join(full, "index.html")
            full = os.path.normpath(full)
            if not any(full.startswith(root + os.sep) or full == root
                       for root in allowed_roots):
                return None, 403
            return full, 0

        def _file(self, path):
            full, status = self._static_path(path)
            if not full:
                return self._json({"error": "forbidden" if status == 403 else "bad path"},
                                  status or 400)
            if not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            ctype = "text/html; charset=utf-8"
            if full.endswith(".css"):
                ctype = "text/css; charset=utf-8"
            elif full.endswith(".js"):
                ctype = "application/javascript; charset=utf-8"
            elif full.endswith(".json"):
                ctype = "application/json; charset=utf-8"
            elif full.endswith(".svg"):
                ctype = "image/svg+xml"
            elif full.endswith(".woff2"):
                ctype = "font/woff2"
            elif full.endswith(".png"):
                ctype = "image/png"
            with open(full, "rb") as f:
                data = f.read()
            # 静态 render 的 slides.html 不带 lecturekit 的文件面板和示例终端
            # （只有 view --watch 的开发服务器会注入），在这里补上。
            if os.path.basename(full).lower() == "slides.html":
                data = lecturekit.inject_deck(
                    data.decode("utf-8", errors="replace")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---- lecturekit 的文件面板 / 示例终端 ----
        def _same_origin(self):
            origin = self.headers.get("Origin")
            return not origin or origin == "http://" + (self.headers.get("Host") or "")

        def _lecturekit_context(self):
            """从 Referer（slides.html 的地址）认出这一讲。

            页面里的脚本只会请求同源地址，Referer 是完整 URL；请求体/查询里
            不带路径，这样浏览器没有办法指定服务端读哪个文件。
            """
            path = urlsplit(self.headers.get("Referer") or "").path
            bits = path.split("/")
            cid = name = ""
            if path.startswith("/lectures/"):
                if len(bits) == 4 and bits[3] == "slides.html":
                    cid, name = "ics", bits[2]
            elif path.startswith("/course/"):
                if (len(bits) == 6 and bits[2] == "lectures"
                        and bits[5] == "slides.html"):
                    cid, name = bits[1], bits[4]
            if not cid or not name or not courses.COURSE_ID_RE.match(cid):
                return None
            full, _status = self._static_path(path)
            if not full:
                return None
            course = app_ref.registry.load(cid)
            if not course:
                return None
            source_root = app_ref.registry.source_path(course) or ""
            key = (cid, name, source_root)
            with source_dirs_lock:
                if key in source_dirs:
                    lec_dir = source_dirs[key]
                else:
                    lec_dir = lecturekit.find_lecture_dir(source_root, name)
                    source_dirs[key] = lec_dir
            return {
                "cid": cid, "name": name, "source_root": source_root,
                "source_dir": lec_dir, "render_dir": os.path.dirname(full),
            }

        def _serve_source(self):
            """`GET /__source?id=...`：返回课件点名的那个文件。"""
            if not self._same_origin():
                return self._json({"error": "cross-origin"}, 403)
            ctx = self._lecturekit_context()
            if not ctx or not ctx["source_dir"]:
                return self._json({"error": "找不到这一讲的源码目录"}, 404)
            wanted = (parse_qs(urlsplit(self.path).query).get("id") or [""])[0]
            rel = lecturekit.read_sources(ctx["render_dir"]).get(wanted)
            if rel is None:
                return self._json({"error": "课件里没有这个文件"}, 404)
            full = lecturekit.safe_file(ctx["source_dir"], rel)
            if not full:
                return self._json({"error": "文件不存在"}, 404)
            try:
                text = lecturekit.load_text(full)
            except OSError:
                return self._json({"error": "文件读不出来"}, 404)
            payload = {"path": rel, "text": text}
            _demo, source_mod = lecturekit.load_modules(ctx["source_root"])
            if source_mod is not None:
                try:
                    tokens = source_mod.highlight(rel, text)
                except Exception:
                    tokens = None
                if tokens is not None:
                    payload["tokens"] = tokens
            return self._json(payload)

        def _demo_request(self, limit):
            """请求体；不是本应用发的 JSON 就返回 None。"""
            if "json" not in (self.headers.get("Content-Type") or "").lower():
                return None
            if not self._same_origin():
                return None
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > limit:
                return None
            try:
                payload = json.loads(self.rfile.read(length))
            except (OSError, ValueError):
                return None
            return payload if isinstance(payload, dict) else None

        def _serve_demo(self):
            """`POST /__demo`：在源码目录里跑 demos.json 里的命令，NDJSON 流回。"""
            payload = self._demo_request(4096)
            demo_id = payload.get("id") if payload else None
            if not isinstance(demo_id, str):
                return self._json({"error": "bad request"}, 400)
            ctx = self._lecturekit_context()
            if not ctx or not ctx["source_dir"]:
                return self._json({"error": "找不到这一讲的源码目录"}, 404)
            demo_mod, _source = lecturekit.load_modules(ctx["source_root"])
            if demo_mod is None:
                return self._json({"error": "这份课件的仓库里没有 lecturekit，无法运行示例"},
                                  501)
            spec = demo_mod.read(Path(ctx["render_dir"])).get(demo_id)
            if spec is None:
                return self._json({"error": "课件里没有这个示例"}, 404)
            refusal = demo_mod.why_not(spec)
            if refusal is not None:
                return self._json({"error": refusal}, 501)
            terminal = None
            if spec.interactive:
                terminal = demo_mod.Terminal(payload.get("cols", 80),
                                             payload.get("rows", 24))
            return self._stream_demo(demo_mod, spec, ctx["source_dir"], terminal)

        def _stream_demo(self, demo_mod, spec, cwd, terminal=None):
            """一次示例运行；连接断开就是停止键（生成器收尾时整组杀掉）。"""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.close_connection = True
            token = None
            events = demo_mod.stream(
                spec.command, cwd=Path(cwd),
                timeout_s=demo_mod.timeout_for(spec, demo_mod.DEFAULT_TIMEOUT_S),
                terminal=terminal)
            try:
                if terminal is not None:
                    token = secrets.token_urlsafe(16)
                    with terminals_lock:
                        terminals[token] = terminal
                    self._send_ndjson({"t": "stdin", "run": token})
                for event in events:
                    if self._listener_gone():
                        break
                    self._send_ndjson(self._demo_event(demo_mod, event))
            except (BrokenPipeError, ConnectionResetError, ValueError):
                pass
            finally:
                try:
                    events.close()
                except Exception:
                    pass
                if token is not None:
                    with terminals_lock:
                        terminals.pop(token, None)

        @staticmethod
        def _demo_event(demo_mod, event):
            if isinstance(event, demo_mod.Chunk):
                return {"t": "out", "d": event.text}
            if isinstance(event, demo_mod.Command):
                return {"t": "cmd", "d": event.text}
            if isinstance(event, demo_mod.Tick):
                return {"t": "tick", "elapsed": round(event.elapsed_s, 3)}
            return {"t": "end", "exit": event.exit_code,
                    "timedOut": event.timed_out,
                    "duration": round(event.duration_s, 3)}

        def _send_ndjson(self, payload):
            self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n")
                             .encode("utf-8"))
            self.wfile.flush()

        def _listener_gone(self):
            """浏览器是否已经关了这条连接（停止键）。"""
            try:
                ready, _, _ = select.select([self.connection], [], [], 0)
                if not ready:
                    return False
                return self.connection.recv(1, socket.MSG_PEEK) == b""
            except (OSError, ValueError):
                return True

        def _terminal_for(self, payload):
            token = payload.get("run") if payload else None
            if not isinstance(token, str):
                return None
            with terminals_lock:
                return terminals.get(token)

        def _serve_demo_stdin(self):
            payload = self._demo_request(64 * 1024)
            data = payload.get("data") if payload else None
            if not isinstance(data, str):
                return self._json({"error": "bad request"}, 400)
            terminal = self._terminal_for(payload)
            if terminal is None:
                return self._json({"error": "no such run"}, 404)
            if not terminal.write(data.encode("utf-8", errors="replace")):
                return self._json({"error": "not reading its input"}, 409)
            self.send_response(204)
            self.end_headers()

        def _serve_demo_winsize(self):
            payload = self._demo_request(4096)
            if payload is None:
                return self._json({"error": "bad request"}, 400)
            terminal = self._terminal_for(payload)
            if terminal is None:
                return self._json({"error": "no such run"}, 404)
            terminal.resize(payload.get("cols"), payload.get("rows"))
            self.send_response(204)
            self.end_headers()

        def _bytes(self, data, ctype, cache=False):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if cache:
                self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            try:
                self.wfile.write(data)
            except OSError:
                pass  # 用户翻页太快，旧请求的管道已经关了

        # ---- 用户上传的资料 ----
        def _doc_get(self, path):
            """`/api/doc/<id>/meta|page/N.png|text/N|slide/N|asset/<name>`。

            doc id 只收 12 位十六进制，所有 `<n>` 都先转 int 再查范围，路径参数
            不会拼进文件系统。
            """
            parts = path[len("/api/doc/"):].split("/")
            doc_id = parts[0] if parts else ""
            if not docs.DOC_ID_RE.match(doc_id):
                return self._json({"error": "资料不存在"}, 404)
            entry = app_ref.store.docs.get(doc_id)
            if not entry:
                return self._json({"error": "资料不存在"}, 404)

            if len(parts) == 2 and parts[1] == "meta":
                pages = []
                for i, p in enumerate(entry["pages"]):
                    pid = p.get("id")
                    pages.append({
                        "index": i,
                        "id": pid,
                        "number": p.get("number") or (i + 1),
                        "title": p.get("title") or "",
                        "has_text": bool((entry["page_text"].get(pid) or "").strip()),
                        "notes": p.get("notes") or "",
                    })
                return self._json({
                    "document": app_ref.store.docs.payload_one(doc_id),
                    "pages": pages,
                    "outline": entry.get("outline") or [],
                })

            if len(parts) != 3:
                return self._json({"error": "not found"}, 404)
            kind, value = parts[1], parts[2]

            if kind == "asset":
                full = app_ref.store.docs.asset_path(doc_id, value)
                if not full:
                    return self._json({"error": "not found"}, 404)
                ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                with open(full, "rb") as f:
                    return self._bytes(f.read(), ctype, cache=True)

            try:
                number = int(value[:-4] if kind == "page" and value.endswith(".png") else value)
            except ValueError:
                return self._json({"error": "bad page"}, 400)

            if kind == "page" and value.endswith(".png"):
                width = 1600
                try:
                    qs = urlparse(self.path).query
                    for kv in qs.split("&"):
                        if kv.startswith("w="):
                            width = int(kv[2:] or 1600)
                except Exception:
                    width = 1600
                try:
                    data = app_ref.store.docs.render_page(doc_id, number, width=width)
                except Exception as e:
                    return self._json({"error": str(e)}, 404)
                return self._bytes(data, "image/png", cache=True)

            if kind == "layer":
                try:
                    layer = app_ref.store.docs.text_layer(doc_id, number)
                except Exception as e:
                    return self._json({"error": str(e)}, 404)
                if layer is None:
                    return self._json({"error": "这一页没有文本层"}, 404)
                return self._json(layer)

            if kind == "notes":
                return self._json({"number": number,
                                   "notes": app_ref.store.docs.page_notes(doc_id, number)})

            if kind == "text":
                if number < 1 or number > len(entry["pages"]):
                    return self._json({"error": "页码超出范围"}, 404)
                page = entry["pages"][number - 1]
                return self._json({
                    "number": page.get("number") or number,
                    "title": page.get("title") or "",
                    "text": entry["page_text"].get(page.get("id"), ""),
                })

            if kind == "slide":
                slide = app_ref.store.docs.page_slide(doc_id, number)
                if not slide:
                    return self._json({"error": "这一页没有可用的幻灯片渲染"}, 404)
                return self._json({
                    "number": number,
                    "title": slide.get("title") or "",
                    "html": slide.get("html") or "",
                })

            return self._json({"error": "not found"}, 404)

        def _upload_document(self):
            """两种入口：

            * JSON `{"path": "..."}`：桌面版原生文件对话框选完直接给路径；
            * 原始二进制 + `X-Filename`：浏览器直开时的 `<input type=file>` 兜底。
              header 只能放 ASCII，所以文件名由前端 `encodeURIComponent` 过一遍。

            解析/转换可能几十秒（PPTX 要过 PowerPoint），这里是同步的：前端显示
            「正在导入」并等 fetch 返回，比再搭一套轮询简单。
            """
            ctype = (self.headers.get("Content-Type") or "").lower()
            if ctype.startswith("application/json"):
                try:
                    b = self._read_json()
                except Exception:
                    return self._json({"error": "请求体不是合法 JSON"}, 400)
                path = str(b.get("path") or "").strip()
                if not path:
                    return self._json({"error": "没有选择文件"}, 400)
                cid = app_ref.target_course(b.get("course"), b.get("title"))
                try:
                    item = app_ref.store.docs.add(
                        path, b.get("name"), course_id=cid,
                        out_root=app_ref.registry.items_root(cid))
                except Exception as e:
                    return self._json({"error": str(e)}, 400)
                app_ref.store.reload()
                app_ref.index.rebuild(cid, app_ref.store)
                return self._json({"ok": True, "course_id": cid,
                                   "document":
                                   app_ref.store.docs.payload_one(item["id"])})

            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return self._json({"error": "空文件"}, 400)
            if n > MAX_UPLOAD:
                return self._json({"error": "文件太大（上限 400 MB）"}, 413)
            qs = dict(kv.split("=", 1) if "=" in kv else (kv, "")
                      for kv in urlparse(self.path).query.split("&") if kv)
            name = unquote(self.headers.get("X-Filename") or qs.get("name", "upload"))
            ext = os.path.splitext(name)[1].lower()
            if ext not in (".pdf", ".pptx", ".md"):
                return self._json({"error": "只支持 PDF、PPTX 和 Markdown 文件。"}, 400)
            cid = app_ref.target_course(unquote(qs.get("course", "")),
                                        unquote(qs.get("title", "")))

            tmpdir = os.path.join(cfgmod.DATA_DIR, "tmp")
            os.makedirs(tmpdir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=tmpdir, suffix=ext)
            try:
                with os.fdopen(fd, "wb") as f:
                    left = n
                    while left > 0:
                        chunk = self.rfile.read(min(1024 * 1024, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
                if left:
                    return self._json({"error": "上传中断，文件不完整"}, 400)
                try:
                    item = app_ref.store.docs.add(
                        tmp, name, course_id=cid,
                        out_root=app_ref.registry.items_root(cid))
                except Exception as e:
                    return self._json({"error": str(e)}, 400)
                app_ref.store.reload()
                app_ref.index.rebuild(cid, app_ref.store)
                return self._json({"ok": True, "course_id": cid,
                                   "document":
                                   app_ref.store.docs.payload_one(item["id"])})
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

        # ---- routes ----
        def do_GET(self):
            p = urlparse(self.path).path
            qs = self._qs()
            if p == "/__source":
                return self._serve_source()
            if p == "/api/courses":
                return self._json(app_ref.course_cards())
            if p == "/api/course":
                return self._json(app_ref.course_payload(qs.get("course") or None))
            if p == "/api/course/job":
                return self._json(app_ref.course_job.snapshot())
            if p == "/api/progress":
                cid = self._course_of(qs)
                if not cid or not app_ref.registry.load(cid):
                    return self._json({"error": "课程不存在"}, 404)
                prog = app_ref.registry.progress(cid)
                lid = qs.get("lecture") or ""
                if lid:
                    prog = {lid: prog[lid]} if lid in prog else {}
                return self._json({"course": cid, "progress": prog})
            if p == "/api/annotations":
                cid = self._course_of(qs)
                if not cid or not app_ref.registry.load(cid):
                    return self._json({"error": "课程不存在"}, 404)
                data = app_ref.registry.annotations(cid)
                lid = qs.get("lecture") or ""
                page = qs.get("page") or ""
                if lid:
                    bucket = data.get(lid) or {}
                    data = {lid: ({page: bucket.get(page) or []} if page else bucket)}
                return self._json({"course": cid, "annotations": data})
            if p == "/api/code/tree":
                cid = self._course_of(qs)
                if not cid or not app_ref.registry.load(cid):
                    return self._json({"error": "课程不存在"}, 404)
                try:
                    return self._json(app_ref.registry.code_tree(cid, qs.get("path") or ""))
                except Exception as e:
                    return self._json({"error": str(e)}, 404)
            if p == "/api/code/file":
                cid = self._course_of(qs)
                rel = qs.get("path") or ""
                if not cid:
                    return self._json({"error": "缺少课程"}, 400)
                try:
                    full, root = app_ref.registry.code_path(cid, rel)
                except Exception as e:
                    return self._json({"error": str(e)}, 404)
                if os.path.getsize(full) > 2 * 1024 * 1024:
                    return self._json({"error": "文件太大，无法在编辑器打开"}, 413)
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError as e:
                    return self._json({"error": str(e)}, 404)
                return self._json({
                    "course": cid, "path": rel, "root": os.path.basename(root),
                    "lang": courses.lang_of(rel), "size": os.path.getsize(full),
                    "text": text,
                })
            if p == "/api/code/find":
                cid = self._course_of(qs)
                needle = (qs.get("q") or "").strip()
                if not cid:
                    return self._json({"error": "缺少课程"}, 400)
                if not needle:
                    return self._json({"results": []})
                rows = app_ref.index.code(cid, needle, limit=20)
                out = []
                for row in rows:
                    text = row.get("text") or ""
                    line = 1
                    pos = text.lower().find(needle.lower())
                    if pos > 0:
                        line = text.count("\n", 0, pos) + 1
                    out.append({"path": row["path"], "lang": row.get("lang") or "",
                                "line": line})
                return self._json({"results": out})
            if p == "/api/config":
                c = dict(app_ref.config)
                c["api_key"] = cfgmod.MASK + c["api_key"][-4:] if c.get("api_key") else ""
                return self._json(c)
            if p == "/api/update/status":
                return self._json(app_ref.update_job.snapshot())
            if p == "/api/export/status":
                return self._json(app_ref.export_job.snapshot())
            if p == "/api/wizard":
                return self._json(app_ref.wizard_payload())
            if p == "/api/wizard/status":
                return self._json(app_ref.install_job.snapshot())
            if p.startswith("/api/doc/"):
                return self._doc_get(p)
            return self._file(p)

        def do_DELETE(self):
            p = urlparse(self.path).path
            try:
                if p == "/api/annotations":
                    b = self._read_json()
                    cid = self._course_of(b)
                    lid = str(b.get("lecture") or "").strip()
                    try:
                        page = int(b.get("page") or 0)
                    except (TypeError, ValueError):
                        page = 0
                    if not cid or not app_ref.registry.load(cid) or not lid or page < 1:
                        return self._json({"error": "缺少课程、资料或页码"}, 400)
                    ok = app_ref.registry.delete_annotation(
                        cid, lid, page, str(b.get("id") or ""))
                    return self._json({"ok": ok})
                return self._json({"error": "unknown"}, 404)
            except Exception as e:
                traceback.print_exc()
                return self._json({"error": str(e)}, 500)

        def do_POST(self):
            p = urlparse(self.path).path
            # 流式必须先走：它自己发响应头，不能落进下面那个 except -> _json(500) 里
            if p == "/api/chat/stream":
                return self._chat_stream()
            if p == "/__demo":
                return self._serve_demo()
            if p == "/__demo/stdin":
                return self._serve_demo_stdin()
            if p == "/__demo/winsize":
                return self._serve_demo_winsize()
            try:
                if p == "/api/doc/upload":
                    return self._upload_document()
                if p == "/api/doc/delete":
                    b = self._read_json()
                    doc_id = str(b.get("id") or "")
                    if not docs.DOC_ID_RE.match(doc_id):
                        return self._json({"error": "资料不存在"}, 404)
                    entry = app_ref.store.lectures.get(doc_id) or {}
                    if not entry or entry.get("kind") != "doc":
                        return self._json({"error": "资料不存在"}, 404)
                    cid = entry.get("course_id") or ""
                    if not cid:
                        cid = app_ref.registry.course_of_item(doc_id) or ""
                    ok = app_ref.store.docs.delete(doc_id)
                    if ok:
                        # 资料都删了，基于它的对话（含资料原文片段）也没有保留意义。
                        # 进度和标注也要一起清，否则首页还会显示「上次读到…」，
                        # 重新导入同 id 的资料时旧标注也会串上来。
                        hist.clear(doc_id)
                        if cid:
                            app_ref.registry.delete_item_state(cid, doc_id)
                    # 即使没有删干净也重扫：把还能读到的旧目录重新登记，别让内存
                    # 里留着一个已经打不开的路径。
                    app_ref.store.refresh_docs()
                    if cid:
                        app_ref.index.rebuild(cid, app_ref.store)
                    if not ok:
                        return self._json(
                            {"error": "有文件正在使用或权限不足，资料没有删干净，请稍后重试"},
                            500)
                    return self._json({"ok": True})
                if p == "/api/course/scan":
                    b = self._read_json()
                    source = str(b.get("source") or "").strip()
                    if not source:
                        return self._json({"error": "请填写本地路径或 Git 地址"}, 400)
                    if re.match(r"^(https?://|git@|ssh://)", source):
                        # 远端不在预览阶段克隆：可能很大，也可能要凭据
                        return self._json({
                            "path": source, "remote": True, "is_git": True,
                            "lectures": [], "decks": [], "code_roots": [],
                        })
                    try:
                        info = courses.scan_repo(source)
                    except Exception as e:
                        return self._json({"error": str(e)}, 400)
                    info["remote"] = False
                    return self._json(info)
                if p == "/api/course/import":
                    b = self._read_json()
                    if app_ref.course_job.running:
                        return self._json({"error": "已有课程任务在进行，请稍候"}, 409)
                    mode = str(b.get("type") or "repo")
                    if mode == "files":
                        paths = b.get("paths") or []
                        if isinstance(paths, str):
                            paths = [paths]
                        if b.get("path"):
                            paths.append(b["path"])
                        paths = [str(x) for x in paths if str(x or "").strip()]
                        if not paths:
                            return self._json({"error": "没有选择文件"}, 400)
                        cid = str(b.get("course") or "").strip()
                        if cid and not app_ref.registry.load(cid):
                            cid = ""
                        if not cid:
                            title = str(b.get("title") or "").strip()
                            cid = (app_ref.registry.create(title, kind="files")["id"]
                                   if title else app_ref.target_course())
                        def work(log, cid=cid, paths=paths):
                            added = 0
                            for path in paths:
                                log(f"导入 {os.path.basename(path)} ...")
                                try:
                                    app_ref.store.docs.add(
                                        path, None, log=log, course_id=cid,
                                        out_root=app_ref.registry.items_root(cid))
                                    added += 1
                                except Exception as e:
                                    log(f"  失败：{e}")
                            app_ref.store.reload()
                            app_ref.index.rebuild(cid, app_ref.store, log)
                            log(f"完成：导入 {added}/{len(paths)} 个文件")
                            return {"course_id": cid, "documents": added}
                        started = app_ref.course_job.start(work, name="course-files")
                        return self._json({"started": started, "course_id": cid})
                    source = str(b.get("source") or "").strip()
                    if not source:
                        return self._json({"error": "请填写本地路径或 Git 地址"}, 400)
                    started = app_ref.start_repo_import(
                        source, str(b.get("title") or ""),
                        bool(b.get("with_decks", True)))
                    if not started:
                        return self._json({"error": "已有课程任务在进行"}, 409)
                    return self._json({"started": True})
                if p == "/api/course/update":
                    b = self._read_json()
                    cid = str(b.get("course") or "").strip()
                    if not app_ref.registry.load(cid):
                        return self._json({"error": "课程不存在"}, 404)
                    if app_ref.course_job.running:
                        return self._json({"error": "已有课程任务在进行，请稍候"}, 409)
                    return self._json({"started": app_ref.start_course_update(cid)})
                if p == "/api/course/delete":
                    b = self._read_json()
                    cid = str(b.get("course") or "").strip()
                    if cid == courses.ICS_ID:
                        return self._json({"error": "ICS 课程不能删除"}, 400)
                    if app_ref.course_job.running:
                        return self._json({"error": "有课程任务正在进行，无法删除"}, 409)
                    try:
                        return self._json({"ok": True,
                                           **app_ref.start_course_delete(cid)})
                    except Exception as e:
                        return self._json({"error": str(e)}, 400)
                if p == "/api/progress":
                    b = self._read_json()
                    cid = self._course_of(b)
                    lid = str(b.get("lecture") or "").strip()
                    if not cid or not app_ref.registry.load(cid) or not lid:
                        return self._json({"error": "缺少课程或资料"}, 400)
                    # 阅读器可能比删除慢半拍：已经删掉的资料不能再写回进度，
                    # 否则首页的「上次读到」会一直挂着一份不存在的资料。
                    item = app_ref.store.lectures.get(lid) or {}
                    if (item.get("course_id") or "") != cid:
                        return self._json({"error": "资料不存在"}, 404)
                    rec = app_ref.registry.set_progress(cid, lid, b)
                    return self._json({"ok": True, "course": cid, "progress": rec})
                if p == "/api/annotations":
                    b = self._read_json()
                    cid = self._course_of(b)
                    lid = str(b.get("lecture") or "").strip()
                    try:
                        page = int(b.get("page") or 0)
                    except (TypeError, ValueError):
                        page = 0
                    anno = b.get("annotation")
                    if not cid or not app_ref.registry.load(cid) or not lid \
                            or page < 1 or not isinstance(anno, dict):
                        return self._json({"error": "缺少课程、资料或页码"}, 400)
                    item = app_ref.store.lectures.get(lid) or {}
                    if (item.get("course_id") or "") != cid:
                        return self._json({"error": "资料不存在"}, 404)
                    rec = app_ref.registry.add_annotation(cid, lid, page, anno)
                    return self._json({"ok": True, "annotation": rec})
                if p == "/api/annotations/clear":
                    b = self._read_json()
                    cid = self._course_of(b)
                    lid = str(b.get("lecture") or "").strip()
                    if not cid or not app_ref.registry.load(cid) or not lid:
                        return self._json({"error": "缺少课程或资料"}, 400)
                    item = app_ref.store.lectures.get(lid) or {}
                    if (item.get("course_id") or "") != cid:
                        return self._json({"error": "资料不存在"}, 404)
                    page = b.get("page")
                    app_ref.registry.clear_annotations(
                        cid, lid, int(page) if page else None)
                    return self._json({"ok": True})
                if p == "/api/chat":
                    b = self._read_json()
                    lid = b.get("lecture")
                    pid = b.get("page")
                    q = (b.get("question") or "").strip()
                    if not q:
                        return self._json({"error": "问题不能为空"}, 400)
                    course = str(b.get("course") or "").strip()
                    if not course and lid:
                        course = app_ref.registry.course_of_item(lid) or ""
                    code = b.get("code") if isinstance(b.get("code"), dict) else None
                    ans = agent.ask(app_ref.config, app_ref.store, lid, pid, q,
                                    b.get("history"), course_id=course or None,
                                    code=code, route=agent.route_question(q, code))
                    return self._json({"answer": ans})
                if p.startswith("/api/history/"):
                    b = self._read_json()
                    lid = b.get("lecture")
                    if not lid:
                        return self._json({"error": "缺少 lecture"}, 400)
                    cid = b.get("id") or b.get("cid")
                    if p == "/api/history/list":
                        return self._json({"conversations": hist.list_conversations(lid)})
                    if p == "/api/history/new":
                        return self._json({"id": hist.new(lid, cid)})
                    if p == "/api/history/get":
                        return self._json({"conversation": hist.get(lid, cid)})
                    if p == "/api/history/delete":
                        return self._json({"ok": hist.delete(lid, cid)})
                if p in ("/api/test_api", "/api/models"):
                    probe = self._probe_config()
                    if p == "/api/test_api":
                        return self._json(agent.test_connection(probe))
                    return self._json(agent.list_models(probe))
                if p == "/api/search":
                    b = self._read_json()
                    q = (b.get("query") or "").strip()
                    if not q:
                        return self._json({"results": []})
                    lid = b.get("lecture") or None
                    scope = b.get("scope") or "lecture"
                    course = str(b.get("course") or "").strip()
                    lec = app_ref.store.lectures.get(lid)
                    if not course and lec:
                        course = lec.get("course_id") or ""
                    if not course:
                        course = None
                    # 不跨课程：范围要么是这一份资料/这一讲，要么是整门课。
                    # 旧前端发的 scope="all" 也映射成「本课程」。
                    if scope in ("course", "all"):
                        lid = None
                    return self._json({"results": app_ref.store.search(
                        q, lid=lid, k=30, course_id=course)})
                if p == "/api/export_pdf":
                    b = self._read_json()
                    lid = b.get("lecture")
                    dest = (b.get("dest") or "").strip()
                    if not lid:
                        return self._json({"error": "缺少 lecture"}, 400)
                    if dest and not dest.lower().endswith(".pdf"):
                        dest += ".pdf"
                    if not os.path.isdir(os.path.join(cfgmod.build_root(), lid)):
                        return self._json({"error": "这一讲还没渲染出课件，先点右上角「更新」"}, 400)
                    if not app_ref.start_export(lid, dest):     # dest 为空则由后端兜底
                        return self._json({"error": "已有导出在进行"}, 400)
                    return self._json({"started": True})
                if p == "/api/reveal":
                    b = self._read_json()
                    path = (b.get("path") or "").strip()
                    if path and os.path.exists(path):
                        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)],
                                         creationflags=sync.CREATE_NO_WINDOW)
                    return self._json({"ok": True})
                if p == "/api/wizard/install":
                    b = self._read_json()
                    key = b.get("key")
                    if not key:
                        return self._json({"error": "缺少 key"}, 400)
                    started = app_ref.start_install(key, b.get("dir"))
                    return self._json({"started": started})
                if p == "/api/config":
                    b = self._read_json()
                    allowed = {k: v for k, v in b.items() if k in cfgmod.DEFAULTS}
                    # api_key 若是界面上那串掩码，save_config 会丢掉它 —— 保存掩码会把
                    # 真 key 覆盖成「••••1234」，之后每个请求都发不出去
                    app_ref.config = cfgmod.save_config(allowed)
                    return self._json({"ok": True, "config": app_ref.course_payload()})
                if p == "/api/update":
                    started = app_ref.start_update()
                    return self._json({"started": started})
                return self._json({"error": "unknown"}, 404)
            except Exception as e:
                traceback.print_exc()
                return self._json({"error": str(e)}, 500)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd


def find_port(preferred: int):
    import socket
    for port in range(preferred, preferred + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")
