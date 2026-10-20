"""课程注册、FTS5、上下文预算与文本层的单元测试。

全部跑在 tempfile 建的空 DATA_DIR 里；不会碰用户自己的 courses/、documents/
或渲染产物。运行：

    python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent       # noqa: E402
import config      # noqa: E402
import courses     # noqa: E402
import docs        # noqa: E402
import search      # noqa: E402

FILLER = "本文讨论页表、缓存与地址翻译的细节。" * 120


def _make_pdf(path, text="Hello Layer"):
    """手写一个最小 PDF（Helvetica，一页），避免测试依赖报告库。"""
    content = ("BT /F1 24 Tf 20 50 Td (" + text + ") Tj ET").encode("ascii")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 260 120] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += ("%d 0 obj\n" % i).encode("ascii") + body + b"\nendobj\n"
    xref = len(out)
    out += ("xref\n0 %d\n" % (len(objs) + 1)).encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode("ascii")
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref)).encode("ascii")
    with open(path, "wb") as f:
        f.write(bytes(out))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="icsreader-test-")
        self._old_data = config.DATA_DIR
        config.DATA_DIR = self.tmp

    def tearDown(self):
        config.DATA_DIR = self._old_data
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_repo(self):
        repo = os.path.join(self.tmp, "repo")
        files = {
            "ICS-Labs/lab1/src/helper.c": "int helper(void) { return 1; }\n",
            "OS-Labs/lab2/main.c": "int main(void) { return 0; }\n",
            "ICS-PPT/lectures/1-intro/lecture.py": "print('intro')\n",
        }
        for rel, text in files.items():
            path = os.path.join(repo, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return repo

    def make_registry(self, repo=None):
        repo = repo or self.make_repo()
        reg = courses.CourseRegistry(
            {"repo": repo, "auto_update": False},
            build_root=os.path.join(self.tmp, "course"),
            legacy_docs_root=os.path.join(self.tmp, "documents"))
        reg.ensure()
        return reg


class RegistryTest(Base):
    def test_ics_course_and_code_roots(self):
        reg = self.make_registry()
        card = reg.load("ics")
        self.assertEqual(card["kind"], "ics")
        self.assertEqual(card["source"]["path"], os.path.join(self.tmp, "repo"))
        self.assertEqual(sorted(card["code_roots"]),
                         ["ICS-Labs", "ICS-PPT/lectures", "OS-Labs"])
        self.assertIn("ics", reg.course_ids())

    def test_legacy_docs_are_copied_not_moved(self):
        legacy = os.path.join(self.tmp, "documents", "0123456789ab")
        os.makedirs(os.path.join(legacy, "cache"))
        with open(os.path.join(legacy, "doc.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "0123456789ab", "title": "旧资料", "mode": "pdf",
                       "pages": [{"id": "p1", "number": 1, "title": "P1",
                                  "text": "hello"}]}, f, ensure_ascii=False)
        _make_pdf(os.path.join(legacy, "source.pdf"))
        with open(os.path.join(legacy, "cache", "page1.png"), "wb") as f:
            f.write(b"png")

        reg = self.make_registry()
        copied = os.path.join(courses.course_dir("mydocs"), "items", "0123456789ab")
        self.assertTrue(os.path.isfile(os.path.join(copied, "doc.json")))
        self.assertTrue(os.path.isfile(os.path.join(copied, "source.pdf")))
        self.assertFalse(os.path.isdir(os.path.join(copied, "cache")))
        with open(os.path.join(copied, "doc.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["course_id"], "mydocs")
        # 原件保留
        self.assertTrue(os.path.isfile(os.path.join(legacy, "source.pdf")))
        mydocs = reg.load("mydocs")
        self.assertEqual(mydocs["kind"], "files")

    def _legacy_doc(self):
        legacy = os.path.join(self.tmp, "documents", "0123456789ab")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "doc.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "0123456789ab", "title": "旧资料", "mode": "pdf",
                       "pages": [{"id": "p1", "number": 1, "title": "P1",
                                  "text": "hello"}]}, f, ensure_ascii=False)
        _make_pdf(os.path.join(legacy, "source.pdf"))
        return legacy

    def test_delete_doc_removes_copy_original_and_state(self):
        legacy = self._legacy_doc()
        reg = self.make_registry()
        store = docs.DocStore(reg.doc_roots())
        did = "0123456789ab"
        copied = os.path.join(courses.course_dir("mydocs"), "items", did)
        self.assertTrue(os.path.isdir(copied))
        self.assertIn(did, store.lectures)

        reg.set_progress("mydocs", did, {"number": 3, "title": "P1", "total": 5})
        reg.add_annotation("mydocs", did, 1, {"kind": "highlight"})
        self.assertTrue(store.delete(did))
        reg.delete_item_state("mydocs", did)

        # 副本和 documents/ 里的原件都得清掉，否则重扫时会「复活」。
        self.assertFalse(os.path.exists(copied))
        self.assertFalse(os.path.exists(legacy))
        store.reload()
        self.assertNotIn(did, store.lectures)
        self.assertNotIn(did, reg.progress("mydocs"))
        self.assertNotIn(did, reg.annotations("mydocs"))

    def test_delete_mydocs_course_takes_legacy_originals(self):
        legacy = self._legacy_doc()
        reg = self.make_registry()
        self.assertIsNotNone(reg.load("mydocs"))
        self.assertTrue(reg.delete("mydocs", doc_ids=["0123456789ab"]))
        self.assertFalse(os.path.isdir(courses.course_dir("mydocs")))
        self.assertFalse(os.path.exists(legacy))
        # 删除后重启也不能再长出一张空「我的资料」
        reg.ensure()
        self.assertIsNone(reg.load("mydocs"))

    def test_last_progress_ignores_missing_item(self):
        reg = self.make_registry()
        reg.set_progress("mydocs", "0123456789ab",
                         {"number": 3, "title": "已删除的资料", "total": 5})
        self.assertIsNone(reg.last_progress("mydocs"))

    def test_code_path_forms_and_traversal(self):
        reg = self.make_registry()
        for rel in ("ICS-Labs/lab1/src/helper.c",       # source 相对
                    "lab1/src/helper.c",                # 代码根内相对
                    "lectures/1-intro/lecture.py"):     # 旧索引的根目录名写法
            full, _root = reg.code_path("ics", rel)
            self.assertTrue(os.path.isfile(full), rel)
        with self.assertRaises(RuntimeError):
            reg.code_path("ics", "../config.py")
        with self.assertRaises(RuntimeError):
            reg.code_path("ics", "ICS-Labs/../../../config.py")

    def test_code_tree_lists_roots_and_children(self):
        reg = self.make_registry()
        tree = reg.code_tree("ics")
        paths = {e["path"] for e in tree["entries"]}
        self.assertIn("ICS-Labs", paths)
        self.assertIn("ICS-PPT/lectures", paths)
        sub = reg.code_tree("ics", "ICS-Labs")
        names = {e["name"] for e in sub["entries"]}
        self.assertIn("lab1", names)


class IndexTest(Base):
    def _store(self):
        return FakeStore({
            "L1": {
                "kind": "ics", "course_id": "ics", "title": "内存讲", "vid": "L1",
                "pages": [{"id": "p1", "number": 1, "title": "虚拟内存"},
                          {"id": "p2", "number": 2, "title": "页表"}],
                "page_text": {"p1": "虚拟内存 虚拟内存 虚拟内存 页表 TLB",
                              "p2": "虚拟内存 " + FILLER},
                "page_title": {"p1": "虚拟内存", "p2": "页表"},
                "tree": [],
            },
            "L2": {
                "kind": "ics", "course_id": "ics", "title": "调度讲", "vid": "L2",
                "pages": [{"id": "p3", "number": 1, "title": "调度"}],
                "page_text": {"p3": "进程调度 时间片 轮转"},
                "page_title": {"p3": "调度"},
                "tree": [],
            },
        })

    def test_schema_and_ranking(self):
        self.make_registry()
        idx = search.CourseIndex(FakeRegistry())
        store = self._store()
        self.assertTrue(idx.rebuild("ics", store))
        self.assertTrue(idx.up_to_date("ics"))

        short = idx.pages("ics", "内存", limit=10)
        self.assertTrue(short)
        self.assertEqual(short[0]["page"], "p1")
        # token 表要返回原文，不能把 token 汤当正文
        self.assertEqual(short[0]["text"], store.lectures["L1"]["page_text"]["p1"])

        long_q = idx.pages("ics", "虚拟内存", limit=10)
        self.assertTrue(long_q)
        self.assertEqual(long_q[0]["page"], "p1")
        self.assertFalse([x for x in long_q if x["lid"] == "L2"])

        near = idx.pages("ics", "虚拟 内存", limit=10)
        self.assertTrue(near)
        self.assertEqual(near[0]["page"], "p1")

    def test_code_index_paths_match_code_tree(self):
        reg = self.make_registry()
        idx = search.CourseIndex(reg)
        self.assertTrue(idx.rebuild("ics", self._store()))
        hits = idx.code("ics", "helper", limit=4)
        self.assertTrue(hits)
        paths = {h["path"] for h in hits}
        self.assertIn("ICS-Labs/lab1/src/helper.c", paths)
        for rel in paths:
            self.assertTrue(os.path.isfile(reg.code_path("ics", rel)[0]), rel)


class FakeStore:
    def __init__(self, lectures):
        self.lectures = lectures

    def lecture_ids(self):
        return list(self.lectures)


class FakeRegistry:
    def __init__(self, roots=()):
        self.roots = list(roots)

    def load(self, cid):
        return {"id": cid} if cid == "ics" else None

    def code_roots(self, cid):
        return self.roots

    def source_path(self, course):
        return ""


class BudgetRouteTest(unittest.TestCase):
    def test_budget_keeps_current_page_and_neighbors(self):
        big = "这一页有大量正文，包含公式 E=mc^2 和地址翻译的细节。" * 2000

        class Store:
            lectures = {"L": {
                "id": "L", "kind": "ics", "course_id": "c1", "title": "很长的一讲",
                "pages": [{"id": "a", "number": 1, "title": "A"},
                          {"id": "b", "number": 2, "title": "B"},
                          {"id": "c", "number": 3, "title": "C"}],
                "page_text": {"a": "上一页", "b": big, "c": "下一页"},
                "page_title": {"a": "A", "b": "B", "c": "C"},
                "tree": [],
            }}
            index = None

            def page_index(self, lid, pid):
                return {"a": 0, "b": 1, "c": 2}[pid]

            def course_map_text(self, **kw):
                return "课程地图 " + ("讲次结构 " * 400)

            def _outline_text(self, tree):
                return ["目录项"]

            def retrieve(self, *a, **kw):
                return []

        ctx = agent.build_context(Store(), "L", "b", "解释这一页", 4,
                                  course_id="c1", config={
                                      "context_budget": 16000,
                                      "local_page_chars": 6000,
                                      "neighbor_summary_chars": 600,
                                      "retrieve_chars": 1000,
                                      "code_context_chars": 4000})
        self.assertLessEqual(len(ctx), 16200)
        self.assertIn("学生正在看", ctx)
        self.assertIn("【上一页摘要】", ctx)

    def test_route_question(self):
        cases = [("这段代码的 header 在哪", None, "code"),
                 ("前面哪一讲讲了这个定理", None, "course"),
                 ("这个公式的中间步骤", None, "local"),
                 ("解释选中的代码", {"path": "a.c"}, "code")]
        for question, code, want in cases:
            self.assertEqual(agent.route_question(question, code), want, question)


class DocTest(Base):
    def test_markdown_pages_and_titles(self):
        path = os.path.join(self.tmp, "notes.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 总标题\n\n导语\n\n## 第一节\n\n内容一\n\n## 第二节\n\n内容二\n")
        pages, outline = docs._extract_md(path)
        self.assertEqual([p["title"] for p in pages], ["总标题", "第一节", "第二节"])
        self.assertIn("内容二", pages[-1]["text"])
        self.assertTrue(outline)

    def test_text_layer_from_pdf(self):
        doc_id = "0123456789ab"
        base = os.path.join(self.tmp, "documents", doc_id)
        os.makedirs(base)
        _make_pdf(os.path.join(base, "source.pdf"))
        with open(os.path.join(base, "doc.json"), "w", encoding="utf-8") as f:
            json.dump({"id": doc_id, "title": "Probe", "mode": "pdf",
                       "pages": [{"id": "p1", "number": 1, "title": "P1"}]},
                      f, ensure_ascii=False)

        store = docs.DocStore([os.path.join(self.tmp, "documents")])
        layer = store.text_layer(doc_id, 1)
        self.assertIsNotNone(layer)
        joined = "".join(s["text"] for line in layer["lines"] for s in line["spans"])
        self.assertIn("Hello", joined)
        first = layer["lines"][0]["spans"][0]
        for key in ("x", "y", "w", "h", "style"):
            self.assertIn(key, first)
        self.assertTrue(os.path.isfile(os.path.join(base, "textlayer", "p1.json")))


if __name__ == "__main__":
    unittest.main()
