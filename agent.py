"""课程知识库 + LLM 客户端。

把每讲渲染出的 lecture.json（大纲树 + 每页文本）读进内存，作为「整门课全貌」。
提问时构造上下文：当前页全文 + 前后页 + 当前讲大纲 + 整门课各讲标题 + 关键词检索
到的相关页，再交给 OpenAI 兼容接口的模型回答。
"""
import json
import os
import re
from urllib.parse import urlsplit

import httpx

#: 中文里出现频率极高、几乎每页都有的字。留着它们当检索信号，只会把噪声页顶上来。
_CJK_STOP = set(
    "的一是不了在人有我他她它这那哪就都而及与或和以及为以于会能可要也你您上中下个们"
    "到说时地得着自之其此该些么吗呢吧啊把被从对向只还很更最没无有不什怎为何如所以因"
    "表示例图见页第条点部分我们可以需要应该如果那么就还有另外一种两个三个"
)

#: 归一化时的页长下限。图表页 `_page_to_text` 抽出来往往只有个标题（二三十字），
#: 按真实长度开方会让它们仅凭「短」就压过真正讲内容的页（实测「量化误差」的第一名
#: 曾是一张只有标题的配图页）。给个下限把这层优势抹平，又不至于把长页翻上来。
_MIN_SCORE_LEN = 100

DEFAULT_SYSTEM = (
    "你是《计算机系统基础》(ICS / CS:APP 风格) 课程的助教，正在陪学生读课件。\n"
    "下面会给你一段课件上下文：包括学生「正在看的那一页」的全文、它前后的页、"
    "本讲的大纲、整门课各讲的结构，以及按问题关键词检索出的相关页。\n"
    "要求：\n"
    "1. 优先依据这些课件内容回答，能结合前后页和整门课的结构来解释，而不是只看当前一页；\n"
    "2. 学生可能刚接触这个主题，讲清楚直觉和来龙去脉，必要时给例子；\n"
    "3. 如果课件上下文不足以回答，明确说明「课件里没有直接讲」，再基于你自己的知识补充；\n"
    "4. 不要编造课件里不存在的页码、公式或结论；\n"
    "5. 数学公式一律用 LaTeX：行内用 $...$、独立成行用 $$...$$，方便前端渲染；\n"
    "6. 用中文回答，条理清晰、重点突出，篇幅适中。"
)

#: 用户自己上传资料（PDF / PPTX）时用的 system prompt。和 ICS 助教分开：资料不一定是
#: 计算机课，而且扫描件常常抽不出文字，得允许模型结合图片/前后页来推断。
DOC_SYSTEM = (
    "你是一个善于读讲义的学习助手，正在陪学生读他自己上传的课程资料（PDF 或幻灯片）。\n"
    "下面会给你一段资料上下文：学生「正在看的那一页」的全文、它前后的页、"
    "这份资料各页的标题和页码，以及按问题关键词在这份资料里检索到的相关页。\n"
    "要求：\n"
    "1. 优先依据这些资料回答，能结合前后页和整份资料的结构来解释，而不是只看当前一页；\n"
    "2. 学生可能刚接触这个主题，讲清楚直觉和来龙去脉，必要时给例子；\n"
    "3. 如果资料里不足以回答，明确说明「这份资料里没有直接讲」，再基于你自己的知识补充；\n"
    "4. 不要编造资料里不存在的页码、公式或结论；\n"
    "5. 如果当前页是可提取文字为空的扫描页/图片页，结合标题、前后页和你的知识来讲解，"
    "并说明这一点；\n"
    "6. 数学公式一律用 LaTeX：行内用 $...$、独立成行用 $$...$$，方便前端渲染；\n"
    "7. 用中文回答，条理清晰、重点突出，篇幅适中。"
)


# --------------------------------------------------------------------------- 文本抽取
def _flatten_strings(obj, out):
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten_strings(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _flatten_strings(v, out)


def page_to_text(page: dict) -> str:
    """把一页的 blocks 抽成纯文本，供检索和塞进上下文。"""
    lines = []
    title = page.get("title")
    if title:
        lines.append(title)
    for b in page.get("blocks", []):
        kind = b.get("kind")
        # row 是分栏容器，content 里还有子 blocks。以前整块跳过，双栏页的一半文字
        # 就这么从检索和上下文里消失了。
        if kind in ("cover", "image", "spacer"):
            continue
        strs = []
        if kind == "row" and isinstance(b.get("content"), (list, tuple)):
            nested = []
            for sub in b.get("content") or []:
                if isinstance(sub, dict):
                    _flatten_strings(sub.get("content"), nested)
                else:
                    _flatten_strings(sub, nested)
            strs = nested
        else:
            _flatten_strings(b.get("content"), strs)
        text = " ".join(s.strip() for s in strs if s and s.strip())
        if kind == "code" and len(text) > 1200:
            text = text[:1200]
        if text:
            lines.append(text)
    return "\n".join(lines)


# --------------------------------------------------------------------------- 课程库
class CourseStore:
    def __init__(self, build_root: str):
        self.build_root = build_root
        # id(源目录名) -> {dir, title, tree, pages, page_text}
        self.lectures = {}
        self.reload()

    def load_lecture(self, build_dir, lid, base=None, course_id="ics", vid=None,
                     kind="ics"):
        """读一个 lecture.json，返回扁平 `lectures` 里的一项。

        `lid` 是全局唯一的讲/资料 id。ICS 目录名保持不变（历史文件名、sessionStorage
        键都靠它）；仓库课程用 `<course_id>:<目录名>`，免得两个仓库里同名的
        `01-intro` 互相覆盖。
        """
        lj = os.path.join(build_dir, "lecture.json")
        if not os.path.isfile(lj):
            return None
        try:
            with open(lj, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        name = os.path.basename(os.path.normpath(build_dir))
        title = (data.get("lecture") or {}).get("title") or name
        pages = data.get("pages", [])
        page_text = {}
        page_number = {}
        page_title = {}
        for i, p in enumerate(pages):
            pid = p.get("id")
            page_text[pid] = page_to_text(p)
            # number 是「幻灯片上印出来的页码」，会因过渡页重复，只可用于展示；
            # 要跳转 / 当下标一律用页在 pages 里的位置（page_index）。
            page_number[pid] = p.get("number") or (i + 1)
            page_title[pid] = p.get("title") or ""
        return {
            "dir": lid,
            "kind": kind,
            "course_id": course_id,
            "base": base or f"/lectures/{lid}/",
            # 阅读器外壳自己的讲 id（如 ics-intro，与目录名 1-intro 不同）。
            # 前端跳页要靠它拼 sessionStorage 的键，所以得透出去。
            "vid": vid or (data.get("lecture") or {}).get("id") or name,
            "title": title,
            "tree": data.get("tree", []),
            "pages": pages,
            "page_text": page_text,
            "page_number": page_number,
            "page_title": page_title,
        }

    def reload(self):
        self.lectures = {}
        if not os.path.isdir(self.build_root):
            return
        for name in sorted(os.listdir(self.build_root)):
            entry = self.load_lecture(os.path.join(self.build_root, name), name,
                                      base=f"/lectures/{name}/", course_id="ics")
            if entry:
                self.lectures[name] = entry

    def lecture_ids(self):
        return list(self.lectures.keys())

    # ---- 大纲 / 结构文本 ----
    def _outline_text(self, nodes, depth=0):
        lines = []
        for node in nodes:
            if node.get("type") == "section":
                lines.append("  " * depth + "▸ " + (node.get("title") or ""))
                lines.extend(self._outline_text(node.get("children") or [], depth + 1))
            else:
                lines.append("  " * depth + "- " + (node.get("title") or ""))
        return lines

    def course_map_text(self, kind=None, course_id=None):
        """整门课 / 整份资料的结构。kind='ics' 只看课件，'doc' 只看上传的资料。

        资料和课件现在住在同一个 `lectures` 字典里，所以这里必须能筛。ICS 的编号
        也依赖筛选后的顺序 —— 不筛的话，用户传的资料会把「第几讲」全挤乱。
        `course_id` 再把范围收窄到某一门课。
        """
        lines = []
        n = 0
        for lid in self.lecture_ids():
            lec = self.lectures[lid]
            if kind and lec.get("kind") != kind:
                continue
            if course_id and lec.get("course_id") != course_id:
                continue
            if lec.get("kind") == "doc":
                lines.append(f"《{lec['title']}》（共 {len(lec['pages'])} 页）")
            else:
                n += 1
                lines.append(f"第{n}讲《{lec['title']}》")
            lines.extend(self._outline_text(lec["tree"], 1))
        if lines:
            return "\n".join(lines)
        if kind == "doc":
            return "（还没有上传资料）"
        return "（暂无课件，先点右上角「更新」渲染）"

    def outline_text(self, lid):
        lec = self.lectures.get(lid)
        if not lec:
            return ""
        return "\n".join(self._outline_text(lec["tree"]))

    # ---- 检索 ----
    def _tokenize(self, text):
        """英文/数字词 + 中文二元组 + 中文单字，权重依次递减。

        中文原来只切单字，于是「的/是/在」这类字把噪声页全顶上来。二元组（「量化」而非
        「量」「化」）精度高得多，再配一张停用词表压掉高频虚字。
        """
        text = text.lower()
        toks = re.findall(r"[a-z0-9_]+", text)                    # 英文/数字词
        for run in re.findall(r"[一-鿿]+", text):
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))  # 中文二元组
            toks.extend(ch for ch in run if ch not in _CJK_STOP)    # 中文单字
        return toks

    @staticmethod
    def _hit_weight(tok):
        if tok.isascii():
            return 3.0          # 英文词最具体
        return 2.0 if len(tok) == 2 else 1.0   # 二元组次之，单字最弱

    def _score(self, tokens, low_text, length):
        """命中权重之和，按页长开方归一化 —— 否则长页仅因为字多就赢。"""
        s = 0.0
        for t in tokens:
            if t in low_text:
                s += self._hit_weight(t)
        if s <= 0:
            return 0.0
        return s / (max(length, _MIN_SCORE_LEN) ** 0.5)

    def _rank(self, tokens, lid_filter=None, boost_lid=None, kind=None,
              course_id=None):
        """所有页的打分，降序。retrieve 与 search 共用，避免两套逻辑。"""
        out = []
        if lid_filter in self.lectures:
            lids = [lid_filter]
        else:
            lids = [lid for lid in self.lecture_ids()
                    if (not kind or self.lectures[lid].get("kind") == kind) and
                    (not course_id or self.lectures[lid].get("course_id") == course_id)]
        for lid in lids:
            lec = self.lectures[lid]
            boost = 1.25 if lid == boost_lid else 1.0   # 当前讲降权改为加权
            for pid, text in lec["page_text"].items():
                s = self._score(tokens, text.lower(), len(text))
                if s > 0:
                    out.append((s * boost, lid, pid))
        out.sort(key=lambda x: -x[0])
        return out

    def _fts_ranked(self, query, lid_filter=None, kind=None, course_id=None,
                    limit=60):
        """FTS5 候选，映射成 (score, lid, pid)。没有索引/没课程时返回空。

        FTS5 的 bm25 分数量级和旧的子串打分不可比，所以这里只取**排序名次**，
        归一成 1.0 → 0.0；混合比例交给 _combined_rank。
        """
        idx = getattr(self, "index", None)
        if idx is None:
            return []
        cid = course_id
        if not cid and lid_filter in self.lectures:
            cid = self.lectures[lid_filter].get("course_id")
        if not cid:
            return []
        try:
            cands = idx.pages(cid, query, limit=limit)
        except Exception:
            return []
        out = []
        n = max(1, len(cands))
        for i, c in enumerate(cands):
            lid, pid = c.get("lid"), c.get("page")
            lec = self.lectures.get(lid)
            if not lec:
                continue
            if lid_filter and lid != lid_filter:
                continue
            if kind and lec.get("kind") != kind:
                continue
            if course_id and lec.get("course_id") != course_id:
                continue
            out.append((1.0 - i / n, lid, pid))
        return out

    def _combined_rank(self, query, tokens, lid_filter=None, boost_lid=None,
                       kind=None, course_id=None):
        legacy = self._rank(tokens, lid_filter=lid_filter, boost_lid=boost_lid,
                            kind=kind, course_id=course_id)
        fts = self._fts_ranked(query, lid_filter=lid_filter, kind=kind,
                               course_id=course_id)
        if not fts:
            return legacy
        top = max((s for s, _l, _p in legacy), default=0.0) or 1.0
        merged = {}
        for s, lid, pid in legacy:
            merged[(lid, pid)] = 0.3 * (s / top)
        for s, lid, pid in fts:
            merged[(lid, pid)] = merged.get((lid, pid), 0.0) + 0.7 * s
        out = [(s, lid, pid) for (lid, pid), s in merged.items() if s > 0]
        out.sort(key=lambda x: -x[0])
        return out

    def retrieve(self, question, boost_lid=None, exclude=None, k=4,
                 lid_filter=None, kind=None, course_id=None):
        """给 LLM 用的相关页。exclude 是 (lid, pid)：当前页已单独进上下文，不必重复。

        `lid_filter` 把检索限制在一份资料/一讲里 —— 读用户上传的 PDF 时，绝不能把
        ICS 课件里的页混进「这份资料的相关页」。`kind` 则是反向的：读 ICS 时用它
        把用户资料排除在外，同时仍允许跨讲检索。
        """
        toks = self._tokenize(question)
        if not toks:
            return []
        ranked = self._combined_rank(question, toks, lid_filter=lid_filter,
                                     boost_lid=boost_lid, kind=kind,
                                     course_id=course_id)
        res = []
        for s, lid, pid in ranked:
            if exclude and (lid, pid) == tuple(exclude):
                continue
            res.append((s, lid, pid, self.lectures[lid]["title"]))
            if len(res) >= k:
                break
        return res

    def search(self, query, lid=None, k=20, kind=None, course_id=None):
        """用户直接搜关键词：返回可点击的结果（含可跳转的下标与展示用页码）。

        同一张逻辑页会因为动画分步而对应多张幻灯片（3-asm：162 张幻灯片只印 67 页），
        它们标题与页码都一样，列出来只是噪声 —— 按「讲 + 印刷页码 + 标题」去重，
        保留分数最高的那张（它也是跳转时最先落到的那张）。
        """
        toks = self._tokenize(query)
        if not toks:
            return []
        out = []
        seen = set()
        for s, L, pid in self._combined_rank(query, toks, lid_filter=lid,
                                             kind=kind, course_id=course_id):
            lec = self.lectures[L]
            num = lec["page_number"].get(pid)
            ptitle = lec["page_title"].get(pid, "")
            key = (L, num, ptitle)
            if key in seen:
                continue
            seen.add(key)
            text = lec["page_text"].get(pid, "")
            out.append({
                "lecture": L,
                "vid": lec["vid"],
                "kind": lec.get("kind") or "ics",
                "course_id": lec.get("course_id") or "ics",
                "lecture_title": lec["title"],
                "page": pid,
                "index": self.page_index(L, pid),          # 0-based，前端跳转用
                "page_number": num,                        # 印刷页码，展示用
                "page_title": ptitle,
                "snippet": self._snippet(text, toks, ptitle),
                "score": round(s, 4),
            })
            if len(out) >= k:
                break
        return out

    @staticmethod
    def _snippet(text, toks, title="", width=110):
        """截取第一处命中附近的片段，让结果列表能看出为什么命中。

        从标题之后开始找：page_to_text 把标题放在首行，不跳过的话每条摘要都只是把标题
        再念一遍。
        """
        body = text
        offset = 0
        if title and text.startswith(title):
            offset = len(title)
            body = text[offset:]
        low = body.lower()
        pos = -1
        for t in toks:
            p = low.find(t)
            if p >= 0 and (pos < 0 or p < pos):
                pos = p
        if pos < 0:
            return body[:width].replace("\n", " ").strip()
        start = max(0, pos - width // 3)
        return ("…" if start else "") + body[start:start + width].replace("\n", " ").strip() + "…"

    def page_by_id(self, lid, pid):
        lec = self.lectures.get(lid)
        if not lec:
            return None
        for p in lec["pages"]:
            if p.get("id") == pid:
                return p
        return None

    def page_index(self, lid, pid):
        lec = self.lectures.get(lid)
        if not lec:
            return None
        for i, p in enumerate(lec["pages"]):
            if p.get("id") == pid:
                return i
        return None


# --------------------------------------------------------------------------- 上下文
#: 三级路由的关键词。宁可把「函数怎么写」误判成代码联动，也别把「这次 lab 的模板
#: 在哪个文件」漏成局部答疑。
_CODE_HINTS = (
    "lab", "实验", "测试", "模板", "函数", "头文件", "实现", "调用", "源码", "代码",
    "starter", "test", "makefile", "编译", "链接", "系统调用", "汇编",
)
_COURSE_HINTS = (
    "哪一讲", "哪一页", "哪一次", "哪章", "哪份", "前面", "之前", "此前", "上次",
    "整个课程", "全课程", "所有讲", "复习", "总结", "贯穿", "在哪", "讲过", "联系",
    "关系", "对比", "比较",
)


def route_question(question: str, code=None) -> str:
    if code:
        return "code"
    q = str(question or "").lower()
    if any(h in q for h in _CODE_HINTS):
        return "code"
    if any(h in q for h in _COURSE_HINTS):
        return "course"
    return "local"


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:max(0, limit)].rstrip() + "\n…（已截断）"


def _trim_page(text: str, limit: int) -> str:
    """当前页太长时保留开头、公式/代码行和结尾 —— 中间通常是重复的正文。

    直接截前 N 字会把结论和底部注释全丢掉；保留「关键行」能救回大部分信息。
    """
    text = str(text or "")
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    key = [ln for ln in lines
           if re.search(r"[=≈≠≤≥∑∫√]|\\\\|\b(def|class|int|void|for|while|if)\b", ln)]
    head = text[: int(limit * 0.62)]
    tail = text[-int(limit * 0.12):]
    mid = "\n".join(key)
    budget = max(0, limit - len(head) - len(tail) - 40)
    if mid:
        mid = mid[:budget]
    return head + ("\n…\n【公式/代码行】\n" + mid if mid else "") + \
        "\n…\n" + tail + "\n（本页较长，已按优先级截断）"


def _page_summary(store, lid, pid, limit=600):
    """相邻页只给「标题 + 第一段 + 公式/代码行」，不再整页塞进去。"""
    lec = store.lectures.get(lid) or {}
    text = lec.get("page_text", {}).get(pid, "")
    title = lec.get("page_title", {}).get(pid, "")
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    first = paras[0] if paras else ""
    key = [ln for ln in text.split("\n")
           if re.search(r"[=≈≠≤≥∑∫√]|\\\\|\b(def|class|int|void|for|while)\b", ln)][:4]
    body = first
    if key:
        body += "\n" + "\n".join(key)
    return _clip((title + "\n" if title else "") + body, limit)


def _doc_notes(lec, pid):
    for p in lec.get("pages") or []:
        if p.get("id") == pid:
            return p.get("notes") or ""
    return ""


def _assemble(parts, budget):
    """按优先级装配：0=当前页，1=检索，2=相邻页。超预算依次丢 2、压 1、截 0。"""
    parts = [(int(p), str(t)) for p, t in parts if str(t or "").strip()]
    used = sum(len(t) for _p, t in parts)

    def drop_priority(pr):
        nonlocal used
        keep = []
        for p, t in parts:
            if p == pr:
                used -= len(t)
            else:
                keep.append((p, t))
        parts[:] = keep

    def shrink(pr, limit):
        nonlocal used
        for i, (p, t) in enumerate(parts):
            if p == pr and len(t) > limit:
                used -= len(t) - limit
                parts[i] = (p, _clip(t, limit))

    if used > budget:
        drop_priority(2)
    if used > budget:
        shrink(1, 500)
    if used > budget:
        shrink(1, 300)
    if used > budget:
        # 当前页是最后保留的：只截它，不删
        for i, (p, t) in enumerate(parts):
            if p == 0:
                allow = max(1200, budget - (used - len(t)))
                parts[i] = (p, _trim_page(t, allow))
                break
    return "\n\n".join(t for _p, t in parts)


def build_context(store: CourseStore, lid, pid, question, max_retrieve,
                  course_id=None, route=None, code=None, config=None):
    config = config or {}
    lec = store.lectures.get(lid)
    cid = course_id or (lec or {}).get("course_id") or "ics"
    route = route or route_question(question, code)
    budget = int(config.get("context_budget", 16000))
    p0_limit = int(config.get("local_page_chars", 6000))
    p2_limit = int(config.get("neighbor_summary_chars", 600))
    p1_limit = int(config.get("retrieve_chars", 1000))
    code_limit = int(config.get("code_context_chars", 4000))

    parts = []

    # ---- 课程地图 / 大纲：永远保留，但也要收着点
    if cid == "ics":
        parts.append((1, _clip("【整门课全貌】\n" + store.course_map_text(
            kind="ics", course_id=cid), 5000)))
    else:
        parts.append((1, _clip("【本课程结构】\n" + store.course_map_text(
            course_id=cid), 4000)))
    if lec:
        parts.append((1, _clip(
            f"【当前{'资料' if lec.get('kind') == 'doc' else '讲'}《{lec['title']}》"
            f"目录】\n" + ("\n".join(store._outline_text(lec["tree"]))[:2500]),
            3000)))

    # ---- P0 当前页
    idx = store.page_index(lid, pid) if lec else None
    if lec and idx is not None:
        pages = lec["pages"]
        cur = pages[idx]
        cur_num = cur.get("number") or (idx + 1)
        cur_title = cur.get("title") or ""
        body = lec["page_text"].get(pid, "")
        if not body.strip():
            body = ("（这一页没有可提取的文字，可能是扫描图片或纯图片幻灯片；"
                    "请结合标题、相邻页和常识讲解。）")
        notes = _doc_notes(lec, pid) if lec.get("kind") == "doc" else ""
        if notes:
            body += "\n\n【演讲者备注】\n" + notes
        label = "页" if lec.get("kind") == "doc" else "页"
        parts.append((0, _trim_page(
            f"【学生正在看：第 {cur_num} {label}《{cur_title}》— 全文】\n" + body,
            p0_limit)))

    # ---- P1 检索
    if lec:
        if route == "course":
            rel = store.retrieve(question, boost_lid=lid, exclude=(lid, pid),
                                 k=max(2, max_retrieve), course_id=cid,
                                 kind=lec.get("kind"))
        elif lec.get("kind") == "doc":
            rel = store.retrieve(question, boost_lid=lid, exclude=(lid, pid),
                                 k=min(2, max_retrieve), lid_filter=lid)
        else:
            rel = store.retrieve(question, boost_lid=lid, exclude=(lid, pid),
                                 k=min(2, max_retrieve), course_id=cid, kind="ics")
        if rel:
            lines = ["【按问题检索到的相关页】"]
            for _s, rlid, rpid, rtitle in rel:
                rlec = store.lectures.get(rlid) or {}
                t = rlec.get("page_text", {}).get(rpid, "")
                num = rlec.get("page_number", {}).get(rpid, "?")
                rt = rlec.get("page_title", {}).get(rpid) or ""
                lines.append(f"— 《{rtitle}》第 {num} 页《{rt}》：\n"
                             + _clip(t, p1_limit))
            parts.append((1, "\n".join(lines)))

    # ---- P2 相邻页（只给摘要）
    if lec and idx is not None:
        for off, label in ((-1, "上一页"), (1, "下一页")):
            j = idx + off
            if 0 <= j < len(lec["pages"]):
                p = lec["pages"][j]
                if p.get("id"):
                    parts.append((2, f"【{label}摘要】\n" + _page_summary(
                        store, lid, p["id"], p2_limit)))

    # ---- 代码联动
    if code and isinstance(code, dict):
        cpath = str(code.get("path") or "")
        ctext = str(code.get("text") or "")
        start, end = code.get("start"), code.get("end")
        lines = [f"【学生选中的代码：{cpath}" +
                 (f" 第 {start}-{end} 行" if start else "") + "】",
                 "```" + str(code.get("lang") or ""), _clip(ctext, code_limit), "```"]
        idxobj = getattr(store, "index", None)
        cid2 = course_id or cid
        if idxobj is not None and cid2:
            try:
                hits = idxobj.code(cid2, " ".join(re.findall(
                    r"[A-Za-z_][A-Za-z0-9_]{2,}", ctext)[:12]), limit=4)
            except Exception:
                hits = []
            if hits:
                lines.append("【课程仓库里可能相关的实现 / 测试】")
                for h in hits[:3]:
                    if h["path"] == cpath:
                        continue
                    lines.append(f"— {h['path']}：\n```\n"
                                 + _clip(h.get("text") or "", code_limit // 3) + "\n```")
        parts.append((0, _clip("\n".join(lines), code_limit + 800)))

    if lec and lec.get("kind") == "doc":
        parts.append((1, "【提示】这是学生自己上传的资料，不是 ICS 课件；"
                        "回答时不要把它当成 ICS 课程内容，也不要用 ICS 的讲次编号。"))

    return _assemble(parts, budget)


def _history_messages(history, budget):
    """把历史轮次裁进字符预算，返回能直接拼进 messages 的列表。

    从最旧的开始丢。只剩一轮还超预算时**截断那一轮的回答而不是整轮丢掉** —— 丢掉的话
    这轮提问就凭空消失了，模型会以为用户压根没问过。
    """
    turns = []
    for h in history or []:
        if not isinstance(h, dict):
            continue
        role = h.get("role")
        content = str(h.get("content") or "")
        if role in ("user", "assistant") and content:
            turns.append({"role": role, "content": content})

    used = sum(len(m["content"]) for m in turns)
    while turns and used > budget:
        if len(turns) == 1:
            turns[0]["content"] = (turns[0]["content"][:max(200, budget)]
                                   + "\n…（前文过长，已截断）")
            break
        used -= len(turns[0]["content"])
        turns.pop(0)
    return turns


def build_messages(config: dict, store: CourseStore, lid, pid, question, history=None,
                   course_id=None, code=None, route=None):
    """拼这次请求的消息列表。一次性问答与流式共用 —— 两处各写一份 prompt 迟早走偏。

    课件上下文只跟着「这一问」走，不进历史：它每轮都得按当时的页重新拼，存进历史既会
    反复撑大请求，也会在用户翻页之后拿旧页的全文去骗模型。
    """
    route = route or route_question(question, code)
    ctx = build_context(store, lid, pid, question,
                        int(config.get("max_retrieve", 4)),
                        course_id=course_id, route=route, code=code, config=config)
    lec = store.lectures.get(lid)
    if lec and lec.get("kind") == "doc":
        # 自定义 system prompt 是给 ICS 助教调口味用的；资料不一定是 ICS，别硬套。
        system = DOC_SYSTEM
    else:
        system = config.get("system_prompt") or DEFAULT_SYSTEM
    msgs = [{"role": "system", "content": system}]
    msgs.extend(_history_messages(history, int(config.get("history_budget", 12000))))
    msgs.append({"role": "user", "content": ctx + "\n\n【学生的问题】\n" + question})
    return msgs


# --------------------------------------------------------------------------- LLM 调用
#: 常见状态码对应的「人话」，直接拼进错误里 —— 用户看到 404 不知道是路径还是模型的问题
_HINT = {
    400: "请求被拒，多半是 model 名写错了",
    401: "key 不对或已失效",
    403: "key 没有权限，或被中转站拦了",
    404: "地址不对，base 可能多了或少了一段；也可能这个站没有该 model",
    429: "限流，或余额/额度不足",
}


def api_base_of(api_base: str) -> str:
    """把用户填的东西归一成 base：去掉尾斜杠，若粘的是完整 chat 地址就砍掉那一段。

    中转站的 base 写法五花八门：有的给到根（https://x.com/），有的带 /v1，有的干脆把
    整个 .../v1/chat/completions 粘进来。三种都认，别让用户去猜该填哪一段。
    """
    base = (api_base or "").strip().rstrip("/")
    tail = "/chat/completions"
    if base.endswith(tail):
        base = base[: -len(tail)].rstrip("/")
    return base


def _endpoint_urls(api_base: str, leaf: str) -> list:
    """在 base 后面接上 leaf（chat/completions 或 models）。

    裸域名先试 /v1 —— openai / ollama / 绝大多数中转站都挂在这个前缀下，deepseek
    两种都收；再退回不带 /v1 的，兜住少数自建网关。已经带路径的说明用户写全了，不动。
    """
    base = api_base_of(api_base)
    if not base:
        return []
    if urlsplit(base).path in ("", "/"):
        return [f"{base}/v1/{leaf}", f"{base}/{leaf}"]
    return [f"{base}/{leaf}"]


def chat_urls(api_base: str) -> list:
    return _endpoint_urls(api_base, "chat/completions")


def models_urls(api_base: str) -> list:
    return _endpoint_urls(api_base, "models")


def _headers(api_key: str) -> dict:
    return {
        # strip 一下：手改过 config.json 的 key 很容易带上行尾换行，那种值 httpx 会直接拒
        "Authorization": "Bearer " + (api_key or "").strip(),
        "Content-Type": "application/json",
        # 有些中转站前面挂着 Cloudflare，会拦 httpx 的默认 UA
        "User-Agent": "os-course-reader/0.2 (+local)",
    }


def _net_error(url: str, e: Exception) -> str:
    """网络层失败也要说清楚是哪种失败 —— ConnectError 和 ReadTimeout 的处理方式完全不同。"""
    if isinstance(e, httpx.ConnectTimeout):
        return f"连接 {url} 超时。检查网络；若这个站需要代理，确认代理已开。"
    if isinstance(e, httpx.ReadTimeout):
        return f"{url} 连上了但一直不回数据。中转站可能太慢，或模型名不对。"
    if isinstance(e, httpx.ProxyError):
        return f"代理出错：{e}"
    if isinstance(e, httpx.ConnectError):
        return (f"连不上 {url}（{e}）。base 可能写错了；若需要代理，"
                f"确认代理已开且允许本程序访问。")
    if isinstance(e, httpx.HTTPError):
        return f"请求 {url} 失败：{type(e).__name__}: {e}"
    return f"{type(e).__name__}: {e}"


def _explain(resp) -> str:
    hint = _HINT.get(resp.status_code)
    if hint is None and resp.status_code >= 500:
        hint = "中转站/服务端自己出错，稍后再试"
    body = " ".join((resp.text or "").split())[:300]
    return f"HTTP {resp.status_code}" + (f"（{hint}）" if hint else "") + "：" + body


def _request(urls: list, method: str, key: str, timeout: float, **kw):
    """按候选地址依次请求，返回 (真正用的 url, 响应)。

    只有 404 才换下一个地址 —— 那是「路径不对」的信号。别的状态码（401/429/500）是
    这个地址本身的问题，换地址也没用，直接交给调用方报错。连不上同理，直接抛。
    """
    if not urls:
        raise RuntimeError("未配置 API Base URL，请在「设置」里填写。")
    resp = None
    for url in urls:
        try:
            resp = httpx.request(method, url, headers=_headers(key), timeout=timeout, **kw)
        except UnicodeEncodeError:
            # key / 地址里有非 ASCII 字符时 httpx 编不了请求头，抛的是 UnicodeEncodeError
            # （ValueError 的子类，不是 HTTPError，所以单独接住）。最可能的来源是界面
            # 回显的掩码「••••1234」被当成真 key 存进了 config.json。
            raise RuntimeError(
                "API Key 或 Base URL 里混进了非 ASCII 字符（比如把设置里打码显示的 "
                "••••1234 原样存了下来）。到「设置」里把 API Key 重新填一遍即可。")
        except httpx.HTTPError as e:
            raise RuntimeError(_net_error(url, e)) from e
        if resp.status_code != 404:
            return url, resp
    return urls[-1], resp


def post_chat(config: dict, payload: dict, timeout: float):
    return _request(chat_urls(config.get("api_base")), "POST",
                    config.get("api_key"), timeout, json=payload)


def list_models(config: dict) -> dict:
    """问中转站「你有哪些模型」。

    中转站的模型名和官方不一样（还常常带自己的后缀），用户瞎猜一个名字只会撞 400/404。
    这里直接把对方列的 id 捞回来，让他照着填。
    """
    urls = models_urls(config.get("api_base"))
    out = {"urls": urls, "ok": False}
    if not urls:
        return {**out, "error": "没填 API Base URL"}
    # 和 test_connection 一样先挡住空 key：放过去的话 httpx 会抛
    # `Illegal header value b'Bearer '`，跟用户的处境八竿子打不着
    if not (config.get("api_key") or "").strip():
        return {**out, "error": "没填 API Key"}
    try:
        url, resp = _request(urls, "GET", config.get("api_key"), 30.0)
    except RuntimeError as e:
        return {**out, "error": str(e)}
    out["url"] = url
    out["status"] = resp.status_code
    if resp.status_code >= 400:
        return {**out, "error": _explain(resp)}
    try:
        data = resp.json()
    except ValueError:
        return {**out, "error": f"{url} 返回的不是 JSON：" + " ".join((resp.text or "").split())[:200]}
    # 标准的 OpenAI 形状是 {"data": [...]}；也有网关直接吐一个数组
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {**out, "error": "返回里没有 data 列表：" + json.dumps(data, ensure_ascii=False)[:200]}
    ids = []
    for it in items:
        mid = it.get("id") if isinstance(it, dict) else it
        if isinstance(mid, str) and mid:
            ids.append(mid)
    out["ok"] = bool(ids)
    out["models"] = sorted(ids)
    if not ids:
        out["error"] = "这个站没有列出任何模型"
    return out


def test_connection(config: dict) -> dict:
    """设置里的「测试连接」：发一条最小请求，把真实结果原样报回来。

    接不上时最没用的信息就是「失败了」；有用的是「打的哪个地址、对方回了什么」。
    """
    api_base = (config.get("api_base") or "").strip()
    model = (config.get("model") or "").strip()
    out = {"urls": chat_urls(api_base), "model": model, "ok": False}
    if not api_base:
        return {**out, "error": "没填 API Base URL"}
    if not (config.get("api_key") or "").strip():
        return {**out, "error": "没填 API Key"}
    if not model:
        return {**out, "error": "没填模型名"}

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    try:
        url, resp = post_chat(config, payload, timeout=30.0)
    except RuntimeError as e:
        return {**out, "error": str(e)}
    out["url"] = url
    out["status"] = resp.status_code
    if resp.status_code >= 400:
        return {**out, "error": _explain(resp)}
    return {**out, "ok": True, "detail": f"HTTP {resp.status_code}，{model} 可用"}


def _require(config: dict):
    """提问前的两项硬检查。一次性问答与流式共用，免得两处报错还不一样。"""
    if not (config.get("api_base") or "").strip():
        raise RuntimeError("未配置 API Base URL，请在「设置」里填写。")
    if not (config.get("api_key") or "").strip():
        raise RuntimeError("未配置 API Key，请在「设置」里填写。")


def ask(config: dict, store: CourseStore, lid, pid, question, history=None,
        course_id=None, code=None, route=None):
    _require(config)
    payload = {
        "model": (config.get("model") or "deepseek-chat").strip(),
        "messages": build_messages(config, store, lid, pid, question, history,
                                   course_id=course_id, code=code, route=route),
        "stream": False,
        "temperature": 0.3,
    }
    url, resp = post_chat(config, payload, timeout=180.0)
    if resp.status_code >= 400:
        raise RuntimeError(_explain(resp))
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"{url} 返回的不是 JSON（多半是被登录页/网关劫持了）："
                           + " ".join((resp.text or "").split())[:200])
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("API 响应格式异常：" + json.dumps(data, ensure_ascii=False)[:300])


# --------------------------------------------------------------------------- 流式
def _frame_text(frame: str) -> str:
    """一个 SSE 帧 → 增量文本。心跳、空 delta、[DONE] 一律跳过。

    只认 `delta.content`：deepseek-reasoner 那类会把思维链放在 `reasoning_content`
    里，接进来就成了把模型的草稿当答案显示给用户。
    """
    for line in frame.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            obj = json.loads(body)
        except ValueError:
            continue
        try:
            delta = obj["choices"][0]["delta"]
        except (KeyError, IndexError, TypeError):
            continue
        text = (delta or {}).get("content")
        if text:
            return text
    return ""


def _whole_body(resp) -> str:
    """对方无视 `stream: true`、直接回了整段 JSON 时的降级路径。

    行为退化成「一次性」，但至少不报错 —— new-api 系的站对 stream 的支持参差不齐，
    这条路上不能把好好的一个回答变成一条错误。
    """
    resp.read()
    raw = resp.text or ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return json.dumps(data, ensure_ascii=False)[:2000]


def _iter_deltas(resp):
    """把一条流式响应拆成增量文本。

    按 `\\n\\n` 切帧，不用 `iter_lines()`：中转站发的行尾可能是 `\\r\\n`，按行切会把
    空行也算一行，帧边界就飘了。
    """
    if "text/event-stream" not in (resp.headers.get("content-type") or "").lower():
        text = _whole_body(resp)
        if text:
            yield text
        return

    buf = ""
    for chunk in resp.iter_bytes():
        buf += chunk.decode("utf-8", "replace")
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            text = _frame_text(frame)
            if text:
                yield text
    if buf.strip():                       # 最后一帧可能没有收尾的空行
        text = _frame_text(buf)
        if text:
            yield text


def stream_chat(config: dict, store: CourseStore, lid, pid, question, history=None,
                course_id=None, code=None, route=None):
    """流式提问，逐段产出文本。

    只有 404 才换下一个候选地址（和 `_request` 的语义一致）；连不上、401、读超时都
    直接抛 —— 那是这个地址本身的毛病，换个地址也一样。
    """
    _require(config)
    payload = {
        "model": (config.get("model") or "deepseek-chat").strip(),
        "messages": build_messages(config, store, lid, pid, question, history,
                                   course_id=course_id, code=code, route=route),
        "stream": True,
        "temperature": 0.3,
    }
    urls = chat_urls(config.get("api_base"))
    if not urls:
        raise RuntimeError("未配置 API Base URL，请在「设置」里填写。")
    key = config.get("api_key")
    timeout = httpx.Timeout(180.0, connect=30.0)
    for i, url in enumerate(urls):
        try:
            with httpx.stream("POST", url, headers=_headers(key), json=payload,
                              timeout=timeout) as r:
                if r.status_code == 404 and i + 1 < len(urls):
                    continue              # with 退出时响应已关闭，可以安全换地址
                if r.status_code >= 400:
                    r.read()
                    raise RuntimeError(_explain(r))
                yield from _iter_deltas(r)
                return
        except UnicodeEncodeError:
            raise RuntimeError(
                "API Key 或 Base URL 里混进了非 ASCII 字符（比如把设置里打码显示的 "
                "••••1234 原样存了下来）。到「设置」里把 API Key 重新填一遍即可。")
        except httpx.HTTPError as e:
            raise RuntimeError(_net_error(url, e)) from e


def generate_title(config: dict, question: str, answer: str):
    """给这轮对话起个短名字。失败返回 None，调用方用问题前 10 字兜底。

    单独发一次小请求、15s 封顶：起名不值得让用户多等，失败了也不影响已经拿到的回答。
    """
    prompt = ("给下面这轮问答起个标题，6-14 个字，直接输出标题本身，"
              "不要引号、不要标点结尾、不要任何解释。\n\n"
              f"【问】{question[:500]}\n【答】{answer[:1500]}")
    payload = {
        "model": (config.get("model") or "deepseek-chat").strip(),
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "temperature": 0.3,
        "max_tokens": 32,
    }
    try:
        _, resp = post_chat(config, payload, timeout=15.0)
    except (RuntimeError, httpx.HTTPError):
        return None
    if resp.status_code >= 400:
        return None
    try:
        text = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    title = " ".join((text or "").split()).strip("《》“”\"'‘’。.、,，:：;；")
    return title[:20] or None
