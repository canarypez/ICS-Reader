# ICS 课件阅读器

面向上海交通大学《计算机系统基础》（ICS / CS:APP）课程的本地课件阅读器，附带一个 AI 助手窗口。除了内置课程，也可以导入自己的仓库课程，或上传 PDF / PPTX / Markdown 资料。课件在左侧阅读，右侧随时就当前页提问。

## 主要功能

- 以单文件 `.exe` 运行。首页用课程卡片管理课程，显示讲/页数、磁盘占用和上次阅读位置；可以导入仓库、上传资料。
- 左侧阅读器支持连续滚动、单页翻页和缩略图，50%–300% 缩放；PDF / PPTX 的文字可选中复制。
- 阅读进度自动保存，重新打开回到上次的资料和页码。
- 右侧为对话侧栏；分隔条可拖动，宽度记录在 `config.json`。
- 默认跟随 Windows 的深浅色，设置里可以切换。
- 回答时注入当前页全文、前后页摘要、本讲大纲、整门课结构，以及检索到的相关页；问题涉及代码时会带上代码片段。
- 启动时自动执行 `git pull` 并重新渲染课件，“更新”按钮可手动触发；导入的仓库课程可以定时更新。
- 采用 OpenAI 兼容接口，`api_base` / `model` / `api_key` 均可编辑，支持 DeepSeek、OpenAI、Moonshot、智谱、本地 Ollama 等。
- 标题、粗体/斜体、删除线、有序与无序列表、表格、引用块、分隔线、链接、行内与块级代码。渲染器内置极简实现，不引 CDN、无第三方库。LaTeX 公式由内置 KaTeX 离线渲染；流式输出时实时渲染。
- 对话按资料 / 讲次分开；可以随时「新对话」。
- 可以将当前讲一键导出为 PDF。
- 提问栏可在「问答 / 检索」之间切换；检索范围为本资料 / 本讲或整门课。
- 选中文字，松开鼠标弹出菜单，包含「引用」、「解释」、「复制」。
- ICS 课件保留「跳到源码」和「运行示例」终端。

## 使用方式

### 方式一：可执行文件（推荐）

到 [Releases](https://github.com/canarypez/ICS-Reader/releases) 下载最新 `ICS-Reader.exe`，仅 Windows。

### 方式二：直接运行

```powershell
pip install -r requirements.txt
python app.py
```

### 方式三：打包为单文件可执行程序

```powershell
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
# 产物：dist\ICS-Reader.exe
```

将 `ICS-Reader.exe` 置于任意目录并双击运行。首次启动时，程序会在 exe 同目录生成 `config.json`
与课件渲染产物 `course\`；开始提问后，对话历史会写在同目录的 `history\<讲>.json` 里。
`history\` 和 `course\` 都可以随时整个删掉，不影响程序运行（前者只是聊天记录，后者下次更新会重建）。

课程数据保存在 `courses\<课程 id>\`。旧的 `documents\<id>\` 会在首次启动时复制到
`courses\mydocs\items\`，原件保留；删除资料或课程时，副本和原件会一起清掉。

## 配置

程序启动后，点击界面右上角「设置」即可修改配置；也可直接编辑 exe 同目录下的 `config.json`。配置项如下：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `repo` | `C:\...\OS-Course` | 课程仓库的本地克隆路径 |
| `api_base` | `https://api.deepseek.com` | OpenAI 兼容接口的根地址，可只填域名（自动补 `/v1/chat/completions`） |
| `model` | `deepseek-chat` | 模型名称 |
| `api_key` | （空） | API 密钥，仅保存在本地 |
| `python` | `python` | 用于渲染课件的 Python 解释器 |
| `auto_update` | `true` | ICS 课程启动时是否更新 |
| `port` | `8787` | 本地 HTTP 服务端口，占用时自动递增 |
| `max_retrieve` | `4` | 提问时额外检索进上下文的相关页数量 |
| `system_prompt` | （空） | 自定义提示词，留空则使用默认值 |
| `chat_width` | `440` | 右侧对话栏宽度（px），拖分隔条时自动记录 |
| `theme` | `system` | 界面主题：`system` 跟随 Windows 深浅色，也可填 `light` / `dark` 强制 |
| `history_budget` | `12000` | 追问时最多往回带多少个字符的旧消息 |
| `wizard_dismissed` | `[]` | 已在向导里选择「不再提示」的缺失项 |

课件上下文相关的 `context_budget`、`local_page_chars`、`neighbor_summary_chars`、`retrieve_chars`、`code_context_chars` 控制各部分字符上限，默认值见 `config.py`。

> 界面偏好（`chat_width` / `theme`）只能存在 `config.json` 里：窗口使用 WebView2
> 隐私模式，`localStorage` 不保留。

### 常见 API 问题

> `api_base` 填写域名，程序自动补上 `/v1/chat/completions`；
> 连接不上时，请在设置自查：
> - **「列出模型」** —— 获取该站 `GET /v1/models` 里列出的模型。
> - **「测试连接」** —— 发一条最小请求，显示**真实请求的地址**和**对方的原始回复**。

```jsonc
// DeepSeek
{ "api_base": "https://api.deepseek.com", "model": "deepseek-chat" }

// 各类中转站（one-api / new-api 等）：站点域名
{ "api_base": "https://中转站.com", "model": "gpt-4o-mini" }

// OpenAI
{ "api_base": "https://api.openai.com/v1", "model": "gpt-4o" }

// Moonshot（Kimi）
{ "api_base": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k" }

// 智谱 GLM
{ "api_base": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-plus" }

// 本地 Ollama
{ "api_base": "http://localhost:11434/v1", "model": "qwen2.5:7b", "api_key": "ollama" }
```

> `api_key` 仅保存在本地 `config.json` 中，不会被提交或上传到任何地方。

## 工作原理

程序启动后依次完成：

1. 启动本地 HTTP 服务（仅绑定 `127.0.0.1`），提供前端页面、课件静态资源与 JSON API；
2. 初始化课程注册表：同步 ICS 元数据、迁移旧 `documents\`（仅复制）、加载各课程的资料与渲染产物；
3. 后台为每门课维护 FTS5 索引，解析 `lecture.json`（大纲树 + 每页文本），构建课程知识库；
4. 打开 `pywebview` 原生窗口加载前端。

提问时，后端将以下内容组合为上下文并发送给模型：

1. 当前页全文及相邻页摘要；
2. 当前讲 / 资料的大纲；
3. 整门课各讲、各部分的标题；
4. 按问题关键词在课程内检索出的相关页；
5. 问题涉及代码时，附上选中的代码和相关实现。

问题包含「哪一讲 / 前面 / 整个课程」等词时会检索整门课，包含代码或实验术语时走代码联动，其余按当前页回答。
追问时还会加上本对话之前的若干轮问答（见 `history_budget`）。**历史里存的是当初的问题和回答原文，课件上下文每轮重新组装。**

回答以 SSE 逐段推给界面（`POST /api/chat/stream`），使用本地服务的 HTTP/1.0 连接关闭定界，
不需要 chunked 编码。中转站若无视 `stream: true` 则直接返回整段 JSON，程序会退化成一次性显示。

## 项目结构

```
os-course-reader/
├── app.py            # 入口：启动服务并打开窗口
├── courses.py        # 课程注册表：课程元数据、迁移、进度、代码根
├── agent.py          # 课程知识库、三级路由、上下文预算与 LLM 调用
├── search.py         # 每门课一个 SQLite FTS5 索引（页面 + 代码）
├── docs.py           # 资料：PDF/PPTX/MD 解析、文本层、页面渲染
├── history.py        # 对话历史：按讲或按资料分文件保存、原子写、增长上限
├── sync.py           # 更新：git pull + 重新渲染
├── pdf_export.py     # 导出 PDF 的独立脚本（由外部 Python 子进程执行）
├── pptx2pdf.ps1      # 调用 PowerPoint / WPS 把 PPTX 转成 PDF
├── lecturekit_bridge.py  # 静态课件补回 source.js / demo.js 与端点解析
├── wizard.py         # 环境检测与逐项安装
├── server.py         # 本地 HTTP 服务与 JSON API
├── config.py         # 配置读写与路径解析
├── config.json       # 用户配置
├── web/              # 前端界面；docview.* 资料阅读器
│   └── vendor/lecturekit/  # 源码面板 / 示例终端控制器与 xterm
├── scripts/          # 开发脚本
├── stubs/            # Windows 兼容占位模块（fcntl / pty / termios）
├── tests/            # 单元测试
├── requirements.txt  # 运行依赖
├── build_exe.ps1     # PyInstaller 打包脚本
└── README.md
```

## 环境依赖

**浏览课件与提问助教不需要下面任何一项**；只有「更新课件」与「导出 PDF」需要：

| 依赖 | 用途 |
| --- | --- |
| Git | 拉取课程仓库 |
| 课程仓库本地克隆 | 课件源码 |
| Python ≥ 3.12 + `pip install -e ICS-PPT` | 渲染引擎 lecturekit |
| Node.js + vendored Marp CLI | 排版幻灯片 |
| Chrome | 打印 PDF 目录页 |

程序自身的运行依赖见 `requirements.txt`；打包另需 `pyinstaller`。上传资料不需要 Git、课程仓库或 lecturekit。

## 更新日志

### v1.1.0

- 新增多课程首页，支持导入仓库、上传 PDF / PPTX / Markdown，同名课程自动合并。
- 重做阅读器：连续 / 单页 / 缩略图、滚轮翻页、缩放、PDF 文字层、服务端阅读进度。
- 恢复 LectureKit 的跳转源码与运行示例。
- AI 增加课程级检索、代码联动和上下文预算。
- 修复删除资料 / 课程不彻底、PDF 首屏加载遮罩不消失等问题。
- 顶栏临时提示 5 秒后消失；回答和划词内容可复制。

### v1.0.0

- **流式回答**：打字机效果，公式实时渲染；
  已经收到的部分会保留，并照常存进历史。
- **对话历史**：对话按讲分开保存，标题由模型自动生成；追问时会带上之前的问答。
- **Markdown 排版**：补上表格、引用块、分隔线、链接、删除线，以及可嵌套的有序 / 无序列表。
  同时修改三处渲染错误 —— 代码块里的空行会把代码块拆开、代码里的 `$变量` 被当成公式、
  代码块的语言标记（` ```bash ` 里的 `bash`）漏进正文；并堵掉一处链接属性注入。

## 注意事项

- 课件渲染本身不导出 PDF，渲染阶段不需要 Chrome；**导出 PDF** 才会用到。
- `git pull` 需要能够访问 GitHub。若网络受限，更新会回退至本地已有版本并在日志中提示，不影响课件浏览。
- 本程序通过本地回环地址提供页面与服务，不对外网开放；仅有 LLM 请求会访问外部 API。
- 仓库导入会以当前用户权限执行仓库里的 `lecture.py`，只导入你信任的仓库。
- 提问时会把当前页、相邻页摘要和检索到的片段发送给你配置的模型服务。
- 向导会真实改动你的系统。它只在你**逐项点击按钮**后执行。
