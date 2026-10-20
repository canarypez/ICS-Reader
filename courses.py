"""课程注册表：把「ICS 一门课 + 一堆散装资料」升级成真正的多课程存储。

每门课一个目录：

    DATA_DIR/courses/<course_id>/
      course.json       元数据：标题、来源、代码根、自动更新间隔
      index.db          FTS5 索引（见 search.py）
      progress.json     读到哪份资料的哪一页
      annotations.json  高亮 / 框选
      items/<id>/       上传或从仓库导入的 PDF / PPTX / MD
      source/           Git URL 的浅克隆（本地路径只记录，不复制）
      render/<dir>/     lecturekit 渲染产物

ICS 课程保持原样：课件仍渲染在 DATA_DIR/course/，页面仍走 /lectures/...，
历史文件名也仍是老讲次 id。这样升级不会把用户已有的阅读记录弄丢。

旧 DATA_DIR/documents/<12hex>/ 在启动时**复制**到 courses/mydocs/items/，
原件保留作备份。复制失败的目录仍然原地登记、照常可读。
"""
import json
import os
import re
import shutil
import threading
import time
import uuid

import config as cfgmod

#: 课程 id 只允许小写字母、数字、下划线和短横线。ICS 与 mydocs 是内置 id。
COURSE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

SCHEMA = 1
ICS_ID = "ics"
MYDOCS_ID = "mydocs"
MYDOCS_TITLE = "我的资料"

#: 首页占用统计按 mtime 缓存，免得每次回首页都全盘遍历一个几 GB 的仓库。
_SIZE_CACHE = {}
_SIZE_TTL = 60.0
_SIZE_LOCK = threading.Lock()


def courses_root() -> str:
    return os.path.join(cfgmod.DATA_DIR, "courses")


def course_dir(cid: str) -> str:
    return os.path.join(courses_root(), cid)


def new_course_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> float:
    return time.time()


def _title_key(title: str) -> str:
    """课程名按「空白折叠 + 忽略大小写」比较，避免多打一个空格就多一门课。"""
    return " ".join(str(title or "").split()).casefold()


def _atomic_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def disk_size(path: str, cache=True) -> int:
    """目录占用。按 (路径, 顶层 mtime) 缓存 60 秒。

    仓库可能有几十万个文件，首页每次刷新都走一遍会明显卡顿；顶层 mtime 变了就
    重算，足够跟上「刚导入/刚删除」这种变化。
    """
    if not os.path.isdir(path):
        return 0
    try:
        key = (os.path.abspath(path), os.path.getmtime(path))
    except OSError:
        key = (os.path.abspath(path), 0)
    if cache:
        with _SIZE_LOCK:
            hit = _SIZE_CACHE.get(key[0])
            if hit and hit[0] == key[1] and _now() - hit[1] < _SIZE_TTL:
                return hit[2]
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    with _SIZE_LOCK:
        _SIZE_CACHE[key[0]] = (key[1], _now(), total)
    return total


def forget_size(path: str):
    """删掉目录后立刻让首页的占用统计失效，不用等 60 秒缓存过期。"""
    path = os.path.abspath(path)
    with _SIZE_LOCK:
        for key in [k for k in _SIZE_CACHE
                    if k == path or k.startswith(path + os.sep)]:
            _SIZE_CACHE.pop(key, None)


def remove_tree(path: str) -> bool:
    """删除目录并确认真的没了。

    Windows 上被杀毒、资源管理器预览或别的句柄占用时 rmtree 会失败；以前用
    ignore_errors=True 会让接口报成功、目录却还在。这里重试几次，并把只读位
    去掉，最后用 exists 给出真实结果。
    """
    if not path or not os.path.exists(path):
        return True

    def _retry(func, target, _exc):
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
        try:
            func(target)
        except OSError:
            pass

    for _ in range(4):
        try:
            shutil.rmtree(path, onexc=_retry)
        except OSError:
            pass
        if not os.path.exists(path):
            forget_size(path)
            return True
        time.sleep(0.12)
    forget_size(path)
    return not os.path.exists(path)


def _dir_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _safe_child(root: str, rel: str) -> str:
    """把相对路径钉在 root 里，挡 ../ 和盘符。"""
    rel = str(rel or "").replace("\\", "/").strip("/")
    full = os.path.normpath(os.path.join(root, rel))
    root = os.path.normpath(root)
    if full != root and not full.startswith(root + os.sep):
        raise RuntimeError("路径越界")
    return full


class CourseRegistry:
    def __init__(self, config: dict, build_root: str, legacy_docs_root: str):
        self.config = config
        self.build_root = build_root
        self.legacy_docs_root = legacy_docs_root
        self.root = courses_root()

    # ------------------------------------------------------------------ 初始化
    def ensure(self):
        os.makedirs(self.root, exist_ok=True)
        self._ensure_ics()
        self._ensure_mydocs()
        self.migrate_legacy_docs()

    def _write(self, cid, data):
        data["updated"] = _now()
        _atomic_json(os.path.join(course_dir(cid), "course.json"), data)
        return data

    def _ensure_ics(self):
        path = os.path.join(course_dir(ICS_ID), "course.json")
        data = read_json(path, {})
        repo = (self.config.get("repo") or "").strip()
        data.update({
            "schema": SCHEMA,
            "id": ICS_ID,
            "title": data.get("title") or "ICS 课程",
            "kind": "ics",
            "source": {"type": "local", "path": repo, "url": None},
            "auto_update": bool(self.config.get("auto_update", True)),
            "update_minutes": int(data.get("update_minutes") or 30),
        })
        data.setdefault("created", _now())
        if not data.get("code_roots"):
            data["code_roots"] = [p for p in ("ICS-Labs", "OS-Labs", "ICS-PPT/lectures")
                                  if os.path.isdir(os.path.join(repo, p))]
        self._write(ICS_ID, data)

    def _ensure_mydocs(self):
        path = os.path.join(course_dir(MYDOCS_ID), "course.json")
        data = read_json(path, {})
        if not data:
            # 只有真的存在旧资料或还没归类的上传资料时才建；全新安装、或者
            # 用户刚把「我的资料」删空时，不必凭空再长出一张空课程卡。
            if not self._has_legacy_docs() and not self._has_doc_items():
                return
            data = {
                "schema": SCHEMA, "id": MYDOCS_ID, "title": MYDOCS_TITLE,
                "kind": "files", "source": {"type": "local", "path": None, "url": None},
                "auto_update": False, "update_minutes": 0, "code_roots": [],
                "created": _now(),
            }
        data["kind"] = "files"
        data["title"] = data.get("title") or MYDOCS_TITLE
        self._write(MYDOCS_ID, data)

    def _has_legacy_docs(self) -> bool:
        if not os.path.isdir(self.legacy_docs_root):
            return False
        for name in os.listdir(self.legacy_docs_root):
            if re.fullmatch(r"[0-9a-f]{12}", name) and os.path.isfile(
                    os.path.join(self.legacy_docs_root, name, "doc.json")):
                return True
        return False

    def _has_doc_items(self) -> bool:
        """内置「我的资料」目录里是否还有真正存在的资料。

        只在 mydocs 自己的 items/ 里看：别的课程有自己的 course.json，
        不需要为它们再补一张空卡片；用户把「我的资料」整个删掉后，这里
        也应该安静地返回 False，而不是让它第二天又冒出来。
        """
        root = os.path.join(course_dir(MYDOCS_ID), "items")
        if not os.path.isdir(root):
            return False
        for name in os.listdir(root):
            if re.fullmatch(r"[0-9a-f]{12}", name) and os.path.isfile(
                    os.path.join(root, name, "doc.json")):
                return True
        return False

    def course_ids(self):
        return self._ids()

    def _ids(self):
        if not os.path.isdir(self.root):
            return []
        return sorted(n for n in os.listdir(self.root)
                      if COURSE_ID_RE.match(n) and os.path.isfile(
                          os.path.join(self.root, n, "course.json")))

    # ------------------------------------------------------------------ 旧资料迁移
    def migrate_legacy_docs(self):
        """复制旧 documents/ 到 courses/mydocs/items/，原件不动。

        复制成功后在**副本**的 doc.json 里补 course_id；复制失败就原地登记，
        DocStore 会把 legacy 目录当成额外 root 继续读。
        """
        if not self._has_legacy_docs():
            return
        dst_root = os.path.join(course_dir(MYDOCS_ID), "items")
        os.makedirs(dst_root, exist_ok=True)
        for name in sorted(os.listdir(self.legacy_docs_root)):
            if not re.fullmatch(r"[0-9a-f]{12}", name):
                continue
            src = os.path.join(self.legacy_docs_root, name)
            if not os.path.isfile(os.path.join(src, "doc.json")):
                continue
            dst = os.path.join(dst_root, name)
            if os.path.isdir(dst):
                continue
            try:
                os.makedirs(dst, exist_ok=True)
                for item in os.listdir(src):
                    if item == "cache":          # 页面 PNG 随时能重建
                        continue
                    s, d = os.path.join(src, item), os.path.join(dst, item)
                    if os.path.isdir(s):
                        shutil.copytree(s, d, dirs_exist_ok=True)
                    else:
                        shutil.copy2(s, d)
                meta = read_json(os.path.join(dst, "doc.json"), {})
                meta["course_id"] = MYDOCS_ID
                _atomic_json(os.path.join(dst, "doc.json"), meta)
            except Exception:
                # 半截副本会污染扫描（缺 doc.json、缺源文件），清掉，让 legacy 继续顶上
                shutil.rmtree(dst, ignore_errors=True)

    # ------------------------------------------------------------------ 课程读写
    def load(self, cid: str):
        if not COURSE_ID_RE.match(str(cid or "")):
            return None
        data = read_json(os.path.join(course_dir(cid), "course.json"), None)
        if not isinstance(data, dict):
            return None
        data.setdefault("id", cid)
        data.setdefault("kind", "files")
        data.setdefault("source", {})
        data.setdefault("code_roots", [])
        return data

    def save(self, cid: str, data: dict):
        data["schema"] = SCHEMA
        data["id"] = cid
        return self._write(cid, data)

    def create(self, title: str, kind: str = "files", source=None, cid: str = None):
        if cid and COURSE_ID_RE.match(cid):
            if os.path.isfile(os.path.join(course_dir(cid), "course.json")):
                return self.load(cid)
        else:
            # 同名课程就是同一门课：再上传一份资料 / 再导入一次仓库时接着用，
            # 不再生成一张看不出区别的新卡片。ICS 是内置课程，不参与合并。
            wanted = _title_key(title)
            if wanted:
                for existing in self._ids():
                    if existing == ICS_ID:
                        continue
                    course = self.load(existing)
                    if course and _title_key(course.get("title")) == wanted:
                        return course
            cid = new_course_id()
            while os.path.isdir(course_dir(cid)):
                cid = new_course_id()
        data = {
            "schema": SCHEMA, "id": cid,
            "title": (title or "未命名课程").strip()[:80],
            "kind": kind if kind in ("files", "repo", "ics") else "files",
            "source": source or {"type": "local", "path": None, "url": None},
            "auto_update": False, "update_minutes": 30,
            "code_roots": [], "created": _now(), "updated": _now(),
        }
        os.makedirs(os.path.join(course_dir(cid), "items"), exist_ok=True)
        if data["kind"] == "repo":
            os.makedirs(os.path.join(course_dir(cid), "render"), exist_ok=True)
        self._write(cid, data)
        return data

    def delete(self, cid: str, doc_ids=None) -> bool:
        if cid == ICS_ID or not COURSE_ID_RE.match(str(cid or "")):
            return False
        path = course_dir(cid)
        if not os.path.isdir(path):
            return False
        # 旧 documents/ 里的原件也要一起清掉。迁移是复制不是搬移，只删课程目录
        # 的话，下次启动扫描 legacy 根目录时这门资料课（尤其是 mydocs）会原样
        # 复活，用户会觉得「删了个寂寞」。
        ids = {str(x) for x in (doc_ids or [])
               if re.fullmatch(r"[0-9a-f]{12}", str(x))}
        root = self.items_root(cid)
        if os.path.isdir(root):
            ids.update(n for n in os.listdir(root)
                       if re.fullmatch(r"[0-9a-f]{12}", n))
        if cid == MYDOCS_ID and os.path.isdir(self.legacy_docs_root):
            # 迁移失败的资料只存在于 documents/，但它们在界面上仍归 mydocs。
            ids.update(n for n in os.listdir(self.legacy_docs_root)
                       if re.fullmatch(r"[0-9a-f]{12}", n))
        for did in ids:
            legacy = os.path.join(self.legacy_docs_root, did)
            if os.path.isdir(legacy) and not remove_tree(legacy):
                return False
        ok = remove_tree(path)
        forget_size(os.path.dirname(path))
        forget_size(path)
        return ok

    def render_root(self, cid: str) -> str:
        if cid == ICS_ID:
            return self.build_root
        return os.path.join(course_dir(cid), "render")

    def items_root(self, cid: str) -> str:
        return os.path.join(course_dir(cid), "items")

    def source_path(self, course: dict) -> str:
        src = course.get("source") or {}
        if src.get("type") == "local":
            return src.get("path") or ""
        if src.get("type") == "git":
            return os.path.join(course_dir(course["id"]), "source")
        return ""

    def doc_roots(self):
        """所有课程的 items 目录，加上迁移失败时仍在原地的 legacy 目录。"""
        roots = []
        for cid in self._ids():
            roots.append(self.items_root(cid))
        if os.path.isdir(self.legacy_docs_root):
            roots.append(self.legacy_docs_root)
        return roots

    # ------------------------------------------------------------------ items
    def _lecture_items(self, cid: str, root: str):
        out = []
        if not os.path.isdir(root):
            return out
        for name in sorted(os.listdir(root)):
            build = os.path.join(root, name)
            lj = os.path.join(build, "lecture.json")
            if not os.path.isfile(lj):
                continue
            data = read_json(lj, {}) or {}
            lec = data.get("lecture") or {}
            title = lec.get("title") or name
            if cid == ICS_ID:
                lid, base = name, f"/lectures/{name}/"
                vid = lec.get("id") or name
            else:
                lid, base = f"{cid}:{name}", f"/course/{cid}/lectures/{name}/"
                vid = f"{cid}:{lec.get('id') or name}"
            out.append({
                "id": lid, "lid": lid, "dir": name, "kind": "lecture",
                "course_id": cid, "title": title, "base": base,
                "vid": vid, "page_count": len(data.get("pages") or []),
            })
        return out

    def _doc_items(self, cid: str, root: str):
        out = []
        if not os.path.isdir(root):
            return out
        for name in sorted(os.listdir(root)):
            if not re.fullmatch(r"[0-9a-f]{12}", name):
                continue
            meta = read_json(os.path.join(root, name, "doc.json"), None)
            if not isinstance(meta, dict):
                continue
            pages = meta.get("pages") or []
            out.append({
                "id": name, "lid": name, "kind": "doc", "course_id": cid,
                "title": meta.get("title") or meta.get("name") or name,
                "name": meta.get("name") or "", "type": meta.get("type") or "pdf",
                "mode": meta.get("mode") or "pdf", "page_count": len(pages),
                "base": f"/docview.html?doc={name}",
                "vid": name, "added": meta.get("added") or 0,
            })
        return out

    def items(self, cid: str):
        course = self.load(cid)
        if not course:
            return []
        items = []
        if cid == ICS_ID:
            items.extend(self._lecture_items(cid, self.build_root))
        else:
            items.extend(self._lecture_items(cid, self.render_root(cid)))
        # 任何课程都可以再放资料，包括 ICS —— 上传按钮不再只对资料课开放。
        seen = set()
        roots = [self.items_root(cid)]
        if cid == MYDOCS_ID:
            # 迁移失败的旧资料还留在 documents/，它们也属于「我的资料」；
            # 不列出来的话首页会少数、最近阅读也会被当成过期记录。
            roots.append(self.legacy_docs_root)
        for root in roots:
            for it in self._doc_items(cid, root):
                if it["id"] not in seen:
                    seen.add(it["id"])
                    items.append(it)
        return items

    def item(self, cid: str, lid: str):
        for it in self.items(cid):
            if it["id"] == lid or it["lid"] == lid:
                return it
        return None

    def course_of_item(self, lid: str):
        """跨课程 API 的兜底：不传 course 时从 item id 反查。"""
        for cid in self._ids():
            if self.item(cid, lid):
                return cid
        return None

    # ------------------------------------------------------------------ 卡片数据
    def last_progress(self, cid: str, items=None):
        data = read_json(os.path.join(course_dir(cid), "progress.json"), {}) or {}
        # 资料删掉后 progress.json 里可能还留着它的页码和标题。首页不能继续
        # 拿一条指向不存在资料（或已经换了内容的同名 item）的记录当「上次读到」。
        items = self.items(cid) if items is None else items
        valid = {it["id"] for it in items}
        best = None
        for lid, rec in data.items():
            if lid not in valid or not isinstance(rec, dict):
                continue
            if not best or (rec.get("updated") or 0) > (best.get("updated") or 0):
                best = dict(rec, lecture=lid)
        if best and not best.get("title"):
            item = next((it for it in items if it["id"] == best["lecture"]), {})
            best["title"] = item.get("title") or ""
        return best

    def card(self, cid: str):
        course = self.load(cid)
        if not course:
            return None
        items = self.items(cid)
        pages = sum(int(it.get("page_count") or 0) for it in items)
        size = disk_size(course_dir(cid))
        src = course.get("source") or {}
        source_size = 0
        if src.get("type") == "local" and src.get("path") and \
                os.path.normpath(src["path"]) != os.path.normpath(course_dir(cid)):
            source_size = disk_size(src["path"])
        return {
            "id": cid, "title": course.get("title") or cid,
            "kind": course.get("kind") or "files",
            "source": src, "auto_update": bool(course.get("auto_update")),
            "update_minutes": course.get("update_minutes") or 0,
            "code_roots": course.get("code_roots") or [],
            "item_count": len(items), "page_count": pages,
            "size": size, "source_size": source_size,
            "last": self.last_progress(cid, items),
            "updated": course.get("updated") or 0,
            "created": course.get("created") or 0,
            "deletable": cid != ICS_ID,
        }

    def cards(self):
        out = [c for c in (self.card(cid) for cid in self._ids()) if c]
        out.sort(key=lambda c: (0 if c["id"] == ICS_ID else 1,
                                -((c.get("last") or {}).get("updated") or 0),
                                c.get("title") or ""))
        return out

    # ------------------------------------------------------------------ 进度
    def progress(self, cid: str) -> dict:
        return read_json(os.path.join(course_dir(cid), "progress.json"), {}) or {}

    def set_progress(self, cid: str, lid: str, rec: dict):
        data = self.progress(cid)
        rec = {
            "page": rec.get("page"), "number": rec.get("number"),
            "index": rec.get("index"), "title": (rec.get("title") or "")[:120],
            "total": rec.get("total") or 0, "updated": _now(),
        }
        data[lid] = rec
        _atomic_json(os.path.join(course_dir(cid), "progress.json"), data)
        return rec

    # ------------------------------------------------------------------ 标注
    def annotations(self, cid: str) -> dict:
        return read_json(os.path.join(course_dir(cid), "annotations.json"), {}) or {}

    def add_annotation(self, cid: str, lid: str, page: int, anno: dict):
        data = self.annotations(cid)
        bucket = data.setdefault(lid, {})
        lst = bucket.setdefault(str(int(page)), [])
        anno = dict(anno)
        anno["id"] = anno.get("id") or uuid.uuid4().hex[:12]
        anno["page"] = int(page)
        anno["created"] = _now()
        lst.append(anno)
        _atomic_json(os.path.join(course_dir(cid), "annotations.json"), data)
        return anno

    def delete_annotation(self, cid: str, lid: str, page: int, aid: str):
        data = self.annotations(cid)
        bucket = data.get(lid) or {}
        lst = bucket.get(str(int(page))) or []
        keep = [a for a in lst if a.get("id") != aid]
        changed = len(keep) != len(lst)
        if keep:
            bucket[str(int(page))] = keep
        else:
            bucket.pop(str(int(page)), None)
        if not bucket:
            data.pop(lid, None)
        else:
            data[lid] = bucket
        if changed:
            _atomic_json(os.path.join(course_dir(cid), "annotations.json"), data)
        return changed

    def delete_item_state(self, cid: str, lid: str):
        """资料本体删掉后，把它的阅读进度和标注也从课程状态里摘干净。"""
        if not cid or not lid:
            return
        path = os.path.join(course_dir(cid), "progress.json")
        data = read_json(path, {}) or {}
        if lid in data:
            data.pop(lid, None)
            _atomic_json(path, data)
        ann_path = os.path.join(course_dir(cid), "annotations.json")
        ann = read_json(ann_path, {}) or {}
        if lid in ann:
            ann.pop(lid, None)
            _atomic_json(ann_path, ann)
        forget_size(course_dir(cid))

    def clear_annotations(self, cid: str, lid: str, page=None):
        data = self.annotations(cid)
        if page is None:
            data.pop(lid, None)
        else:
            bucket = data.get(lid) or {}
            bucket.pop(str(int(page)), None)
            if bucket:
                data[lid] = bucket
            else:
                data.pop(lid, None)
        _atomic_json(os.path.join(course_dir(cid), "annotations.json"), data)

    # ------------------------------------------------------------------ 代码根
    def code_roots(self, cid: str):
        course = self.load(cid)
        if not course:
            return []
        base = self.source_path(course)
        out = []
        for rel in course.get("code_roots") or []:
            path = rel if os.path.isabs(rel) else os.path.join(base, rel)
            path = os.path.normpath(path)
            if os.path.isdir(path):
                out.append(path)
        return out

    def code_path(self, cid: str, rel: str):
        """代码文件路径：先按代码根拼接，再要求落回某个根里。"""
        rel = str(rel or "").replace("\\", "/").strip("/")
        if not rel:
            raise RuntimeError("缺少文件路径")
        for root in self.code_roots(cid):
            full = os.path.normpath(os.path.join(root, rel))
            if full.startswith(os.path.normpath(root) + os.sep) and os.path.isfile(full):
                return full, root
        # 允许用户直接传「根目录名/文件」这种相对 source 的路径
        course = self.load(cid) or {}
        base = self.source_path(course)
        if base:
            for root in self.code_roots(cid):
                try:
                    prefix = os.path.relpath(root, base).replace("\\", "/")
                except ValueError:
                    continue
                if rel.startswith(prefix + "/"):
                    cand = os.path.join(base, rel)
                    if os.path.isfile(cand):
                        return os.path.normpath(cand), root
        # 旧索引里代码路径只带根目录名（如 lectures/1-intro/lecture.py），
        # 不带 source 相对前缀。这里兜底匹配一次，免得这些路径打不开。
        for root in self.code_roots(cid):
            name = os.path.basename(root)
            if rel == name or rel.startswith(name + "/"):
                cand = os.path.normpath(os.path.join(root, rel[len(name):].strip("/")))
                if os.path.isfile(cand):
                    return cand, root
        raise RuntimeError("找不到这个代码文件")

    def code_tree(self, cid: str, rel: str = ""):
        roots = self.code_roots(cid)
        if not roots:
            return {"roots": [], "path": "", "entries": []}
        rel = str(rel or "").replace("\\", "/").strip("/")
        if not rel:
            entries = []
            for r in roots:
                base = (self.load(cid) or {}).get("source") or {}
                try:
                    label = os.path.relpath(r, self.source_path(self.load(cid) or {}))
                except Exception:
                    label = os.path.basename(r)
                entries.append({"name": label.replace("\\", "/"), "path": label.replace("\\", "/"),
                                "type": "dir", "root": True})
            return {"roots": [os.path.basename(r) for r in roots],
                    "path": "", "entries": entries}
        # 顶层条目给的是「相对源码根」的路径（如 ICS-Labs/Lab1）。这里既接受代码
        # 根内的相对路径（Lab1），也接受带根名前缀的写法（ICS-Labs/Lab1）。
        base = self.source_path(self.load(cid) or {})
        target = None
        for r in roots:
            try:
                label = os.path.relpath(r, base).replace("\\", "/")
            except ValueError:
                label = os.path.basename(r)
            inner = rel
            if rel == label:
                inner = ""
            elif rel.startswith(label + "/"):
                inner = rel[len(label) + 1:]
            cand = os.path.normpath(os.path.join(r, inner))
            root_n = os.path.normpath(r)
            if (cand == root_n or cand.startswith(root_n + os.sep)) and os.path.isdir(cand):
                target = cand
                break
        if target is None:
            raise RuntimeError("找不到这个目录")
        entries = []
        try:
            names = sorted(os.listdir(target), key=lambda n: (not os.path.isdir(
                os.path.join(target, n)), n.lower()))
        except OSError as e:
            raise RuntimeError(f"读不了目录：{e}")
        for name in names:
            if name in (".git", "node_modules", "__pycache__", "build", "dist", ".venv"):
                continue
            full = os.path.join(target, name)
            is_dir = os.path.isdir(full)
            if not is_dir and not _is_code_file(name):
                continue
            entries.append({
                "name": name, "path": (rel + "/" + name).strip("/"),
                "type": "dir" if is_dir else "file",
                "size": 0 if is_dir else os.path.getsize(full),
            })
        return {"roots": [os.path.basename(r) for r in roots], "path": rel,
                "entries": entries}


#: 代码浏览器里可读的扩展名。二进制一律不列出来。
CODE_EXTS = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".s", ".asm", ".py",
    ".js", ".ts", ".java", ".rs", ".go", ".rb", ".sh", ".ps1", ".lua", ".sql",
    ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".cfg", ".ini",
    ".html", ".css", ".xml", ".mk", ".cmake", "Makefile",
}


def _is_code_file(name: str) -> bool:
    low = name.lower()
    if low in ("makefile", "dockerfile", "license", "readme"):
        return True
    return os.path.splitext(low)[1] in CODE_EXTS


def lang_of(path: str) -> str:
    low = (path or "").lower()
    table = {
        ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
        ".hpp": "cpp", ".hh": "cpp", ".s": "asm", ".asm": "asm",
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".java": "java", ".rs": "rust", ".go": "go", ".sh": "shell",
        ".ps1": "powershell", ".md": "markdown", ".json": "json",
        ".html": "html", ".css": "css", ".xml": "xml", ".sql": "sql",
        ".yaml": "yaml", ".yml": "yaml", ".toml": "ini", ".ini": "ini",
    }
    return table.get(os.path.splitext(low)[1], "plaintext")


# ---------------------------------------------------------------------- 仓库扫描
#: 仓库里常见的讲义 / 代码目录名。只扫这些位置，避免把 node_modules 拖进来。
_LECTURE_PARENTS = ("lectures", "slides", "decks", "course/lectures", "ICS-PPT/lectures")
_DECK_DIRS = ("", "slides", "decks", "OS-PPT", "docs")
_CODE_DIRS = ("lab", "labs", "assignments", "src", "code", "exercises",
              "ICS-Labs", "OS-Labs", "labs-src")


def scan_repo(path: str):
    path = os.path.abspath(str(path or ""))
    if not os.path.isdir(path):
        raise RuntimeError(f"目录不存在：{path}")
    lectures = []
    seen = set()
    for parent in _LECTURE_PARENTS:
        base = os.path.join(path, parent)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            src = os.path.join(base, name)
            if name in seen or not os.path.isfile(os.path.join(src, "lecture.py")):
                continue
            seen.add(name)
            lectures.append({"name": name, "path": os.path.relpath(src, path),
                             "has_lecturepy": True})
    decks = []
    for rel in _DECK_DIRS:
        base = os.path.join(path, rel) if rel else path
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            full = os.path.join(base, name)
            if not os.path.isfile(full):
                continue
            if os.path.splitext(name)[1].lower() not in (".pdf", ".pptx", ".md"):
                continue
            if name.lower() in ("readme.md", "agents.md"):
                continue
            decks.append({"name": name, "path": os.path.relpath(full, path)})
    code_roots = []
    for name in _CODE_DIRS:
        full = os.path.join(path, name)
        if os.path.isdir(full):
            code_roots.append(name.replace("\\", "/"))
    return {
        "path": path, "exists": True,
        "is_git": os.path.isdir(os.path.join(path, ".git")),
        "lectures": lectures, "decks": decks, "code_roots": code_roots,
    }
