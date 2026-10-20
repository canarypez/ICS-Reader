"""配置读写 + 路径解析。

api_base / model / api_key 全部可编辑（OpenAI 兼容接口），写在 exe 同目录的
config.json 里；界面「设置」里也能改。打包成 exe 后，config.json 和渲染产物
build/ 都放在 exe 旁边（DATA_DIR），可写；代码和前端资源打进 exe 内部。
"""
import json
import os
import sys

# 是否被 PyInstaller 打包
FROZEN = bool(getattr(sys, "frozen", False))

# 源码目录（开发时）
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# 可写数据目录：打包后 = exe 所在目录；开发时 = 本源码目录
DATA_DIR = os.path.dirname(sys.executable) if FROZEN else APP_DIR

# 打包后的只读资源目录（onefile 解压临时目录）
def resource(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", APP_DIR)
    return os.path.join(base, rel)


DEFAULTS = {
    # 老师仓库的本地 clone
    "repo": r"C:\Users\lenovo\Downloads\CSAPP\OS-Course",
    # OpenAI 兼容接口：deepseek / openai / moonshot / 智谱 / ollama 等都能填
    "api_base": "https://api.deepseek.com",
    "api_key": "",
    "model": "deepseek-chat",
    # 用哪个 python 解释器去渲染课件（打包后 exe 本身不是解释器）
    "python": "python",
    # 启动时自动 git pull + 重新渲染
    "auto_update": True,
    # 本地 HTTP 端口（被占用会自动 +1）
    "port": 8787,
    # 检索时额外塞进上下文的相关页数量
    "max_retrieve": 4,
    # 自定义 system prompt（留空用默认）
    "system_prompt": "",
    # 用户在向导里勾了「不再提示」的检测项（key 列表）
    "wizard_dismissed": [],
    # 聊天侧栏宽度（px，拖拽后记住）
    "chat_width": 440,
    # 主题：system = 跟随 Windows，light / dark = 强制
    "theme": "system",
    # 多轮对话里往回带多少字符的旧消息（超了从最旧的开始丢）
    "history_budget": 12000,
    # 单次请求的课件上下文总预算，以及各层上限（见 agent.build_context）
    "context_budget": 16000,
    "local_page_chars": 6000,
    "neighbor_summary_chars": 600,
    "retrieve_chars": 1000,
    "code_context_chars": 4000,
}


#: 界面上 api_key 的回显掩码（见 server.py 的 /api/config）。
#: 用户打开设置、没重填 key 就点保存的话，这串圆点会被当成真 key 存下来 —— 之后每次
#: 请求都发 `Authorization: Bearer ••••1234`，而 httpx 编不了非 ASCII 的请求头，报回来
#: 的是一句 `'ascii' codec can't encode characters in position 7-10` 的天书。
#: 读、写两侧都得挡住它。
MASK = "••••"


def is_masked(value) -> bool:
    """这个值是界面回显用的掩码，不是真 key。"""
    return str(value or "").strip().startswith(MASK)


def system_theme() -> str:
    """Windows「应用」模式的深浅色；读不到就按浅色算。

    只在开窗那一刻用一次（窗口背景色 + 首绘提示）—— 页面自己还会 matchMedia 一次，
    之后以 config.json 的 theme 为准。
    """
    if os.name != "nt":
        return "light"
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as k:
            light, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        return "light" if int(light) else "dark"
    except Exception:
        return "light"


def config_path() -> str:
    return os.environ.get("OSCOURSE_CONFIG", os.path.join(DATA_DIR, "config.json"))


def build_root() -> str:
    # 用 course/ 而不是 build/，避免和 PyInstaller 的构建缓存目录撞名
    return os.path.join(DATA_DIR, "course")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    p = config_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    # 自愈：早先的版本会把界面回显的掩码当 key 存下来，那种配置等于坏掉了，
    # 当作没填，让用户在设置里重填一次
    if is_masked(cfg.get("api_key")):
        cfg["api_key"] = ""
    return cfg


def save_config(updates: dict) -> dict:
    cfg = load_config()
    clean = {k: v for k, v in updates.items() if k in DEFAULTS}
    # 掩码只是给界面看的，不是真 key；原样存下去会把用户真正的 key 覆盖掉
    if is_masked(clean.get("api_key")):
        clean.pop("api_key")
    cfg.update(clean)
    p = config_path()
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg
