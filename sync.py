"""更新：git pull + 把每讲重新渲染成网页阅读器 bundle（不导出 PDF，无需 Chrome）。"""
import os
import subprocess

# 窗口化 exe 下，git / python / marp 这些控制台子进程默认会各自弹一个 cmd 窗口；
# CREATE_NO_WINDOW 让它们不带控制台窗口运行（stdout/stderr 仍走管道被我们捕获）。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def marp_js(ppt: str) -> str:
    """vendored marp-cli 的入口 js。lecturekit 自己会优先用它，这里只用来判断有没有。"""
    return os.path.join(ppt, "node_modules", "@marp-team", "marp-cli", "marp-cli.js")


def render_env(ppt: str, stubs_dir: str) -> dict:
    """渲染子进程的环境（更新与导出 PDF 共用）。

    lecturekit/demo.py 顶层 import fcntl/pty/termios（Unix 专属），用占位模块让 import 过。

    这里不设 LECTUREKIT_MARP：lecturekit 的 marp_command() 会自己解析，优先用 vendored 的
    node_modules/@marp-team/marp-cli/marp-cli.js（以 `node <js>` 方式启动）。而那个覆盖会指向
    node_modules/.bin/marp.cmd —— Windows 上 .bin 里是 shell 脚本，CreateProcess 起不来
    （见 lecturekit/renderers/viewer/marp.py 的 marp_command 注释）。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = stubs_dir + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _git_env():
    env = dict(os.environ)
    # 后台 Job 里绝不能让 git 弹出凭据提示：窗口化进程没有控制台可输，会一直挂着
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def pull(repo: str, log=lambda line: None):
    """git pull --ff-only。失败不致命，用本地版本继续。"""
    log("git pull --ff-only ...")
    try:
        r = subprocess.run(["git", "-C", repo, "pull", "--ff-only"],
                           capture_output=True, text=True, env=_git_env(),
                           creationflags=CREATE_NO_WINDOW)
    except OSError as e:
        log(f"git 不可用：{e}")
        return False
    if r.returncode == 0:
        log("git pull 成功")
        return True
    log("git pull 失败（用本地版本继续）: "
        + (r.stderr.strip().splitlines() or ["?"])[-1][:200])
    return False


def clone(url: str, dst: str, log=lambda line: None):
    """浅克隆到 dst。目标已存在时直接失败，调用方负责选新 id。"""
    if os.path.isdir(os.path.join(dst, ".git")):
        return True
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    log(f"git clone --depth 1 {url}")
    try:
        r = subprocess.run(["git", "clone", "--depth", "1", url, dst],
                           capture_output=True, text=True, env=_git_env(),
                           creationflags=CREATE_NO_WINDOW)
    except OSError as e:
        raise RuntimeError(f"git 不可用：{e}")
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        raise RuntimeError("clone 失败：" + (detail[-1][:300] if detail else "未知错误"))
    return True


def find_lecturekit_root(repo: str):
    """找 `lecturekit/` 包所在目录（ICS 仓库在 ICS-PPT/ 下）。"""
    if not os.path.isdir(repo):
        return None
    for rel in ("", "ICS-PPT", "lecturekit", "slides", "tools"):
        base = os.path.join(repo, rel) if rel else repo
        if os.path.isdir(os.path.join(base, "lecturekit")):
            return base
    for base, dirs, _files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__",
                                                "build", "dist")]
        if "lecturekit" in dirs:
            return base
        if base[len(repo):].count(os.sep) >= 3:
            dirs[:] = []
    return None


def render_lecture(src: str, out: str, python: str, stubs_dir: str, kit_root: str,
                   log=lambda line: None):
    """渲染一个含 lecture.py 的目录。返回 (ok, 错误摘要)。"""
    env = render_env(kit_root, stubs_dir)
    # 仓库自己的 lecturekit 可能没装进解释器：把它所在目录塞进 PYTHONPATH
    env["PYTHONPATH"] = kit_root + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [python, "-m", "lecturekit.cli", "render", src, "--out", out],
        cwd=kit_root, env=env, capture_output=True, text=True,
        creationflags=CREATE_NO_WINDOW,
    )
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or r.stdout or "").strip()[-500:]


def update(config: dict, build_root: str, stubs_dir: str, log=lambda line: None):
    """阻塞执行更新，log 回调逐条报告进度。渲染出的 bundle 放到 build_root/<name>/。"""
    repo = config["repo"]
    ppt = os.path.join(repo, "ICS-PPT")
    python = config.get("python") or "python"
    lectures_dir = os.path.join(ppt, "lectures")

    env = render_env(ppt, stubs_dir)
    if os.path.exists(marp_js(ppt)):
        log("使用 vendored marp-cli")
    else:
        log("未找到 vendored marp，将回退 npx（可能需联网）")

    # 1. 拉取老师最新课件（失败不致命，用本地版本继续）
    pull(repo, log)

    # 2. 逐个渲染
    if not os.path.isdir(lectures_dir):
        log("找不到课件目录：" + lectures_dir)
        return
    names = sorted(
        n for n in os.listdir(lectures_dir)
        if os.path.isfile(os.path.join(lectures_dir, n, "lecture.py"))
    )
    if not names:
        log("lectures/ 下没有含 lecture.py 的课件")
        return

    os.makedirs(build_root, exist_ok=True)
    ok = 0
    for n in names:
        src = os.path.join(lectures_dir, n)
        out = os.path.join(build_root, n)
        log(f"渲染 {n} ...")
        rep, detail = render_lecture(src, out, python, stubs_dir, ppt)
        if rep:
            ok += 1
            log(f"  {n} 成功")
        else:
            log(f"  {n} 失败：{detail[-400:]}")
    log(f"完成：{ok}/{len(names)} 个课件渲染成功")
