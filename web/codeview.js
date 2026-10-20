"use strict";

// 课程代码抽屉：文件树 + 只读 Monaco。Monaco 资源随包发布在
// /vendor/monaco/vs（scripts/fetch_monaco.ps1 下载）；缺失时退回 <pre>。
// app.js 在脚本末尾调用 openCodePanel / closeCodePanel，所以这两个必须是全局函数。

let codeCid = "";
let codeTreePath = "";
let codeFilePath = "";
let codePlain = null;            // 降级模式下的 <pre>
let codeEditor = null;           // Monaco 实例
let codeMonaco = null;
let monacoPromise = null;
let codeSeq = 0;                 // 防止慢响应覆盖新文件
let codeAskBtn = null;
let codeRefBar = null;

const MONACO_BASE = "/vendor/monaco/vs";

const LANG_MAP = {
  c: "c", h: "c", cpp: "cpp", cxx: "cpp", cc: "cpp",
  asm: "mips", python: "python", javascript: "javascript",
  typescript: "typescript", shell: "shell", powershell: "powershell",
  markdown: "markdown", json: "json", html: "html", css: "css",
  xml: "xml", sql: "sql", yaml: "yaml", ini: "ini", plaintext: "plaintext",
};

const CODE_KEYWORDS = new Set((
  "auto break case char const continue default do double else enum extern float for goto if " +
  "inline int long register return short signed sizeof static struct switch typedef union " +
  "unsigned void volatile while class public private protected namespace template typename " +
  "using new delete try catch throw true false nullptr this operator virtual override " +
  "def elif except finally from global import in is lambda None nonlocal not or pass raise " +
  "and as assert async await break class continue del else except finally for if import in " +
  "is lambda None nonlocal not or pass raise return try while with yield True False " +
  "fn let mut impl pub use mod crate self super match loop move ref struct enum trait " +
  "package func var go chan defer interface map range select type switch"
).split(/\s+/).filter(Boolean));

// ------------------------------------------------------------------ 面板开关
function openCodePanel(cid) {
  const panel = document.getElementById("code-panel");
  if (!panel) return;
  if (!cid) return;
  panel.hidden = false;
  if (codeCid !== cid) {
    codeCid = cid;
    codeFilePath = "";
    codeTreePath = "";
    if (codeEditor) { codeEditor.dispose(); codeEditor = null; }
    codeMonaco = null;
    codePlain = null;
    document.getElementById("code-editor").innerHTML =
      '<div class="code-placeholder">从左侧选择一个文件</div>';
    ensureAskBtn();
    loadTree("");
  } else if (!document.querySelector("#code-tree .code-tree-item")) {
    loadTree(codeTreePath);
  }
}

function closeCodePanel() {
  const panel = document.getElementById("code-panel");
  if (panel) panel.hidden = true;
}

// ------------------------------------------------------------------ 文件树
async function loadTree(path) {
  const box = document.getElementById("code-tree");
  if (!box || !codeCid) return;
  try {
    const res = await fetch("/api/code/tree?course=" + encodeURIComponent(codeCid) +
      "&path=" + encodeURIComponent(path || ""));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    codeTreePath = path || "";
    renderTree(box, data.entries || [], codeTreePath);
  } catch (e) {
    box.innerHTML = "";
    const msg = document.createElement("div");
    msg.className = "anno-empty";
    msg.textContent = "读不了代码目录：" + e.message;
    box.appendChild(msg);
  }
}

function renderTree(box, entries, path) {
  box.innerHTML = "";
  if (path) {
    const up = document.createElement("button");
    up.type = "button";
    up.className = "code-tree-item dir";
    up.textContent = "↰ 上一级";
    up.addEventListener("click", () => {
      const bits = path.split("/");
      bits.pop();
      loadTree(bits.join("/"));
    });
    box.appendChild(up);
  }
  for (const ent of entries) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "code-tree-item" +
      (ent.type === "dir" ? " dir" : "") +
      (ent.type === "file" && ent.path === codeFilePath ? " current" : "");
    b.textContent = (ent.type === "dir" ? "▸ " : "· ") + ent.name;
    b.title = ent.path;
    b.dataset.path = ent.path;
    b.addEventListener("click", () => {
      if (ent.type === "dir") loadTree(ent.path);
      else openCodeFile(ent.path);
    });
    box.appendChild(b);
  }
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "anno-empty";
    empty.textContent = "这个目录下没有代码文件。";
    box.appendChild(empty);
  }
}

// ------------------------------------------------------------------ Monaco
function ensureMonaco() {
  if (monacoPromise) return monacoPromise;
  const p = new Promise((resolve, reject) => {
    if (window.monaco && window.monaco.editor) { resolve(window.monaco); return; }
    if (window.require && window.require.config) {
      window.require.config({ paths: { vs: MONACO_BASE } });
      window.require(["vs/editor/editor.main"], () => {
        window.monaco ? resolve(window.monaco) : reject(new Error("Monaco 未初始化"));
      }, reject);
      return;
    }
    const s = document.createElement("script");
    s.src = MONACO_BASE + "/loader.js";
    s.onload = () => {
      try {
        window.require.config({ paths: { vs: MONACO_BASE } });
        window.require(["vs/editor/editor.main"], () => {
          window.monaco ? resolve(window.monaco) : reject(new Error("Monaco 未初始化"));
        }, reject);
      } catch (e) { reject(e); }
    };
    s.onerror = () => reject(new Error("缺少 Monaco 资源"));
    document.head.appendChild(s);
  });
  monacoPromise = p;
  // 失败后清掉缓存，允许下次重试（比如用户补下载了资源）。
  p.catch(() => { if (monacoPromise === p) monacoPromise = null; });
  return p;
}

function currentMonacoTheme() {
  return document.documentElement.dataset.theme === "dark" ? "vs-dark" : "vs";
}

function mountEditor(host) {
  const wrap = document.createElement("div");
  wrap.id = "monaco-host";
  host.appendChild(wrap);
  codeMonaco = window.monaco;
  codeEditor = codeMonaco.editor.create(wrap, {
    value: "",
    language: "plaintext",
    theme: currentMonacoTheme(),
    readOnly: true,
    domReadOnly: true,
    automaticLayout: true,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    fontSize: 12,
    renderWhitespace: "selection",
    wordWrap: "off",
    contextmenu: false,
  });
  codeEditor.onDidChangeCursorSelection(() => updateCodeAsk());
  codeEditor.onMouseDown((e) => {
    if (!e.event.ctrlKey && !e.event.metaKey) return;
    const pos = e.target && e.target.position;
    if (pos) jumpByToken(pos.lineNumber, pos.column);
  });
  ensureAskBtn();
}

function showFallback(host, text, lang) {
  codeMonaco = null;
  codeEditor = null;
  host.innerHTML = "";
  const pre = document.createElement("pre");
  pre.className = "code-plain";
  pre.innerHTML = highlightPlain(text, lang);
  host.appendChild(pre);
  codePlain = pre;
  ensureAskBtn();
}

function applyEditorTheme() {
  if (codeEditor && codeMonaco) {
    codeMonaco.editor.setTheme(currentMonacoTheme());
  }
}

new MutationObserver(() => applyEditorTheme()).observe(document.documentElement, {
  attributes: true, attributeFilter: ["data-theme"],
});

// ------------------------------------------------------------------ 打开文件
async function openCodeFile(path) {
  if (!codeCid) return;
  const host = document.getElementById("code-editor");
  if (!host) return;
  const seq = ++codeSeq;
  try {
    const res = await fetch("/api/code/file?course=" + encodeURIComponent(codeCid) +
      "&path=" + encodeURIComponent(path));
    const data = await res.json();
    if (seq !== codeSeq) return;
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    codeFilePath = path;
    markCurrent(path);
    const lang = LANG_MAP[data.lang] || data.lang || "plaintext";

    if (codeMonaco) {
      codeEditor.setValue(data.text);
      codeMonaco.editor.setModelLanguage(codeEditor.getModel(), lang);
      codeEditor.revealLine(1);
      updateCodeAsk();
      return;
    }
    try {
      await ensureMonaco();
      if (seq !== codeSeq) return;
      host.innerHTML = "";
      mountEditor(host);
      codeEditor.setValue(data.text);
      codeMonaco.editor.setModelLanguage(codeEditor.getModel(), lang);
    } catch (e) {
      if (seq !== codeSeq) return;
      showFallback(host, data.text, lang);
      updateCodeAsk();
    }
  } catch (e) {
    if (seq !== codeSeq) return;
    host.innerHTML = "";
    const msg = document.createElement("div");
    msg.className = "code-placeholder";
    msg.textContent = e.message;
    host.appendChild(msg);
  }
}

function markCurrent(path) {
  for (const b of document.querySelectorAll("#code-tree .code-tree-item")) {
    b.classList.toggle("current", b.dataset.path === path);
  }
}

// ------------------------------------------------------------------ 选中 → 问 AI
function updateCodeAsk() {
  if (!codeAskBtn) return;
  const sel = codeEditor ? codeEditor.getSelection() : null;
  const model = codeEditor ? codeEditor.getModel() : null;
  if (!sel || !model || sel.isEmpty()) { codeAskBtn.hidden = true; return; }
  codeAskBtn.hidden = false;
}

function ensureAskBtn() {
  const host = document.getElementById("code-editor");
  if (!host) return;
  if (!codeAskBtn) {
    codeAskBtn = document.createElement("button");
    codeAskBtn.type = "button";
    codeAskBtn.className = "code-ask primary";
    codeAskBtn.textContent = "问 AI";
    codeAskBtn.hidden = true;
    codeAskBtn.addEventListener("click", askSelectedCode);
  }
  if (!host.contains(codeAskBtn)) host.appendChild(codeAskBtn);
}

function askSelectedCode() {
  if (!codeEditor) {
    // 降级模式：选中的是 <pre> 里的文字
    const sel = window.getSelection();
    const text = sel ? String(sel).trim() : "";
    if (!text) return;
    const lines = codePlain ? codePlain.textContent.split("\n") : [];
    const start = lines.findIndex((l) => l.includes(text.split("\n")[0])) + 1;
    const count = text.split("\n").length;
    setCodeRef({ path: codeFilePath, start: start || 1,
                 end: (start || 1) + count - 1,
                 text: text, lang: "" });
    return;
  }
  const sel = codeEditor.getSelection();
  const model = codeEditor.getModel();
  const text = model.getValueInRange(sel);
  if (!text.trim()) return;
  setCodeRef({
    path: codeFilePath,
    start: sel.startLineNumber,
    end: sel.endLineNumber,
    text: text,
    lang: model.getLanguageId(),
  });
}

function setCodeRef(ref) {
  pendingCode = ref;
  const box = document.getElementById("question");
  if (codeRefBar) codeRefBar.remove();
  codeRefBar = document.createElement("div");
  codeRefBar.className = "code-ref";
  const label = document.createElement("span");
  label.textContent = "代码 · " + ref.path + " 第 " + ref.start + "-" + ref.end + " 行";
  const x = document.createElement("button");
  x.type = "button";
  x.className = "ghost";
  x.textContent = "×";
  x.setAttribute("aria-label", "取消代码引用");
  x.addEventListener("click", clearCodeRef);
  codeRefBar.appendChild(label);
  codeRefBar.appendChild(x);
  const form = document.getElementById("chat-form");
  const row = form && form.querySelector(".input-row");
  if (form) form.insertBefore(codeRefBar, row);
  if (box) {
    box.placeholder = "就选中的代码提问…";
    box.focus();
  }
}

function clearCodeRef() {
  pendingCode = null;
  if (codeRefBar) { codeRefBar.remove(); codeRefBar = null; }
  const box = document.getElementById("question");
  if (box) box.placeholder = "问问AI助手";
}
window.__clearCodeRef = clearCodeRef;
window.__setCodeRef = setCodeRef;

// ------------------------------------------------------------------ 跨文件跳转
async function jumpByToken(lineNumber, column) {
  const model = codeEditor && codeEditor.getModel();
  if (!model) return;
  const line = model.getLineContent(lineNumber);
  const word = model.getWordAtPosition({ lineNumber, column });
  const dir = codeFilePath.includes("/")
    ? codeFilePath.slice(0, codeFilePath.lastIndexOf("/")) : "";

  let target = "";
  let m;
  if ((m = line.match(/#\s*include\s*[<"]([^">]+)[">]/))) target = m[1];
  else if ((m = line.match(/^\s*(?:from|import)\s+([.\w]+)/))) {
    const mod = m[1].replace(/^\.+/, "").replace(/\./g, "/");
    target = mod + ".py";
  }
  if (!target && word && /[./]/.test(word.word)) target = word.word;
  if (!target) return;

  const cands = [];
  const join = (a, b) => (a ? a + "/" : "") + b;
  const add = (p) => {
    p = p.replace(/^\.\//, "").replace(/\\/g, "/").replace(/\/{2,}/g, "/");
    if (p && !cands.includes(p)) cands.push(p);
  };
  add(join(dir, target));
  add(target);
  if (!/\.[a-z0-9]+$/i.test(target)) {
    for (const ext of [".h", ".c", ".py", ".cpp", ".hpp", ".s", ".md"]) {
      add(join(dir, target + ext));
    }
  }
  for (const p of cands) {
    try {
      const res = await fetch("/api/code/file?course=" + encodeURIComponent(codeCid) +
        "&path=" + encodeURIComponent(p));
      if (res.ok) { openCodeFile(p); return; }
    } catch (e) { /* 继续试下一个候选 */ }
  }
}

// ------------------------------------------------------------------ 符号搜索
function wireFind() {
  const input = document.getElementById("code-find");
  if (!input) return;
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(runFind, 320);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); runFind(); }
  });
}

async function runFind() {
  const input = document.getElementById("code-find");
  const box = document.getElementById("code-tree");
  if (!input || !box || !codeCid) return;
  const q = input.value.trim();
  if (!q) { loadTree(codeTreePath); return; }
  try {
    const res = await fetch("/api/code/find?course=" + encodeURIComponent(codeCid) +
      "&q=" + encodeURIComponent(q));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
    box.innerHTML = "";
    const rows = data.results || [];
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "anno-empty";
      empty.textContent = "没有匹配的代码。";
      box.appendChild(empty);
      return;
    }
    for (const row of rows) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "code-tree-item";
      b.textContent = "· " + row.path + ":" + row.line;
      b.title = row.path;
      b.dataset.path = row.path;
      b.addEventListener("click", () => {
        openCodeFile(row.path);
        if (codeEditor) {
          codeEditor.revealLineInCenter(row.line);
          codeEditor.setPosition({ lineNumber: row.line, column: 1 });
        }
      });
      box.appendChild(b);
    }
  } catch (e) {
    box.innerHTML = "";
    const msg = document.createElement("div");
    msg.className = "anno-empty";
    msg.textContent = "搜索失败：" + e.message;
    box.appendChild(msg);
  }
}

// ------------------------------------------------------------------ 降级高亮
function highlightPlain(text, lang) {
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  const python = /^(python|shell|yaml|ruby|perl)$/.test(lang);
  const comment = python ? "#[^\\n]*" : "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/";
  const re = new RegExp("(" + comment +
    "|\\\"(?:\\\\.|[^\\\"\\\\])*\\\"|'(?:\\\\.|[^'\\\\])*'" +
    "|\\b\\d+(?:\\.\\d+)?\\b|\\b[A-Za-z_]\\w*\\b)", "g");
  let out = "";
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    out += esc(text.slice(last, m.index));
    const tok = m[0];
    let cls = "";
    if (/^(\/\/|\/\*|#)/.test(tok)) cls = "tk-com";
    else if (/^["']/.test(tok)) cls = "tk-str";
    else if (/^\d/.test(tok)) cls = "tk-num";
    else if (CODE_KEYWORDS.has(tok)) cls = "tk-kw";
    out += cls ? '<span class="' + cls + '">' + esc(tok) + "</span>" : esc(tok);
    last = m.index + tok.length;
  }
  out += esc(text.slice(last));
  return out;
}

// ------------------------------------------------------------------ 启动
ensureAskBtn();
wireFind();

// 降级模式没有 Monaco 的选区事件，用文档级 mouseup 兜底。
document.addEventListener("mouseup", () => {
  if (codeEditor || !codePlain || !codeAskBtn) return;
  const sel = window.getSelection();
  const text = sel ? String(sel).trim() : "";
  let inside = false;
  try {
    inside = !!(sel && sel.rangeCount && codePlain.contains(sel.getRangeAt(0).commonAncestorContainer));
  } catch (e) { inside = false; }
  codeAskBtn.hidden = !(inside && text);
});
