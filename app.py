"""课件阅读器 + AI 助教 —— 入口。

启动本地 HTTP 服务（前端壳 + 课件 bundle + API），再用 pywebview 开一个原生窗口。
用法：python app.py    （打包：build_exe.ps1）

窗口通过 js_api 暴露三个原生对话框（打开资料 / 另存为 / 选目录）——前端在浏览器里
跑时拿不到它们，会自己降级，见 web/app.js。
"""
import threading

import config as cfgmod
import server


class Bridge:
    """暴露给前端 JS 的原生能力：window.pywebview.api.<method>(...)。

    pywebview 6 里 create_file_dialog 返回路径序列（取消则 None），这里统一成
    单个字符串或 None，前端只需判断空值。

    窗口引用必须叫 `_window`。pywebview 生成 JS 代理时会遍历 js_api 对象的每个公开
    属性并**递归**下去（webview/util.py: get_functions，只跳过下划线开头的名字）。
    叫 window 的话它会一路走到 WinForms/WebView2 控件上，在非 UI 线程上调
    CoreWebView2Controller 的 COM 属性 —— 整条桥就卡死在那里，页面永远收不到
    pywebviewready，窗口出来就是一片「无响应」。
    """

    def __init__(self):
        self._window = None

    def _dialog(self, kind, **kw):
        if self._window is None:
            return None
        import webview
        try:
            res = self._window.create_file_dialog(kind, **kw)
        except Exception:
            return None
        if not res:
            return None
        return res[0] if isinstance(res, (list, tuple)) else res

    def pick_document(self):
        """弹「打开文件」选一份资料。返回路径字符串，取消返回 None。"""
        import webview
        return self._dialog(webview.FileDialog.OPEN,
                            allow_multiple=False,
                            file_types=("课程资料 (*.pdf;*.pptx;*.md)",))

    def pick_documents(self):
        """多选资料：PDF / PPTX / Markdown。返回路径列表，取消返回 []。"""
        if self._window is None:
            return []
        import webview
        try:
            res = self._window.create_file_dialog(
                webview.FileDialog.OPEN,
                allow_multiple=True,
                file_types=("课程资料 (*.pdf;*.pptx;*.md)",))
        except Exception:
            return []
        if not res:
            return []
        return list(res) if isinstance(res, (list, tuple)) else [res]

    def save_pdf_dialog(self, default_name="lecture.pdf"):
        """弹「另存为」选 PDF 目标路径。返回路径字符串，取消返回 None。"""
        import webview
        return self._dialog(webview.FileDialog.SAVE,
                            save_filename=default_name,
                            file_types=("PDF 文件 (*.pdf)",))

    def pick_directory(self):
        """弹目录选择（向导里 clone 课程仓库用）。"""
        import webview
        return self._dialog(webview.FileDialog.FOLDER)


def main():
    config = cfgmod.load_config()
    app = server.App(config)

    port = server.find_port(int(config.get("port") or 8787))
    httpd = server.make_server(app, port)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    # 启动时自动更新（后台线程，不阻塞窗口）
    if config.get("auto_update"):
        app.start_update()

    import webview
    bridge = Bridge()
    # 「跟随系统」在这儿就解析成具体值：窗口背景色必须在页面加载前定下来，否则深色下
    # 开窗会先闪一块白。页面的内联脚本读这个 ?theme= 只管首绘，之后以 /api/config 为准。
    theme = str(config.get("theme") or "system").strip()
    if theme not in ("light", "dark"):
        theme = cfgmod.system_theme()
    window = webview.create_window(
        "ICS-Reader",
        f"http://127.0.0.1:{port}/?theme={theme}",
        width=1440,
        height=900,
        min_size=(960, 640),
        js_api=bridge,
        background_color="#181818" if theme == "dark" else "#f7f7f8",
    )
    bridge._window = window
    webview.start()


if __name__ == "__main__":
    main()
