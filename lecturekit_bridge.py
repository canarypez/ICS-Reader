"""把 lecturekit 的「跳到源码 / 运行示例」接回静态渲染的课件。

老师自己的 `view --watch` 开发服务器会在 `slides.html` 里注入 source.js 和
demo.js，并实现 `/__source`、`/__demo` 两个端点；静态 `render` 出来的 bundle
不带这两样，所以按钮一直是 disabled。阅读器在这里补上同一套东西：

* 按课件 URL 找到它对应的源码目录（有 `lecture.py` 的那一层），
  源码文件与 demo 命令都以它为根 —— 和 lecturekit 开发服务器一致；
* 直接复用仓库里的 `lecturekit.demo` / `lecturekit.source`，不另写一份执行器；
  仓库里没有 lecturekit 时，源码面板仍能显示文本，示例运行则明确报不可用。

请求只带 id，路径解析全部发生在服务端（和 lecturekit 的信任模型相同）。
"""
from __future__ import annotations

import json
import os
import sys

import sync

#: 渲染产物里由 lecturekit 写出的两张表。
SOURCES_FILE = "sources.json"
DEMOS_FILE = "demos.json"

#: 源码根下可能藏着 lecture.py 的目录（与 courses.scan_repo 一致）。
_LECTURE_PARENTS = ("lectures", "slides", "decks", "course/lectures",
                    "ICS-PPT/lectures")

_SKIP_DIRS = {".git", ".hg", "node_modules", "__pycache__", "build", "dist",
              ".venv", "venv", "target", "out"}

#: 和 lecturekit.source 的上限一致；面板里再大的文件也没法读。
MAX_SOURCE_BYTES = 512 * 1024


def find_lecture_dir(source_root: str, name: str):
    """`source_root` 里名为 `name`、带 `lecture.py` 的目录；找不到就 None。"""
    source_root = os.path.abspath(str(source_root or ""))
    name = str(name or "").strip()
    if not name or not os.path.isdir(source_root):
        return None
    if name in (".", "..") or "/" in name or "\\" in name:
        return None
    for rel in _LECTURE_PARENTS:
        candidate = os.path.join(source_root, *rel.split("/"), name)
        if os.path.isfile(os.path.join(candidate, "lecture.py")):
            return candidate
    for base, dirs, files in os.walk(source_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if os.path.basename(base) == name and "lecture.py" in files:
            return base
        if base[len(source_root):].count(os.sep) >= 4:
            dirs[:] = []
    return None


def load_modules(source_root: str):
    """仓库里的 (demo, source) 模块；没有 lecturekit 时返回 (None, None)。

    仓库的 lecturekit 通常没装进解释器，所以把包所在目录临时塞进 sys.path。
    两个模块导入很轻（不碰渲染依赖），失败就当作不可用。
    """
    kit = sync.find_lecturekit_root(source_root) if source_root else None
    if not kit:
        return None, None
    if kit not in sys.path:
        sys.path.insert(0, kit)
    try:
        from lecturekit import demo, source       # noqa: WPS433 (runtime import)
        return demo, source
    except Exception:
        return None, None


def read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_sources(render_dir: str) -> dict:
    """`sources.json` 的 `id -> 相对路径` 表；坏文件当作空表。"""
    table = read_json(os.path.join(render_dir, SOURCES_FILE))
    out = {}
    for key, value in table.items():
        if isinstance(value, dict) and isinstance(value.get("path"), str):
            out[str(key)] = value["path"]
    return out


def read_demos(render_dir: str) -> dict:
    """`demos.json` 的 `id -> {command, timeout, interactive}` 表。"""
    table = read_json(os.path.join(render_dir, DEMOS_FILE))
    out = {}
    for key, value in table.items():
        if isinstance(value, dict) and isinstance(value.get("command"), str):
            out[str(key)] = value
    return out


def safe_file(root: str, rel: str):
    """`rel` 指向 `root` 内的真实文件时返回绝对路径，否则 None。

    用 realpath 再判一次：符号链接可以把一个 `..` 已经检查过的相对路径带到
    源码根之外，这和 lecturekit.source.resolve 的第二道门是同一个理由。
    """
    root = os.path.realpath(str(root or ""))
    rel = str(rel or "").strip()
    if not root or not rel:
        return None
    try:
        path = os.path.realpath(os.path.join(root, rel))
    except (OSError, ValueError):
        return None
    if path != root and root + os.sep not in path:
        return None
    return path if os.path.isfile(path) else None


def load_text(path: str) -> str:
    """源码文本，超长截断并标注（与 lecturekit.source.load 同一规则）。"""
    with open(path, "rb") as f:
        data = f.read(MAX_SOURCE_BYTES + 1)
    text = data[:MAX_SOURCE_BYTES].decode("utf-8", errors="replace")
    if len(data) > MAX_SOURCE_BYTES:
        text += "\n… [file past %d bytes not shown]" % MAX_SOURCE_BYTES
    return text


#: 由本地服务注入到 slides.html 末尾。样式先于脚本，source.js 要先于 demo.js
#: （两者都在捕获阶段处理 Escape / 点击，先加载的那个先看到）。
INJECT_HEAD = (
    '<link rel="stylesheet" href="/vendor/lecturekit/vendor/xterm/xterm.css">'
    '<link rel="stylesheet" href="/vendor/lecturekit/source.css">'
    '<link rel="stylesheet" href="/vendor/lecturekit/demo.css">'
)
INJECT_BODY = (
    '<script src="/vendor/lecturekit/vendor/xterm/xterm.js"></script>'
    '<script src="/vendor/lecturekit/vendor/xterm/addon-fit.js"></script>'
    '<script src="/vendor/lecturekit/source.js"></script>'
    '<script src="/vendor/lecturekit/demo.js"></script>'
)


def inject_deck(html: str) -> str:
    """给渲染好的 slides.html 补上文件面板与示例终端。

    只有真的存在 `.lk-demo` / `data-lk-source` 的课件才注入，避免给每份
    静态课件平白加几百 KB。
    """
    if "lk-demo" not in html and "data-lk-source" not in html:
        return html
    block = INJECT_HEAD + INJECT_BODY
    at = html.lower().rfind("</body>")
    if at < 0:
        return html + block
    return html[:at] + block + html[at:]
