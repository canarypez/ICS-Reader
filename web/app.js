"use strict";

const $ = (s) => document.querySelector(s);
const viewer = $("#viewer");
const select = $("#lecture-select");
const courseSel = $("#course-select");
const fileInput = $("#file-input");
const statusEl = $("#update-status");
// 顶栏那句小字是「刚刚发生了什么」，不是常驻状态：出现后停留 5 秒就淡出。
// 用 MutationObserver 而不是给每个赋值点加定时器 —— 导出/导入这类轮询会不停改
// 同一行文字，每次变化都重新计时，任务结束 5 秒后自然消失。
let statusTimer = null;
new MutationObserver(() => {
  clearTimeout(statusTimer);
  if (!statusEl.textContent) return;
  statusTimer = setTimeout(() => { statusEl.textContent = ""; }, 5000);
}).observe(statusEl, { childList: true, characterData: true, subtree: true });
const messagesEl = $("#messages");
const resultsEl = $("#results");
const scopeEl = $("#search-scope");
const emptyEl = $("#deck-empty");
const deckLoadingEl = $("#deck-loading");
const deckLoadingText = $("#deck-loading-text");
const ctxMenu = $("#ctx-menu");
const resizer = $("#chat-resizer");

// 课件区缓冲层。切换讲次/资料时先盖住旧画面，等 iframe 真正 load（资料阅读器
// 则等第一页的 doc-page 消息）再淡出；至少显示 200ms，避免一闪而过比不显示还难受。
let deckLoadingSeq = 0;
let deckLoadingShownAt = 0;

function showDeckLoading(text) {
  if (!deckLoadingEl) return;
  deckLoadingSeq++;
  deckLoadingShownAt = Date.now();
  deckLoadingText.textContent = text || "正在加载…";
  deckLoadingEl.classList.add("show");
}

function hideDeckLoading() {
  if (!deckLoadingEl || !deckLoadingEl.classList.contains("show")) return;
  const seq = deckLoadingSeq;
  const wait = Math.max(0, 200 - (Date.now() - deckLoadingShownAt));
  setTimeout(() => {
    if (seq === deckLoadingSeq) deckLoadingEl.classList.remove("show");
  }, wait);
}

let current = null;              // {lecture, page, title, number}
let mode = "ask";                // ask | search
let lectureVid = {};             // 目录名 -> 外壳自己的讲 id（跳页用的 sessionStorage 键）
let pendingQuote = null;         // 划词菜单选中待用的文本

// 课程首页 / 当前课程
let courses = [];                // /api/courses 的卡片
let currentCourse = null;        // 当前打开的课程卡片
let items = [];                  // 当前课程的全部 item（课件 + 资料）
let courseMode = "ics";          // ics | docs，由当前 item 推导
let icsLectures = [];            // 当前课程里的课件 item
let documents = [];              // 当前课程里的资料 item
const lastPick = { ics: "", docs: "" };

let view = "home";               // home | work
let courseJobTimer = null;

// ------------------------------------------------------------------ 基础
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  return res.json();
}

// 引号也要转：链接的 href 是唯一一处把模型给的文字放进**属性**的地方，
// 不转的话 `[x](https://a.com"onmouseover="alert(1))` 能拼出一个真的属性注入。
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// KaTeX 是整个渲染里最贵的一步（单个公式 1-5ms，一轮回答几十个公式就是上百毫秒）。
// 流式时每来一小段就要重跑一次 md()，不记下来的话同一批公式会被反复渲染几十遍。
// 键里带上 displayMode：同一个 tex 行内和块级的渲染结果不一样。
const katexCache = new Map();
const KATEX_CACHE_MAX = 500;

function renderTex(tex, display) {
  const key = (display ? "D\u0000" : "I\u0000") + tex;
  const hit = katexCache.get(key);
  if (hit !== undefined) return hit;
  let html;
  try {
    html = katex.renderToString(tex, { displayMode: display, throwOnError: false });
  } catch (e) {
    html = escapeHtml(display ? `$$${tex}$$` : `$${tex}$`);
  }
  if (katexCache.size >= KATEX_CACHE_MAX) katexCache.clear();   // 够用就行，不必做 LRU
  katexCache.set(key, html);
  return html;
}

// 流到一半时 `$x^2` 还没闭合，原样渲染会闪一个裸 `$`。这里给未闭合的部分补上**配对的**
// 收尾符，让中途也渲染成公式；真闭合符到了自然收敛。
//
// 只作用于传给 md() 的副本，回答缓冲保持原样 —— 这个函数不修改输入。
// 朴素地数 `$` 的奇偶会全错：得认 `\` 转义、``` 围栏、` 行内代码，代码里的 `$` 不是公式；
// `$$` 也只能用 `$$` 收。已知残留：`$5` 这种把 `$` 当货币用的写法仍会被当成公式开头
// （可接受 —— 系统提示已经要求公式一律写成 $...$）。
function balanced(src) {
  let i = 0;
  const n = src.length;
  let fence = false;      // 在 ``` 围栏里
  let inline = false;     // 在 ` 行内代码里
  let open = "";          // 当前没闭合的公式定界符："$" 或 "$$"
  while (i < n) {
    if (src[i] === "\\") { i += 2; continue; }        // 转义：连着下一个字符一起跳过
    if (fence) {
      if (src.startsWith("```", i)) { fence = false; i += 3; } else i += 1;
      continue;
    }
    if (src.startsWith("```", i)) { fence = true; i += 3; continue; }
    if (inline) {
      if (src[i] === "`") inline = false;
      i += 1;
      continue;
    }
    if (src[i] === "`") { inline = true; i += 1; continue; }
    if (src[i] !== "$") { i += 1; continue; }

    if (src.startsWith("$$", i)) {
      open = open === "$$" ? "" : "$$";
      i += 2;
      continue;
    }
    if (open === "$$") { i += 1; continue; }          // 块级里单个 $ 只是正文
    if (open === "$") { open = ""; i += 1; continue; }
    // 行内公式体里不能有换行也没有 $（md 的规则），所以往后看一个 $ 是不是在当前行内
    const nl = src.indexOf("\n", i + 1);
    const close = src.indexOf("$", i + 1);
    if (close >= 0 && (nl < 0 || close < nl)) { i = close + 1; continue; }
    open = "$";
    i += 1;
  }
  if (fence) return src + "\n```";
  if (inline) return src + "`";
  return open ? src + open : src;
}

// 表格：GFM 的「表头行 + |---|---| 分隔行」。逐行扫比一条大正则好读，也好改。
// 前后补空行，让它独占一个段落（否则会被后面跟进来的正文并进同一个 <p>）。
function mdTables(s) {
  const lines = s.split("\n");
  const out = [];
  const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const isRow = (t) => /^\s*\|.*\|\s*$/.test(t);
  const isDelim = (t) => /^\s*\|[\s:|-]+\|\s*$/.test(t) && /-/.test(t);
  for (let i = 0; i < lines.length; i++) {
    if (isRow(lines[i]) && i + 1 < lines.length && isDelim(lines[i + 1])) {
      let html = "<table><thead><tr>" +
        cells(lines[i]).map((c) => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>";
      i += 2;
      while (i < lines.length && isRow(lines[i])) {
        html += "<tr>" + cells(lines[i]).map((c) => "<td>" + c + "</td>").join("") + "</tr>";
        i++;
      }
      i--;
      out.push("", html + "</tbody></table>", "");
      continue;
    }
    out.push(lines[i]);
  }
  return out.join("\n");
}

// 列表：逐行扫，按缩进用栈做嵌套（写死两级不够用，大纲式回答常有三层）。
// 子列表要开在父 <li> **闭合之前**，所以 <li> 的收尾是延迟的（pend 标记）。
// 「列表紧接着正文」按列表结束处理，正文自己成段 —— 不实现 GFM 的 lazy continuation。
function mdLists(s) {
  const lines = s.split("\n");
  const out = [];
  const item = /^([ \t]*)([-*+]|\d+\.)[ \t]+(.*)$/;
  const stack = [];                  // [{tag, indent, pend}]
  // 「有没有没闭合的 <li>」必须**按层记**：从三层缩进一次退回根层时，每一层的 <li> 都要收，
  // 用一个全局标记只收得掉最里面那层，剩下的标签就不配平了。
  const closeLi = () => {
    const t = stack[stack.length - 1];
    if (t && t.pend) { out.push("</li>"); t.pend = false; }
  };
  const closeAll = () => {
    const had = stack.length > 0;
    while (stack.length) { closeLi(); out.push("</" + stack.pop().tag + ">"); }
    if (had) out.push("");
  };
  const top = () => stack[stack.length - 1];
  for (const line of lines) {
    const m = line.match(item);
    if (!m) { closeAll(); out.push(line); continue; }
    const indent = m[1].replace(/\t/g, "    ").length;
    const tag = /\d/.test(m[2]) ? "ol" : "ul";
    while (stack.length && indent < top().indent) { closeLi(); out.push("</" + stack.pop().tag + ">"); }
    if (!stack.length) { out.push("", "<" + tag + ">"); stack.push({ tag, indent, pend: false }); }
    else if (indent > top().indent) { out.push("<" + tag + ">"); stack.push({ tag, indent, pend: false }); }
    else if (tag !== top().tag) {
      closeLi(); out.push("</" + stack.pop().tag + ">", "<" + tag + ">");
      stack.push({ tag, indent, pend: false });
    } else { closeLi(); }
    out.push("<li>" + m[3]);
    top().pend = true;
  }
  closeAll();
  return out.join("\n");
}

// 极简 markdown（离线，不引 CDN）。
//
// 顺序是这个函数的关键：**代码块和行内代码必须先抠成占位符**。以前它们是随手在中间
// 替换掉的，于是后面的规则能穿进代码里 —— 代码块里的空行会被段落规则插进 </p><p>
// 把 <pre> 劈开，`$PATH` 会被当成行内公式渲染成 KaTeX。抠出来时顺手 escapeHtml
// （占位符要到最后一刻才还原，赶不上中间那道统一转义）。
//
// XSS 不变量：模型给的文字全部经过 escapeHtml；例外只有 KaTeX 自己的输出、和上面
// 已经转义过的代码。新增任何渲染分支都必须放在 escapeHtml() 之后。
function md(src) {
  const blocks = [];                 // 块级代码
  const inlines = [];                // 行内代码
  const math = [];
  let s = String(src == null ? "" : src);

  // 1) 块级代码。info string（```python 里的 python）剥掉不显示。
  s = s.replace(/```([^\n]*)\n?([\s\S]*?)```/g, (_, info, code) => {
    blocks.push("<pre><code>" + escapeHtml(code.replace(/^\n/, "")) + "</code></pre>");
    return "\n\n\u0000F" + (blocks.length - 1) + "\u0000\n\n";
  });
  // 2) 行内代码
  s = s.replace(/`([^`\n]+)`/g, (_, code) => {
    inlines.push("<code>" + escapeHtml(code) + "</code>");
    return "\u0000I" + (inlines.length - 1) + "\u0000";
  });
  // 3) 公式（$$ 在前，否则 $$ 会被当成两个空的 $...$）
  s = s.replace(/\$\$([\s\S]+?)\$\$/g, (_, tex) => {
    math.push({ display: true, tex });
    return "\u0000K" + (math.length - 1) + "\u0000";
  });
  s = s.replace(/\$([^$\n]+?)\$/g, (_, tex) => {
    math.push({ display: false, tex });
    return "\u0000K" + (math.length - 1) + "\u0000";
  });

  s = escapeHtml(s);

  // 4) 块级结构
  s = mdTables(s);
  s = mdLists(s);
  s = s.replace(/(?:^&gt; ?.*(?:\n|$))+/gm, (m) =>
    "<blockquote>" + m.replace(/\n$/, "").replace(/^&gt; ?/gm, "").replace(/\n/g, "<br>") + "</blockquote>");
  s = s.replace(/^(?:-{3,}|\*{3,}|_{3,})[ \t]*$/gm, "<hr>");
  s = s.replace(/^#### (.*)$/gm, "<h4>$1</h4>");
  s = s.replace(/^### (.*)$/gm, "<h3>$1</h3>");
  s = s.replace(/^## (.*)$/gm, "<h2>$1</h2>");
  s = s.replace(/^# (.*)$/gm, "<h1>$1</h1>");

  // 5) 行内。下划线**故意不做**斜体：ICS 课里 page_table、max_retrieve 这类标识符满篇
  //    都是，认 _ 会把它们拆得七零八落。
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  // URL 里不放引号和尖括号，协议只认这几种 —— 两道一起挡属性注入。
  // 被拒的链接原样留着（此时参数已经过 escapeHtml，是纯文本，不会变成标签）。
  s = s.replace(/\[([^\]\n]*)\]\(([^)\s"'<>]+)\)/g, (m, text, url) =>
    /^(?:https?:|mailto:|#|\/)/i.test(url)
      ? '<a href="' + url + '" target="_blank" rel="noreferrer noopener">' + text + "</a>"
      : m);

  // 6) 段落
  s = s.replace(/^\n+|\n+$/g, "");              // 前后的空行会让 <p> 挂空
  s = s.replace(/\n{2,}/g, "</p><p>");
  s = "<p>" + s + "</p>";
  // 块级元素不该待在 <p> 里：浏览器会提前闭合 <p>，标签就配不平了
  s = s.replace(/<p>(<(?:ul|ol|pre|h[1-4]|table|blockquote|hr)\b)/g, "$1");
  s = s.replace(/<p>(\u0000F\d+\u0000)<\/p>/g, "$1");
  s = s.replace(/(<\/(?:ul|ol|pre|h[1-4]|table|blockquote)>|<hr>|\u0000F\d+\u0000)<\/p>/g, "$1");

  // 7) 还原（放最后：之后没有任何渲染分支，转义过的内容不会再有被解释的机会）
  s = s.replace(/\u0000K(\d+)\u0000/g, (_, i) => renderTex(math[+i].tex, math[+i].display));
  s = s.replace(/\u0000I(\d+)\u0000/g, (_, i) => inlines[+i]);
  s = s.replace(/\u0000F(\d+)\u0000/g, (_, i) => blocks[+i]);
  return s;
}
// 用户往上翻的时候别把他拽回底部 —— 只有本来就贴着底才跟着滚
function nearBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 60;
}

// 剪贴板：WebView2 下 navigator.clipboard 通常可用；浏览器直开非安全上下文时退回
// 老式的 textarea + execCommand。两条路都失败就返回 false，按钮自己显示「复制失败」。
async function copyText(text) {
  const value = String(text || "");
  if (!value) return false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch (e) { /* 落到下面的退路 */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = value;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch (e) {
    return false;
  }
}

// 助手消息右上角的「复制」。取的是回答原文（带 Markdown / LaTeX），不是渲染后的
// textContent —— 后者会把公式压成 Unicode、把代码块的缩进吃掉。
function addCopyButton(el, getText) {
  const tools = document.createElement("div");
  tools.className = "msg-tools";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "msg-copy";
  btn.textContent = "复制";
  btn.title = "复制回答原文";
  btn.addEventListener("click", async () => {
    const ok = await copyText(getText());
    btn.textContent = ok ? "已复制" : "复制失败";
    setTimeout(() => { btn.textContent = "复制"; }, 1200);
  });
  tools.appendChild(btn);
  el.appendChild(tools);
}

function addMessage(role, text) {
  const stick = nearBottom();
  const el = document.createElement("div");
  el.className = "msg " + role;
  // __raw 是这轮回答的 Markdown 原文。流式期间先留空，收尾时由 send() 填上；
  // 复制按钮始终读它，所以中途不会复制到渲染了一半的 DOM。
  el.__raw = String(text || "");
  if (role === "assistant") {
    const inner = document.createElement("div");
    inner.className = "markdown";
    inner.innerHTML = md(text || "");
    el.appendChild(inner);
    addCopyButton(el, () => el.__raw);
  } else {
    el.textContent = text;
  }
  messagesEl.appendChild(el);
  if (stick) messagesEl.scrollTop = messagesEl.scrollHeight;
  return el;
}

function stoppedMark() {
  const mark = document.createElement("span");
  mark.className = "msg-stopped";
  mark.textContent = "（已停止，以上是已收到的部分）";
  return mark;
}

// ------------------------------------------------------------------ 课程 / 资料
function itemById(id) {
  return items.find((x) => x.id === id) || null;
}

function docById(id) {
  const it = itemById(id);
  return it && it.kind === "doc" ? it : null;
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

function fmtLast(last) {
  if (!last) return "还没有读过";
  const item = itemById(last.lecture) || courses.find((c) => c.id === last.lecture);
  const title = last.title || (item && item.title) || "上次的位置";
  const page = last.number || last.page;
  return "上次读到「" + title + "」" + (page ? " · 第 " + page + " 页" : "");
}

// ------------------------------------------------------------------ 首页
function renderHome() {
  const box = $("#course-cards");
  box.innerHTML = "";
  $("#home-empty").hidden = courses.length > 0;
  for (let i = 0; i < courses.length; i++) {
    const c = courses[i];
    const card = document.createElement("article");
    card.className = "course-card";
    card.tabIndex = 0;
    card.dataset.course = c.id;
    card.style.animationDelay = `${i * 45}ms`;

    const head = document.createElement("div");
    head.className = "course-card-head";

    const iconWrap = document.createElement("div");
    iconWrap.className = "course-card-icon " + (c.kind || "files");
    if (c.kind === "ics") {
      iconWrap.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="20" rx="4"/><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 7h5M17 17h5"/></svg>';
    } else if (c.kind === "repo") {
      iconWrap.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>';
    } else {
      iconWrap.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>';
    }

    const titleWrap = document.createElement("div");
    titleWrap.className = "course-title-wrap";
    const title = document.createElement("h3");
    title.textContent = c.title || c.id;
    titleWrap.appendChild(title);

    const badge = document.createElement("span");
    badge.className = "course-badge kind-" + (c.kind || "files");
    badge.textContent = c.kind === "ics" ? "课件" : (c.kind === "repo" ? "代码仓库" : "资料");
    titleWrap.appendChild(badge);

    head.append(iconWrap, titleWrap);

    const meta = document.createElement("div");
    meta.className = "course-meta";
    meta.innerHTML = `<span class="meta-item">${c.item_count} 项</span>` +
      `<span class="meta-dot">·</span><span class="meta-item">${c.page_count} 页</span>` +
      `<span class="meta-dot">·</span><span class="meta-item">${fmtSize((c.size || 0) + (c.source_size || 0))}</span>`;

    const last = document.createElement("div");
    last.className = "course-last";
    const lastStr = fmtLast(c.last);
    last.innerHTML = `<svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor" class="last-icon"><path d="M8 3.5a.5.5 0 0 0-1 0V9a.5.5 0 0 0 .252.434l3.5 2a.5.5 0 0 0 .496-.868L8 8.71V3.5z"/><path d="M8 16A8 8 0 1 0 8 0a8 8 0 0 0 0 16zm7-8A7 7 0 1 1 1 8a7 7 0 0 1 14 0z"/></svg><span>${escapeHtml(lastStr)}</span>`;

    const actions = document.createElement("div");
    actions.className = "course-actions";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "primary btn-card-open";
    open.textContent = c.last ? "继续阅读" : "打开";
    open.addEventListener("click", (e) => {
      e.stopPropagation();
      openCourse(c.id);
    });
    actions.appendChild(open);

    if (c.kind !== "ics") {
      const upd = document.createElement("button");
      upd.type = "button";
      upd.className = "btn-card-action";
      upd.textContent = "更新";
      upd.addEventListener("click", async (e) => {
        e.stopPropagation();
        await updateCourse(c.id);
      });
      actions.appendChild(upd);
    }
    if (c.deletable) {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "danger btn-card-action";
      del.textContent = "删除";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        deleteCourse(c);
      });
      actions.appendChild(del);
    }

    card.append(head, meta, last, actions);
    card.addEventListener("click", () => openCourse(c.id));
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openCourse(c.id); }
    });
    box.appendChild(card);
  }
}

async function loadCourses() {
  try {
    const data = await api("/api/courses");
    courses = data.courses || [];
  } catch (e) {
    courses = [];
    statusEl.textContent = "读取课程失败：" + e.message;
  }
  renderHome();
  return courses;
}

function showView(v) {
  view = v;
  const isHome = v === "home";
  const homeEl = $("#home-view");
  const workEl = $("#work-view");
  const topbar = $(".topbar");

  if (topbar) topbar.hidden = isHome;
  homeEl.hidden = !isHome;
  workEl.hidden = isHome;

  if (isHome) {
    homeEl.classList.remove("view-fade-in");
    void homeEl.offsetWidth;
    homeEl.classList.add("view-fade-in");
    $("#course-title").hidden = true;
    $("#btn-add-course").hidden = false;
  } else {
    workEl.classList.remove("view-fade-in");
    void workEl.offsetWidth;
    workEl.classList.add("view-fade-in");
    $("#course-title").hidden = false;
    $("#btn-add-course").hidden = true;
  }
}

function showHome() {
  hideDeckLoading();
  showView("home");
  currentCourse = null;
  items = [];
  icsLectures = [];
  documents = [];
  lastProgress = "";
  loadCourses();
}

// 重建讲次/资料下拉框。preferred 是重建后想继续选中的 id（不传就沿用当前值）。
// 重建 <select> 会把 value 掉回第一项，所以必须显式恢复，否则更新一跑完正在读的人
// 就被弹回第一讲第一页。
function rebuildSelect(preferred) {
  const prev = preferred !== undefined ? preferred : select.value;
  lectureVid = {};
  select.innerHTML = "";
  if (!items.length) {
    select.innerHTML = '<option value="">（这门课还没有内容）</option>';
    lastPick.ics = "";
    lastPick.docs = "";
    return;
  }
  for (const item of items) {
    const o = document.createElement("option");
    o.value = item.id;
    const isDoc = item.kind === "doc";
    o.textContent = item.title + "（" + (isDoc ? item.page_count + " 页" : "课件") + "）";
    select.appendChild(o);
    if (!isDoc) lectureVid[item.id] = item.vid || item.id;
  }
  if (items.some((x) => x.id === prev)) select.value = prev;
  const it = itemById(select.value);
  if (it) lastPick[it.kind === "doc" ? "docs" : "ics"] = select.value;
}

function refreshModeUI() {
  const it = itemById(select.value);
  const docs = !!(it && it.kind === "doc");
  const kind = currentCourse ? currentCourse.kind : "ics";
  // 任何已打开的课程都能继续加资料，ICS 也不例外（上传的 PDF 会挂在这门课下面）。
  $("#btn-upload").hidden = !currentCourse;
  $("#btn-delete-doc").hidden = !docs;
  $("#btn-update").hidden = !currentCourse || (kind === "files" && it && it.kind === "doc");
  $("#btn-export").hidden = !(currentCourse && kind === "ics" && it && it.kind !== "doc");
  scopeEl.options[0].textContent = docs ? "本资料" : "本讲";
  if (scopeEl.options[1]) {
    scopeEl.options[1].textContent = "本课程";
    scopeEl.options[1].hidden = false;
    scopeEl.options[1].disabled = false;
  }
}

function setCourseMode(m, preferredId) {
  courseMode = m === "docs" ? "docs" : "ics";
  if (courseSel) courseSel.value = courseMode;
  rebuildSelect(preferredId);
  refreshModeUI();
}

function openSelected() {
  const it = itemById(select.value);
  if (!it) return;
  if (it.kind === "doc") openDocument(select.value);
  else openLecture(select.value);
}

// 打开课程：先取课程详情，再按进度恢复到最后读的 item / 页码。
async function openCourse(cid, opts = {}) {
  const card = [...document.querySelectorAll(".course-card")]
    .find((c) => c.dataset.course === cid);
  if (card) card.classList.add("is-loading");
  let data;
  try {
    data = await api("/api/course?course=" + encodeURIComponent(cid));
  } catch (e) {
    statusEl.textContent = "打开课程失败：" + e.message;
    return;
  } finally {
    if (card) card.classList.remove("is-loading");
  }
  if (!data || data.error) {
    statusEl.textContent = (data && data.error) || "课程不存在";
    await loadCourses();
    return;
  }
  currentCourse = data.course || { id: cid, title: cid, kind: "files" };
  icsLectures = data.lectures || [];
  documents = data.documents || [];
  items = icsLectures.concat(documents);
  const kind = currentCourse.kind || "ics";
  if (kind === "ics") items.sort((a, b) => a.id.localeCompare(b.id, "zh"));
  else items.sort((a, b) => (b.added || 0) - (a.added || 0));

  showView("work");
  $("#course-title").textContent = currentCourse.title || "";
  $("#course-title").title = currentCourse.title || "";
  loadedLecture = loadedDoc = null;
  current = null;
  lastProgress = "";

  const last = data.last || null;
  const pick = opts.item || (last && last.lecture) || (items[0] && items[0].id) || "";
  rebuildSelect(pick);
  if (!pick) {
    hideDeckLoading();
    viewer.src = "about:blank";
    emptyEl.textContent = kind === "ics"
      ? "还没有渲染课件。点「更新」或到首页导入仓库。"
      : "这门课还没有内容，点「上传资料」或重新导入。";
    emptyEl.style.display = "flex";
    resetChat();
    refreshModeUI();
    return;
  }
  const startPage = opts.page ||
    (last && last.lecture === pick ? { number: last.number, index: last.index,
                                       page: last.page } : null);
  openItem(pick, startPage);
}

// 兼容旧调用：prefer 可以是 {mode,id}，也可以直接给 item id
async function loadCourse(prefer) {
  if (!currentCourse) { await loadCourses(); showHome(); return; }
  const id = typeof prefer === "string" ? prefer : (prefer && prefer.id) || "";
  await openCourse(currentCourse.id, { item: id || undefined });
}

function openItem(id, start) {
  const it = itemById(id);
  if (!it) return;
  courseMode = it.kind === "doc" ? "docs" : "ics";
  if (courseSel) courseSel.value = courseMode;
  select.value = id;
  lastPick[courseMode] = id;
  refreshModeUI();
  if (it.kind === "doc") openDocument(id, start && (start.number || start));
  else openLecture(id, start);
}

let loadedLecture = null;        // 课件区里现在装着哪一讲
let loadedDoc = null;            // 资料区里现在装着哪一份

function openLecture(id, start) {
  const changed = id !== loadedLecture;
  loadedLecture = id;
  loadedDoc = null;
  if (!id) {
    hideDeckLoading();
    viewer.src = "about:blank";
    current = null;
    emptyEl.textContent = "还没有课件，点「更新」渲染";
    emptyEl.style.display = "flex";
    resetChat();
    return;
  }
  const it = itemById(id);
  const base = (it && it.base) || ("/lectures/" + id + "/");
  // 恢复进度：把页码写进 lecturekit 的 sessionStorage 键，外壳加载时会读它
  if (start && start.index != null) {
    saveIcsPosition(id, start.index, start.page || "");
  }
  emptyEl.style.display = "none";
  showDeckLoading(changed ? "正在打开课件…" : "正在刷新课件…");
  viewer.src = base.replace(/\/?$/, "/") + "index.html";
  current = current && current.lecture === id ? current : null;
  // 对话按讲分，换了讲就不能把上一讲的聊天记录留在眼前。
  // 但同一讲重载（更新跑完、保存设置）不算换讲 —— 那会把正在进行的对话也抹掉。
  if (changed) resetChat();
}

function openDocument(id, page) {
  const changed = id !== loadedDoc;
  // 同一份资料重载（更新跑完）时留在当前页；docview 自己会把页码报回来。
  const keep = (!changed && current && current.number) || null;
  loadedDoc = id;
  loadedLecture = null;
  if (!id) {
    hideDeckLoading();
    viewer.src = "about:blank";
    current = null;
    emptyEl.textContent = documents.length
      ? "请选择一份资料"
      : "还没有上传资料，点「上传资料」添加 PDF、PPTX 或 Markdown";
    emptyEl.style.display = "flex";
    resetChat();
    return;
  }
  emptyEl.style.display = "none";
  const start = page || keep;
  showDeckLoading(changed ? "正在加载资料…" : "正在重新加载资料…");
  viewer.src = "docview.html?doc=" + encodeURIComponent(id) +
    (start ? "&page=" + encodeURIComponent(start) : "");
  if (changed) { current = null; resetChat(); }
}

// 阅读进度：轮询当前页，变化时防抖写回服务端；刷新/重启后能接着读。
let lastProgress = "";
function trackProgress() {
  if (view !== "work" || !currentCourse || !select.value) return;
  const p = currentPage();
  if (!p) return;
  const key = select.value + "|" + p.page;
  if (key === lastProgress) return;
  lastProgress = key;
  const it = itemById(select.value) || {};
  api("/api/progress", {
    method: "POST",
    body: {
      course: currentCourse.id, lecture: select.value, page: p.page,
      number: p.number, index: p.index, title: p.title || it.title || "",
      total: it.page_count || 0,
    },
  }).catch(() => {});
}

// 读进两层 iframe：外壳(index.html) → .slide-frame(slides.html)
function innerFrame() {
  let doc;
  try { doc = viewer.contentDocument; } catch (e) { return null; }
  if (!doc) return null;
  try { return doc.querySelector(".slide-frame"); } catch (e) { return null; }
}

function innerDoc() {
  const f = innerFrame();
  if (!f) return null;
  try { return f.contentDocument; } catch (e) { return null; }
}

// 从内嵌的课件阅读器里读出「当前正在看哪一页」
// 外壳用 #N 定位第 N 张幻灯片，与 lecture.json 的 pages[N-1] 一一对应。
function currentPage() {
  // 资料阅读器是单层 iframe，直接把页码挂在 window.__reader 上（同源，可读）。
  // 页面滚动/翻页时它还会 postMessage 一份，见下面的 message 监听。
  if (courseMode === "docs") {
    try {
      const s = viewer.contentWindow && viewer.contentWindow.__reader;
      if (s) {
        return { page: s.page, title: s.title || "", number: s.number, index: s.index };
      }
    } catch (e) { /* 还在加载 */ }
    return null;
  }
  let doc;
  try { doc = viewer.contentDocument; } catch (e) { return null; }
  if (!doc) return null;
  const slide = doc.querySelector(".slide-frame");
  if (!slide) return null; // 还在目录页
  let n;
  try { n = parseInt(slide.contentWindow.location.hash.replace(/^#/, ""), 10); } catch (e) { return null; }
  if (isNaN(n)) return null;
  let data;
  try { data = JSON.parse(doc.getElementById("lecture-data").textContent); } catch (e) { return null; }
  const page = data.pages && data.pages[n - 1];
  if (!page) return null;
  return { page: page.id, title: page.title, number: page.number || n, index: n - 1 };
}

// 提问 / 引用要立刻用到当前页，所以用之前先现读一遍（不再靠轮询维护）
function livePage() {
  const p = currentPage();
  if (p) current = { lecture: select.value, ...p };
  return current;
}

// ------------------------------------------------------------------ 跳页
// 外壳把阅读位置存在 sessionStorage（键 lecturekit:viewer:<外壳讲id>）并在载入时恢复，
// 所以跳页 = 改写这个键 + 让外壳重载；已经在看幻灯片时直接改内层 hash 更快。
function saveIcsPosition(lid, index, pageId) {
  const vid = lectureVid[lid] || lid;
  const key = "lecturekit:viewer:" + vid;
  let st = {};
  try { st = JSON.parse(sessionStorage.getItem(key) || "{}") || {}; } catch (e) { /* 无存储 */ }
  st.mode = "slide";
  st.currentPageId = pageId;
  try { sessionStorage.setItem(key, JSON.stringify(st)); } catch (e) { /* 无存储 */ }
}

async function jumpTo(lid, index, pageId, kind, courseId) {
  // 结果可能来自另一门课：先把那门课打开，再定位。
  if (courseId && (!currentCourse || currentCourse.id !== courseId)) {
    await openCourse(courseId, { item: lid });
  }
  const target = kind === "doc" ? "docs" : "ics";
  if (target !== courseMode) {
    setCourseMode(target, lid);
    if (target === "docs") {
      openDocument(lid, index + 1);
    } else {
      saveIcsPosition(lid, index, pageId);
      openLecture(lid);         // 外壳 restoreState() 会直接落到这一页
    }
    return;
  }

  if (courseMode === "docs") {
    if (select.value !== lid) {
      select.value = lid;
      lastPick.docs = lid;
      openDocument(lid, index + 1);
    } else {
      try { viewer.contentWindow.goTo(index + 1); }
      catch (e) { openDocument(lid, index + 1); }
    }
    return;
  }

  saveIcsPosition(lid, index, pageId);
  if (select.value !== lid) {
    select.value = lid;
    lastPick.ics = lid;
    openLecture(lid);           // 外壳 restoreState() 会直接落到这一页
    return;
  }
  const frame = innerFrame();
  if (frame) frame.src = "slides.html#" + (index + 1);
  else openLecture(lid);
}

// ------------------------------------------------------------------ 模式切换
function setMode(m) {
  mode = m;
  for (const t of document.querySelectorAll(".tab[data-mode]")) {
    t.classList.toggle("active", t.dataset.mode === m);
  }
  const isSearch = m === "search";
  messagesEl.hidden = isSearch;
  resultsEl.hidden = !isSearch;
  scopeEl.hidden = !isSearch;
  $("#question").placeholder = isSearch ? "搜索关键词" : "问问AI助手";
}

// ------------------------------------------------------------------ 检索
async function doSearch() {
  const q = $("#question").value.trim();
  if (!q) return;
  resultsEl.innerHTML = '<div class="result-empty">搜索中…</div>';
  let r;
  try {
    r = await api("/api/search", {
      method: "POST",
      body: {
        query: q, scope: scopeEl.value, lecture: select.value,
        course: currentCourse ? currentCourse.id : "",
      },
    });
  } catch (e) {
    resultsEl.innerHTML = '<div class="result-empty">搜索失败：' + escapeHtml(e.message) + "</div>";
    return;
  }
  renderResults(r.results || [], q);
}

function renderResults(list, q) {
  resultsEl.innerHTML = "";
  if (!list.length) {
    resultsEl.innerHTML = '<div class="result-empty">没有找到「' + escapeHtml(q) + "」</div>";
    return;
  }
  const head = document.createElement("div");
  head.className = "result-head";
  head.textContent = "找到 " + list.length + " 处（点击跳转）";
  resultsEl.appendChild(head);

  for (const r of list) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "result";
    const meta = document.createElement("div");
    meta.className = "result-meta";
    const tag = document.createElement("span");
    tag.className = "result-tag";
    tag.textContent = "第 " + (r.page_number || "?") + " 页";
    const crs = courses.find((c) => c.id === r.course_id);
    if (crs && (!currentCourse || crs.id !== currentCourse.id)) {
      const ctag = document.createElement("span");
      ctag.className = "result-tag course";
      ctag.textContent = crs.title;
      meta.append(tag, ctag);
    } else {
      meta.append(tag);
    }
    meta.append(document.createTextNode(r.lecture_title || r.lecture));
    const title = document.createElement("div");
    title.className = "result-title";
    title.textContent = r.page_title || r.page;
    row.append(meta, title);
    if (r.snippet) {
      const sn = document.createElement("div");
      sn.className = "result-snippet";
      sn.textContent = r.snippet;
      row.appendChild(sn);
    }
    row.addEventListener("click", () =>
      jumpTo(r.lecture, r.index, r.page, r.kind, r.course_id));
    resultsEl.appendChild(row);
  }
}

// ------------------------------------------------------------------ 聊天
let streaming = null;              // 正在飞的流：{ctl, cid}；非空时发送键变「停止」

// 一帧的正文：后端发的是 "data: {...}\n\n"
function parseFrame(frame) {
  for (const line of frame.split("\n")) {
    if (!line.startsWith("data:")) continue;
    const body = line.slice(5).trim();
    if (!body) continue;
    try { return JSON.parse(body); } catch (e) { return null; }
  }
  return null;
}

function setSending(on) {
  const btn = $("#btn-send");
  btn.classList.toggle("stop", on);
  btn.textContent = on ? "■" : "↑";
  btn.setAttribute("aria-label", on ? "停止" : "发送");
}

async function send() {
  const q = $("#question").value.trim();
  if (!q) return;
  if (!select.value) {
    addMessage("error", courseMode === "docs"
      ? "还没有上传资料，先点「上传资料」添加 PDF 或 PPTX。"
      : "还没有可用的课件，先点右上角「更新」渲染。");
    return;
  }
  if (streaming) return;                       // 正在回答，别叠着发

  const p = livePage();
  const lid = select.value;
  const conv = ensureConv();
  const cid = conv.id;
  // 发给模型的是**本轮之前**的内容。本轮的课件上下文由后端按当前页现拼，不进历史 ——
  // 存进去的话用户一翻页就会拿着旧页的全文去问。
  const history = conv.messages.map((m) => ({ role: m.role, content: m.content }));

  addMessage("user", q);
  $("#question").value = "";
  if (typeof clearCodeRef === "function") clearCodeRef();
  conv.messages.push({ role: "user", content: q, page: p ? p.page : null });

  const el = addMessage("assistant", "");
  el.classList.add("streaming");
  const inner = el.querySelector(".markdown");

  const ctl = new AbortController();
  streaming = { ctl, cid };
  setSending(true);

  let answer = "";
  let sawStream = false;                       // 收到过响应头 —— 后端从这一刻起才开始记这轮
  let stopped = false;
  let failed = null;
  let stick = true;
  let timer = null;
  let lastPaint = 0;

  // 每来一小段就把整段重跑一遍 md()。KaTeX 有记忆化，markdown 正则本身是亚毫秒级，
  // 所以真正的成本是 layout —— 用 70ms 的节流压住，done 时再来一次收尾。
  function paint(force) {
    if (!force) {
      const now = performance.now();
      if (now - lastPaint < 70) {
        if (!timer) timer = setTimeout(() => { timer = null; paint(true); }, 70);
        return;
      }
    }
    lastPaint = performance.now();
    stick = nearBottom();
    inner.innerHTML = md(balanced(answer));
    if (stick) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: ctl.signal,
      body: JSON.stringify({
        lecture: lid, page: p ? p.page : null, question: q, cid, history,
        course: currentCourse ? currentCourse.id : "",
      }),
    });
    if (!res.ok || !res.body) {
      // 后端在发响应头之前就失败了（没填 key、地址不对、401…），回的是普通 JSON
      let msg = "HTTP " + res.status;
      try {
        const j = await res.json();
        if (j && j.error) msg = j.error;
      } catch (e) { /* 不是 JSON 就用状态码 */ }
      throw new Error(msg);
    }
    sawStream = true;

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;                         // 一定要读到 EOF：title 帧在 done 之后才发
      buf += dec.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const ev = parseFrame(buf.slice(0, cut));
        buf = buf.slice(cut + 2);
        if (!ev) continue;
        if (ev.type === "meta") {
          const label = ev.route === "course" ? "全课程检索"
            : ev.route === "code" ? "代码联动" : "";
          if (label) {
            const badge = document.createElement("span");
            badge.className = "route-badge " + ev.route;
            badge.textContent = label;
            el.insertBefore(badge, inner);
          }
        } else if (ev.type === "delta") { answer += ev.content; paint(false); }
        else if (ev.type === "error") throw new Error(ev.error);
        else if (ev.type === "title") {
          conv.title = ev.title;
          if (!historyPop.hidden) renderHistory();
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") stopped = true;
    else failed = e;
  } finally {
    if (timer) clearTimeout(timer);
    if (failed) {
      // 这轮后端什么都没记（它是在发响应头之后才开始记的），本地也退回原样，
      // 否则界面和磁盘上的历史会对不上
      if (!sawStream) conv.messages.pop();
      el.remove();
      addMessage("error", "出错了：" + failed.message);
    } else {
      // 收尾用原文而不是 balanced() 补过的：真闭合符没到就不该在最后留下一个假的
      inner.innerHTML = md(answer || (stopped ? "" : "（空回答）"));
      el.__raw = answer;                 // 复制按钮取原文（Markdown / LaTeX 都保留）
      if (stick) messagesEl.scrollTop = messagesEl.scrollHeight;
      el.classList.remove("streaming");
      if (answer) {
        const msg = { role: "assistant", content: answer };
        if (stopped) { msg.stopped = true; el.appendChild(stoppedMark()); }
        conv.messages.push(msg);
      } else if (stopped) {
        el.remove();                           // 一个字都没收到，别留个空气泡
        if (!sawStream) conv.messages.pop();
      }
    }
    streaming = null;
    setSending(false);
    $("#question").focus();
  }
}

// ------------------------------------------------------------------ 历史
// 对话按讲分开：切换讲次时整个换一份列表，上一讲的记录不该留在眼前。
// 「当前对话」只活在内存里，每次提问把它之前的内容当历史发给后端；后端那边才是真源。
let currentConv = null;            // {id, title, messages[]}；null = 还没开始的新对话
let historyCache = [];
const historyPop = $("#history-pop");

function newId() {
  // 对话 id 由前端生成：第一帧到达之前界面就得把它建好并选中
  try {
    return crypto.randomUUID();
  } catch (e) {
    return "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  }
}

function ensureConv() {
  if (!currentConv) currentConv = { id: newId(), title: "", messages: [] };
  return currentConv;
}

function showConversation(conv) {
  messagesEl.innerHTML = "";
  currentConv = {
    id: conv.id,
    title: conv.title || "",
    messages: (conv.messages || []).slice(),
  };
  for (const m of currentConv.messages) {
    if (m.role === "user") addMessage("user", m.content);
    else {
      const el = addMessage("assistant", m.content);
      if (m.stopped) el.appendChild(stoppedMark());
    }
  }
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// 切讲时调用。在飞的流要掐掉 —— 后端会把已经收到的部分存下来，不会丢。
function resetChat() {
  if (streaming) streaming.ctl.abort();
  currentConv = null;
  messagesEl.innerHTML = "";
  if (!historyPop.hidden) loadHistoryList().then(renderHistory);
}

function relTime(ts) {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "刚刚";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前";
  if (s < 86400 * 30) return Math.floor(s / 86400) + " 天前";
  return new Date(ts * 1000).toLocaleDateString();
}

async function loadHistoryList() {
  if (!select.value) { historyCache = []; return; }
  try {
    const r = await api("/api/history/list", {
      method: "POST",
      body: { lecture: select.value },
    });
    historyCache = r.conversations || [];
  } catch (e) {
    historyCache = [];
  }
}

function renderHistory() {
  const box = $("#history-list");
  box.innerHTML = "";
  if (!historyCache.length) {
    box.innerHTML = '<div class="hist-empty">' +
      (courseMode === "docs" ? "这份资料还没有对话" : "这一讲还没有对话") + "</div>";
    return;
  }
  for (const c of historyCache) {
    const row = document.createElement("div");
    row.className = "hist-row" + (currentConv && currentConv.id === c.id ? " active" : "");

    const text = document.createElement("div");
    text.className = "hist-text";
    const title = document.createElement("div");
    title.className = "hist-title";
    title.textContent = c.title || "新对话";
    const time = document.createElement("div");
    time.className = "hist-time";
    time.textContent = relTime(c.updated) + (c.count ? " · " + c.count + " 条" : "");
    text.append(title, time);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "hist-del";
    del.textContent = "×";
    del.setAttribute("aria-label", "删除这条对话");
    del.addEventListener("click", (e) => { e.stopPropagation(); removeConversation(c.id); });

    row.append(text, del);
    row.addEventListener("click", () => openConversation(c.id));
    box.appendChild(row);
  }
}

async function openConversation(id) {
  try {
    const r = await api("/api/history/get", {
      method: "POST",
      body: { lecture: select.value, id },
    });
    if (!r.conversation) {          // 列表过期了（比如在别处删过），刷新一下
      await loadHistoryList();
      renderHistory();
      return;
    }
    showConversation(r.conversation);
    closeHistory();
  } catch (e) { /* 读不到就维持现状 */ }
}

async function removeConversation(id) {
  try {
    await api("/api/history/delete", {
      method: "POST",
      body: { lecture: select.value, id },
    });
  } catch (e) { /* 删不掉就当没删 */ }
  if (currentConv && currentConv.id === id) {
    currentConv = null;
    messagesEl.innerHTML = "";
  }
  await loadHistoryList();
  renderHistory();
}

function openHistory() {
  historyPop.hidden = false;
  loadHistoryList().then(renderHistory);
}

function closeHistory() {
  historyPop.hidden = true;
}

// ------------------------------------------------------------------ 划词引用
function quoteLabel() {
  const title = select.options[select.selectedIndex]
    ? select.options[select.selectedIndex].textContent.replace(/（.*?）$/, "")
    : select.value;
  const p = livePage();
  return "[" + title + " 第" + (p ? p.number : "?") + "页]";
}

// 公式是 MathJax 预渲染的 SVG，选不中；但 data-c 属性里存了字符码点，
// 解出来是可读的（上下标会压平，如 10¹ 变 101 —— 配合页面原文的 $...$ 足够用）。
function texFromTarget(node) {
  let el = node;
  while (el && el.nodeType === 1) {
    if (el.tagName && el.tagName.toLowerCase() === "mjx-container") {
      let text = "";
      for (const p of el.querySelectorAll("[data-c]")) {
        try { text += String.fromCodePoint(parseInt(p.getAttribute("data-c"), 16)); }
        catch (e) { /* 跳过坏码点 */ }
      }
      return text.trim();
    }
    el = el.parentNode;
  }
  return "";
}

function openCtxMenu(x, y, text) {
  pendingQuote = text;
  ctxMenu.hidden = false;
  const r = ctxMenu.getBoundingClientRect();
  const left = Math.min(x, window.innerWidth - r.width - 8);
  const top = Math.min(y, window.innerHeight - r.height - 8);
  ctxMenu.style.left = Math.max(8, left) + "px";
  ctxMenu.style.top = Math.max(8, top) + "px";
}

function closeCtxMenu() {
  ctxMenu.hidden = true;
  pendingQuote = null;
}

// 换讲或重载都会把内层文档整个重建，所以要反复确认监听还在；两个标志位防重复挂。
function ensureQuoteInjection() {
  // 资料阅读器只有一层 iframe，直接挂在外层文档上。左右翻页由 docview 自己处理，
  // 这里只接划词菜单（引用 / 解释 / 复制），让资料页也能像 ICS 课件一样提问。
  if (courseMode === "docs") {
    let doc;
    try { doc = viewer.contentDocument; } catch (e) { return; }
    if (!doc || !doc.body) return;
    if (!doc.__quoteHooked) {
      doc.__quoteHooked = true;
      doc.addEventListener("contextmenu", onInnerContextMenu);
      doc.addEventListener("mouseup", onInnerMouseUp);
    }
    return;
  }

  const doc = innerDoc();
  if (!doc || !doc.body) return;
  if (!doc.__quoteHooked) {
    doc.__quoteHooked = true;
    doc.addEventListener("contextmenu", onInnerContextMenu);
    doc.addEventListener("mouseup", onInnerMouseUp);
  }
  const frame = innerFrame();
  if (frame && !frame.__quoteHooked) {
    frame.__quoteHooked = true;
    // 内层自己导航时立刻补挂，不用等下一次轮询（它已经把 flag 置上了，不会无限递归）
    frame.addEventListener("load", () => { try { ensureQuoteInjection(); } catch (e) { /* 忽略 */ } });
  }
}

// 内层视口坐标 → 外层窗口坐标（两层 iframe 的 rect 相加）
function toWindowXY(e) {
  try {
    const sr = viewer.getBoundingClientRect();
    const frame = innerFrame();
    const dr = frame ? frame.getBoundingClientRect() : { left: 0, top: 0 };
    return { x: sr.left + dr.left + e.clientX, y: sr.top + dr.top + e.clientY };
  } catch (err) {
    return { x: e.clientX, y: e.clientY };
  }
}

function innerSelection(doc) {
  try {
    const s = doc.getSelection();
    if (!s) return "";
    let text = String(s).trim();
    // WebView2 里，由脚本塞进去的 range 会让 Selection.toString() 返回空串（range 本身
    // 是有文字的）。真人拖选两条路结果一样，这里只是留个退路。
    if (!text && s.rangeCount) text = String(s.getRangeAt(0)).trim();
    return text;
  } catch (err) {
    return "";
  }
}

// 划词：松开左键就弹菜单，选区空了（在别处点一下）就收起来。
// iframe 是独立的浏览上下文，事件不会冒泡到外层 document —— 「点一下消失」只能在这儿做。
function onInnerMouseUp(e) {
  if (e.button !== 0) return;          // 右键交给 contextmenu
  const text = innerSelection(e.currentTarget);
  if (!text) { closeCtxMenu(); return; }
  const { x, y } = toWindowXY(e);
  // 稍微错开一点，别让指针正好压在第一个按钮上
  openCtxMenu(x + 2, y + 8, text);
}

function onInnerContextMenu(e) {
  const text = innerSelection(e.currentTarget) || texFromTarget(e.target);
  if (!text) return;          // 没有可引用的内容，放行系统右键菜单

  e.preventDefault();
  const { x, y } = toWindowXY(e);
  openCtxMenu(x, y, text);
}

ctxMenu.addEventListener("click", (e) => {
  const act = e.target.dataset ? e.target.dataset.act : null;
  if (!act) return;
  const text = pendingQuote;
  closeCtxMenu();
  if (!text) return;

  if (act === "copy") {
    navigator.clipboard.writeText(text).catch(() => {});
    return;
  }
  const quoted = "> " + quoteLabel() + " " + text.replace(/\n/g, "\n> ") + "\n\n";
  const box = $("#question");
  if (act === "quote") {
    box.value = quoted + box.value;
    if (mode !== "ask") setMode("ask");
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
    return;
  }
  if (act === "explain") {
    if (mode !== "ask") setMode("ask");
    box.value = quoted + "请解释这段话。";
    send();
  }
});

document.addEventListener("mousedown", (e) => {
  if (!ctxMenu.hidden && !ctxMenu.contains(e.target)) closeCtxMenu();
});
window.addEventListener("blur", closeCtxMenu);
// 内层翻页/滚动后关掉菜单
document.addEventListener("scroll", closeCtxMenu, true);

// ------------------------------------------------------------------ 导出 PDF
let exportTimer = null;

async function exportPdf() {
  // 只有 ICS 的渲染产物有 marp 导出链路；仓库课件和上传资料没有
  if (!currentCourse || currentCourse.kind !== "ics") return;
  const lid = select.value;
  if (!lid) { statusEl.textContent = "先选一讲"; return; }
  const opt = select.options[select.selectedIndex];
  const title = opt ? opt.textContent.replace(/（.*?）$/, "") : lid;

  let dest = "";
  const bridge = window.pywebview && window.pywebview.api;
  if (bridge && bridge.save_pdf_dialog) {
    dest = await bridge.save_pdf_dialog(title + ".pdf");
    if (!dest) return;                        // 用户取消
  }                                          // 否则留空：后端落到 exe 旁的 exports/

  statusEl.textContent = "正在导出…";
  const r = await api("/api/export_pdf", { method: "POST", body: { lecture: lid, dest } });
  if (r.error) { statusEl.textContent = "导出失败：" + r.error; return; }
  if (!r.started) { statusEl.textContent = "已有导出在进行"; return; }
  watchExport();
}

function watchExport() {
  if (exportTimer) clearInterval(exportTimer);
  $("#btn-export").disabled = true;
  exportTimer = setInterval(async () => {
    const st = await api("/api/export/status");
    const last = st.lines && st.lines.length ? st.lines[st.lines.length - 1] : "";
    statusEl.textContent = st.running ? "导出中：" + last : (st.error ? "导出失败" : "已导出");
    if (st.running) return;
    clearInterval(exportTimer);
    exportTimer = null;
    $("#btn-export").disabled = false;
    if (st.error) {
      if (mode !== "ask") setMode("ask");
      addMessage("error", "导出失败：" + st.error);
    } else if (st.result && st.result.path) {
      if (mode !== "ask") setMode("ask");
      showExportDone(st.result.path);
    }
  }, 1200);
}

function showExportDone(path) {
  const el = document.createElement("div");
  el.className = "msg assistant";
  const inner = document.createElement("div");
  inner.className = "markdown";
  const p = document.createElement("p");
  p.textContent = "PDF 已导出：";
  const code = document.createElement("code");
  code.textContent = path;
  const p2 = document.createElement("p");
  p2.appendChild(code);
  inner.append(p, p2);
  el.appendChild(inner);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = "打开文件夹";
  btn.addEventListener("click", () => api("/api/reveal", { method: "POST", body: { path } }));
  el.appendChild(btn);

  messagesEl.appendChild(el);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

// ------------------------------------------------------------------ 添加课程 / 导入资料
let pendingFiles = [];         // [{file} | {path, name}]
let importTab = "files";

function openImport(tab) {
  importTab = tab || "files";
  for (const t of document.querySelectorAll("[data-import-tab]")) {
    t.classList.toggle("active", t.dataset.importTab === importTab);
  }
  // 从课程里点「上传资料」时，留空就是加到这门课；从首页「添加课程」进来才是
  // 新建课程 / 放进「我的资料」。文案要说清楚落点，免得用户以为又建了一门。
  const inCourse = view === "work" && !!currentCourse;
  $("#import-title").textContent = inCourse ? "添加课件" : "添加课程";
  $("#import-file-course").placeholder = inCourse
    ? "留空则放进「" + (currentCourse.title || "当前课程") + "」"
    : "留空则放进「我的资料」";
  $("#import-files").hidden = importTab !== "files";
  $("#import-repo").hidden = importTab !== "repo";
  $("#repo-log").hidden = true;
  $("#repo-log").textContent = "";
  $("#btn-import-start").disabled = false;
  $("#import-modal").showModal();
}

function setImportTab(tab) {
  importTab = tab;
  for (const t of document.querySelectorAll("[data-import-tab]")) {
    t.classList.toggle("active", t.dataset.importTab === tab);
  }
  $("#import-files").hidden = tab !== "files";
  $("#import-repo").hidden = tab !== "repo";
}

function addPendingFiles(list) {
  const known = new Set(pendingFiles.map((f) => f.name));
  for (const f of list || []) {
    if (!f || !f.name || known.has(f.name)) continue;
    if (!/\.(pdf|pptx|md)$/i.test(f.name)) continue;
    known.add(f.name);
    pendingFiles.push(f);
  }
  renderPendingFiles();
}

function renderPendingFiles() {
  const box = $("#import-file-list");
  box.innerHTML = "";
  for (let i = 0; i < pendingFiles.length; i++) {
    const f = pendingFiles[i];
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = f.name;
    const del = document.createElement("button");
    del.type = "button";
    del.className = "ghost";
    del.textContent = "×";
    del.addEventListener("click", () => {
      pendingFiles.splice(i, 1);
      renderPendingFiles();
    });
    li.append(name, del);
    box.appendChild(li);
  }
}

async function pickFiles() {
  const bridge = window.pywebview && window.pywebview.api;
  if (bridge && bridge.pick_documents) {
    let paths = [];
    try { paths = await bridge.pick_documents(); } catch (e) { paths = []; }
    if (typeof paths === "string") paths = [paths];
    addPendingFiles((paths || []).map((p) => ({
      path: p, name: String(p).split(/[\\/]/).pop(),
    })));
    return;
  }
  if (bridge && bridge.pick_document) {
    let path = null;
    try { path = await bridge.pick_document(); } catch (e) { path = null; }
    if (path) addPendingFiles([{ path, name: String(path).split(/[\\/]/).pop() }]);
    return;
  }
  fileInput.click();
}

// 上传落点：填了课程名 → 按名字找/建课程；否则留在当前课程；
// 首页直接上传则交给服务端放进「我的资料」。
function importTarget() {
  const title = $("#import-file-course").value.trim();
  if (title) return { title, course: "" };
  if (currentCourse && view === "work") {
    return { title: "", course: currentCourse.id };
  }
  return { title: "", course: "" };
}

async function uploadOne(file, target) {
  if (file.path) {
    // 桌面版：只给路径，文件不经过 JS，免得大 PDF 占内存
    const body = { path: file.path };
    if (target.course) body.course = target.course;
    else if (target.title) body.title = target.title;
    const r = await api("/api/doc/upload", { method: "POST", body });
    if (r.error) throw new Error(r.error);
    return r;
  }
  const qs = new URLSearchParams();
  if (target.course) qs.set("course", target.course);
  else if (target.title) qs.set("title", target.title);
  // 不能走 api()：它会 JSON.stringify，二进制必须原样发。
  // 浏览器给的 File 本身就是 Blob；原生选择器给的是 {path, name}，不会走到这里。
  const blob = file.file || file;
  const name = file.name || blob.name || "upload";
  const res = await fetch("/api/doc/upload?" + qs.toString(), {
    method: "POST",
    headers: {
      "Content-Type": "application/octet-stream",
      "X-Filename": encodeURIComponent(name),
    },
    body: blob,
  });
  let r = {};
  try { r = await res.json(); } catch (e) { /* 非 JSON 用状态码 */ }
  if (!res.ok || r.error) throw new Error(r.error || ("HTTP " + res.status));
  return r;
}

async function startImport() {
  if (importTab === "repo") return startRepoImport();
  if (!pendingFiles.length) {
    $("#import-file-status").textContent = "先选择文件（PDF / PPTX / Markdown）。";
    return;
  }
  const target = importTarget();
  const status = $("#import-file-status");
  status.textContent = "正在导入…（PPTX 需要调用 PowerPoint/WPS，可能要几十秒）";
  $("#btn-import-start").disabled = true;
  let cid = target.course;
  let title = target.title;
  let done = 0;
  const errors = [];
  for (const f of pendingFiles) {
    status.textContent = "正在导入 " + (done + 1) + "/" + pendingFiles.length +
      "：" + f.name + "…";
    try {
      const r = await uploadOne(f, { course: cid, title });
      cid = r.course_id || cid;      // 第一份新建课程后，后面的都进同一门课
      title = "";                    // 避免每份都再建一门课
      done++;
    } catch (e) {
      errors.push(f.name + "：" + e.message);
    }
  }
  status.textContent = "完成：" + done + "/" + pendingFiles.length +
    (errors.length ? "；失败 " + errors.join("；") : "");
  $("#btn-import-start").disabled = false;
  pendingFiles = [];
  renderPendingFiles();
  await loadCourses();
  if (cid) await openCourse(cid);
  if (!errors.length) $("#import-modal").close();
}

async function scanRepo() {
  const source = $("#import-repo-source").value.trim();
  const box = $("#repo-scan-result");
  const warn = $("#repo-warning");
  if (!source) { box.hidden = false; box.textContent = "先填路径或 Git 地址。"; return; }
  box.hidden = false;
  box.textContent = "正在扫描…";
  warn.hidden = true;
  try {
    const info = await api("/api/course/scan", { method: "POST", body: { source } });
    if (info.error) { box.textContent = "扫描失败：" + info.error; return; }
    if (info.remote) {
      box.textContent = "远端仓库：导入后会浅克隆并扫描。";
      return;
    }
    box.textContent = "课件 " + (info.lectures || []).length + " 个 · 资料 " +
      (info.decks || []).length + " 份 · 代码根 " +
      (info.code_roots || []).length + " 个" +
      (info.is_git ? " · Git 仓库" : "");
    warn.hidden = !(info.lectures || []).length;
  } catch (e) {
    box.textContent = "扫描失败：" + e.message;
  }
}

async function startRepoImport() {
  const source = $("#import-repo-source").value.trim();
  if (!source) { $("#repo-scan-result").hidden = false; $("#repo-scan-result").textContent = "先填路径或 Git 地址。"; return; }
  const r = await api("/api/course/import", {
    method: "POST",
    body: {
      type: "repo", source,
      title: $("#import-repo-title").value.trim(),
      with_decks: $("#import-with-decks").checked,
    },
  });
  if (r.error) {
    $("#repo-scan-result").hidden = false;
    $("#repo-scan-result").textContent = "导入失败：" + r.error;
    return;
  }
  const log = $("#repo-log");
  log.hidden = false;
  log.textContent = "正在导入…";
  $("#btn-import-start").disabled = true;
  watchCourseJob(log);
}

// 后台课程任务（导入 / 更新）轮询。日志写进给定元素，完成后刷新首页。
// openAfter 为 false 时只刷新当前课程，不因为一次「更新」把用户从首页拽进课程。
function watchCourseJob(logEl, openAfter = true) {
  if (courseJobTimer) clearInterval(courseJobTimer);
  courseJobTimer = setInterval(async () => {
    let st;
    try { st = await api("/api/course/job"); } catch (e) { return; }
    const lines = st.lines || [];
    if (logEl) {
      logEl.textContent = lines.join("\n") || "…";
      logEl.scrollTop = logEl.scrollHeight;
    }
    statusEl.textContent = st.running ? "任务进行中：" + (lines[lines.length - 1] || "")
      : (st.error ? "任务失败" : "任务完成");
    if (st.running) return;
    clearInterval(courseJobTimer);
    courseJobTimer = null;
    $("#btn-import-start").disabled = false;
    await loadCourses();
    if (st.error) {
      if (logEl) logEl.textContent += "\n" + st.error;
      return;
    }
    const done = st.result || {};
    if (openAfter && done.course_id) {
      $("#import-modal").close();
      await openCourse(done.course_id);
    } else if (view === "work" && currentCourse) {
      await openCourse(currentCourse.id, { item: select.value });
    }
  }, 1000);
}

async function deleteDocument() {
  if (courseMode !== "docs" || !select.value) return;
  const d = docById(select.value);
  const title = d ? d.title : "这份资料";
  if (!window.confirm("删除《" + title + "》？\n它的页面、阅读进度、标注和对话历史会一起删除。")) return;
  const r = await api("/api/doc/delete", { method: "POST", body: { id: select.value } });
  if (r.error) { statusEl.textContent = "删除失败：" + r.error; return; }
  statusEl.textContent = "已删除";
  if (currentCourse) await openCourse(currentCourse.id);
  await loadCourses();
}

async function updateCourse(cid) {
  const r = await api("/api/course/update", { method: "POST", body: { course: cid } });
  if (r.error) { statusEl.textContent = "更新失败：" + r.error; return; }
  statusEl.textContent = "更新已启动…";
  watchCourseJob($("#repo-log"), false);
}

async function deleteCourse(card) {
  if (!card || !card.deletable) return;
  if (!window.confirm("删除课程《" + card.title + "》？\n课程里的资料、索引、进度和对话都会删除。")) return;
  const r = await api("/api/course/delete", {
    method: "POST", body: { course: card.id },
  });
  if (r.error) { statusEl.textContent = "删除失败：" + r.error; return; }
  statusEl.textContent = "课程已删除";
  if (currentCourse && currentCourse.id === card.id) showHome();
  else await loadCourses();
}

// ------------------------------------------------------------------ 更新
// ICS 走原来的 /api/update（更新作业在 update_job 里），不能套用课程作业的轮询。
let icsUpdateTimer = null;
function watchUpdate() {
  $("#btn-update").disabled = true;
  if (icsUpdateTimer) clearInterval(icsUpdateTimer);
  icsUpdateTimer = setInterval(async () => {
    let st;
    try { st = await api("/api/update/status"); } catch (e) { return; }
    const lines = st.lines || [];
    statusEl.textContent = st.running
      ? "更新中：" + (lines[lines.length - 1] || "")
      : (st.error ? "更新失败" : "更新完成");
    if (st.running) return;
    clearInterval(icsUpdateTimer);
    icsUpdateTimer = null;
    $("#btn-update").disabled = false;
    if (st.error) return;
    await loadCourses();
    if (currentCourse && currentCourse.id === "ics") {
      await openCourse("ics", { item: select.value });
    }
  }, 1000);
}

async function triggerUpdate() {
  if (!currentCourse) return;
  if (currentCourse.id === "ics") {
    // ICS 仍走原来的整仓渲染链路（配置里有 repo 路径和 lecture.py 环境）
    const r = await api("/api/update", { method: "POST" });
    if (r.error) { statusEl.textContent = "更新失败：" + r.error; return; }
    if (!r.started) { statusEl.textContent = "已有更新在进行"; return; }
  } else {
    const r = await api("/api/course/update", {
      method: "POST", body: { course: currentCourse.id },
    });
    if (r.error) { statusEl.textContent = "更新失败：" + r.error; return; }
  }
  statusEl.textContent = "更新已启动…";
  $("#btn-update").disabled = true;
  watchUpdate();
}

// ------------------------------------------------------------------ 向导
let installTimer = null;

async function openWizard() {
  const w = await api("/api/wizard");
  renderWizard(w.items || []);
  $("#wizard-modal").showModal();
}

function renderWizard(items) {
  const list = $("#wizard-list");
  list.innerHTML = "";
  for (const it of items) {
    const row = document.createElement("div");
    row.className = "wizard-row " + (it.ok ? "ok" : (it.skip ? "skip" : "bad"));

    const mark = document.createElement("span");
    mark.className = "wizard-mark";
    mark.textContent = it.ok ? "✓" : (it.skip ? "–" : "✗");

    const body = document.createElement("div");
    body.className = "wizard-body";
    const name = document.createElement("div");
    name.className = "wizard-name";
    name.textContent = it.label;
    const detail = document.createElement("div");
    detail.className = "wizard-detail";
    detail.textContent = it.detail || "";
    body.append(name, detail);
    row.append(mark, body);

    if (!it.ok && !it.skip && it.action) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = it.action.kind === "clone" ? "选目录并克隆" : "安装";
      btn.addEventListener("click", () => runInstall(it.key, btn));
      row.appendChild(btn);
    }
    list.appendChild(row);
  }
}

async function runInstall(key, btn) {
  let dir = null;
  if (key === "repo") {
    const bridge = window.pywebview && window.pywebview.api;
    if (bridge && bridge.pick_directory) dir = await bridge.pick_directory();
    if (!dir) dir = prompt("把课程仓库克隆到哪里？请输入完整路径：");
    if (!dir) return;
  }
  btn.disabled = true;
  const r = await api("/api/wizard/install", { method: "POST", body: { key, dir } });
  if (r.error) { btn.disabled = false; statusEl.textContent = "安装失败：" + r.error; return; }
  if (!r.started) { btn.disabled = false; return; }
  watchInstall();
}

function watchInstall() {
  if (installTimer) clearInterval(installTimer);
  const logEl = $("#wizard-log");
  logEl.hidden = false;
  logEl.textContent = "正在安装…";
  installTimer = setInterval(async () => {
    const st = await api("/api/wizard/status");
    logEl.textContent = (st.lines || []).join("\n") || "…";
    logEl.scrollTop = logEl.scrollHeight;
    if (st.running) return;
    clearInterval(installTimer);
    installTimer = null;
    if (st.error) logEl.textContent += "\n" + st.error;
    const w = await api("/api/wizard");
    renderWizard(w.items || []);
  }, 1200);
}

// ------------------------------------------------------------------ 界面偏好
// 主题和侧栏宽度都存在 config.json 里 —— pywebview 默认 private_mode，cookies 和
// localStorage 都不保留，存那边等于没存。首绘用的主题见 index.html 的内联脚本。
const media = window.matchMedia("(prefers-color-scheme: dark)");
const MIN_CHAT_W = 280;
let prefs = { theme: "system", chat_width: 440 };
let chatW = 440;

function applyTheme() {
  const t = prefs.theme === "light" || prefs.theme === "dark"
    ? prefs.theme
    : (media.matches ? "dark" : "light");
  document.documentElement.dataset.theme = t;
  syncViewerTheme();
}

// iframe 有自己独立的 :root，不会继承父页面的 data-theme。资料阅读器用 postMessage
// 接主题；ICS 的嵌套阅读器忽略这条消息即可。
function syncViewerTheme() {
  try {
    const t = document.documentElement.dataset.theme || "light";
    viewer.contentWindow.postMessage({ type: "theme", theme: t }, "*");
  } catch (e) { /* iframe 还没就绪 */ }
}

// 选了「跟随系统」才理系统，强制了就别跟
function onSystemThemeChange() {
  if (prefs.theme !== "light" && prefs.theme !== "dark") applyTheme();
}
if (media.addEventListener) media.addEventListener("change", onSystemThemeChange);
else if (media.addListener) media.addListener(onSystemThemeChange);

function maxChatW() { return Math.max(MIN_CHAT_W, Math.round(window.innerWidth * 0.55)); }

function applyChatWidth(w) {
  const n = Math.round(Number(w));
  // 只挡「根本没有值」。负数是合法的：指针拖到窗口右边以外时宽度算出来就是负的，
  // 那种情况要夹到下限，不能让侧栏卡在原地不动。
  if (!isFinite(n)) return 0;
  chatW = Math.min(Math.max(n, MIN_CHAT_W), maxChatW());
  document.documentElement.style.setProperty("--chat-w", chatW + "px");
  return chatW;
}

async function loadPrefs() {
  try {
    const c = await api("/api/config");
    prefs.theme = c.theme || "system";
    prefs.chat_width = c.chat_width || 440;
  } catch (e) { /* 读不到就按默认值来 */ }
  applyTheme();
  applyChatWidth(prefs.chat_width);
}

// ------------------------------------------------------------------ 设置
async function openSettings() {
  const c = await api("/api/config");
  $("#cfg-base").value = c.api_base || "";
  $("#cfg-model").value = c.model || "";
  $("#cfg-key").value = c.api_key || "";
  $("#cfg-autoupdate").checked = !!c.auto_update;
  $("#cfg-theme").value = c.theme || "system";
  // 上一轮探测的残留别留到这一轮，免得看起来像是刚测出来的
  $("#test-result").hidden = true;
  $("#model-picker").hidden = true;
  $("#settings-modal").showModal();
}

// 测试连接 / 列出模型都用输入框里的「当前值」，不必先保存
function probeBody() {
  return {
    api_base: $("#cfg-base").value.trim(),
    model: $("#cfg-model").value.trim(),
    api_key: $("#cfg-key").value.trim(),
  };
}

async function saveSettings() {
  prefs.theme = $("#cfg-theme").value;
  await api("/api/config", {
    method: "POST",
    body: {
      api_base: $("#cfg-base").value.trim(),
      model: $("#cfg-model").value.trim(),
      api_key: $("#cfg-key").value.trim(),
      auto_update: $("#cfg-autoupdate").checked,
      theme: prefs.theme,
    },
  });
  applyTheme();
  $("#settings-modal").close();
  // 这里不重读课程列表：设置里能改的五项都不影响课程，而 loadCourse() 会重建
  // <select> 并把课件区导航回第一讲，读着课件进来改个设置就被弹回去。
}

// ------------------------------------------------------------------ 绑定
courseSel.addEventListener("change", () => {
  const m = courseSel.value;
  setCourseMode(m, lastPick[m] || "");
  openSelected();
});
select.addEventListener("change", () => {
  const it = itemById(select.value);
  if (it) courseMode = it.kind === "doc" ? "docs" : "ics";
  lastPick[courseMode] = select.value;
  refreshModeUI();
  openSelected();
});
$("#btn-home").addEventListener("click", showHome);
$("#btn-add-course").addEventListener("click", () => openImport("files"));
$("#btn-upload").addEventListener("click", () => openImport("files"));
$("#btn-delete-doc").addEventListener("click", deleteDocument);
for (const t of document.querySelectorAll("[data-import-tab]")) {
  t.addEventListener("click", () => setImportTab(t.dataset.importTab));
}
$("#btn-pick-files").addEventListener("click", pickFiles);
$("#btn-import-cancel").addEventListener("click", () => $("#import-modal").close());
$("#btn-import-start").addEventListener("click", startImport);
$("#btn-repo-scan").addEventListener("click", scanRepo);
$("#drop-zone").addEventListener("dragover", (e) => {
  e.preventDefault();
  $("#drop-zone").classList.add("over");
});
$("#drop-zone").addEventListener("dragleave", () => $("#drop-zone").classList.remove("over"));
$("#drop-zone").addEventListener("drop", (e) => {
  e.preventDefault();
  $("#drop-zone").classList.remove("over");
  addPendingFiles(Array.from((e.dataTransfer && e.dataTransfer.files) || []));
});
fileInput.addEventListener("change", () => {
  addPendingFiles(Array.from(fileInput.files || []));
  fileInput.value = "";          // 清掉，才能连续选同一个文件
});
// 资料阅读器翻页后把当前页报上来。currentPage() 也能直接读 window.__reader，
// 这条消息主要是让「网页刚加载完、__reader 还没赋值」的空档也有值。
window.addEventListener("message", (e) => {
  const d = e.data;
  if (!d || typeof d !== "object" || d.type !== "doc-page") return;
  if (courseMode !== "docs" || d.doc !== select.value) return;
  hideDeckLoading();
  current = {
    lecture: select.value,
    page: d.page,
    title: d.title || "",
    number: d.number,
    index: d.index,
  };
});
$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (streaming) { streaming.ctl.abort(); return; }   // 流式期间这个键是「停止」
  if (mode === "search") doSearch();
  else send();
});
$("#question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (streaming) return;                            // 停不下来，等它自己结束
    if (mode === "search") doSearch();
    else send();
  }
});
// 历史浮层
$("#btn-history").addEventListener("click", () => {
  if (historyPop.hidden) openHistory(); else closeHistory();
});
$("#btn-new-conv").addEventListener("click", () => {
  currentConv = null;
  messagesEl.innerHTML = "";
  closeHistory();
});
$("#history-pop").addEventListener("mousedown", (e) => e.stopPropagation());
// 点浮层外面收起来。contextmenu 那块也挂了 mousedown，两者互不影响
document.addEventListener("mousedown", (e) => {
  if (historyPop.hidden) return;
  if (e.target === $("#btn-history")) return;
  closeHistory();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !historyPop.hidden) closeHistory();
});
for (const t of document.querySelectorAll(".tab[data-mode]")) {
  t.addEventListener("click", () => setMode(t.dataset.mode));
}
$("#btn-update").addEventListener("click", triggerUpdate);
$("#btn-export").addEventListener("click", exportPdf);
$("#btn-settings").addEventListener("click", openSettings);
const btnHomeSettings = $("#btn-home-settings");
if (btnHomeSettings) btnHomeSettings.addEventListener("click", openSettings);
$("#btn-save").addEventListener("click", saveSettings);
$("#btn-cancel").addEventListener("click", () => $("#settings-modal").close());
$("#btn-wizard-open").addEventListener("click", () => { $("#settings-modal").close(); openWizard(); });
// 后端根本没能拼出候选地址时（没填 base / 没填 key），urls 是缺的 —— 那时候还报
// 「尝试的地址：(没填 Base URL)」只会把人带偏，比如明明只是 key 没填
function triedLine(urls) {
  return urls && urls.length ? "\n尝试的地址：" + urls.join("\n            ") : "";
}
$("#btn-test-api").addEventListener("click", async () => {
  const el = $("#test-result");
  const btn = $("#btn-test-api");
  el.hidden = false;
  el.className = "test-result";
  el.textContent = "正在测试…";
  btn.disabled = true;
  try {
    const r = await api("/api/test_api", { method: "POST", body: probeBody() });
    el.className = "test-result " + (r.ok ? "ok" : "bad");
    el.textContent = (r.ok ? "✓ " + r.detail : "✗ " + r.error) + triedLine(r.urls);
  } catch (e) {
    el.className = "test-result bad";
    el.textContent = "✗ 测试请求本身失败了：" + e.message;
  } finally {
    btn.disabled = false;
  }
});
// 中转站的模型名和官方不一样，还常带自己的后缀。列出来让人点，比让他猜一个强。
$("#btn-list-models").addEventListener("click", async () => {
  const el = $("#test-result");
  const picker = $("#model-picker");
  const btn = $("#btn-list-models");
  el.hidden = false;
  picker.hidden = true;
  el.className = "test-result";
  el.textContent = "正在取模型列表…";
  btn.disabled = true;
  try {
    const r = await api("/api/models", { method: "POST", body: probeBody() });
    if (!r.ok) {
      el.className = "test-result bad";
      el.textContent = "✗ " + r.error + triedLine(r.urls);
      return;
    }
    el.className = "test-result ok";
    el.textContent = "共 " + r.models.length + " 个模型，点一个填进「模型」框：";
    picker.innerHTML = "";
    const cur = $("#cfg-model").value.trim();
    for (const id of r.models) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "model-chip" + (id === cur ? " current" : "");
      b.textContent = id;
      b.addEventListener("click", () => {
        $("#cfg-model").value = id;
        for (const c of picker.children) c.classList.toggle("current", c === b);
      });
      picker.appendChild(b);
    }
    picker.hidden = false;
  } catch (e) {
    el.className = "test-result bad";
    el.textContent = "✗ 取模型列表的请求本身失败了：" + e.message;
  } finally {
    btn.disabled = false;
  }
});
$("#btn-wizard-close").addEventListener("click", () => $("#wizard-modal").close());
$("#btn-wizard-redetect").addEventListener("click", () => {
  $("#wizard-log").hidden = true;
  openWizard();
});
$("#btn-wizard-dismiss").addEventListener("click", async () => {
  const w = await api("/api/wizard");
  const cur = await api("/api/config");
  const merged = Array.from(new Set([].concat(cur.wizard_dismissed || [], w.missing || [])));
  await api("/api/config", { method: "POST", body: { wizard_dismissed: merged } });
  $("#wizard-modal").close();
});

// ------------------------------------------------------------------ 侧栏拖拽
// 侧栏贴着窗口右边，所以宽度 = 窗口右边到指针的距离。拖动期间只改 CSS 变量，
// 松手才写 config.json（免得一次拖动发几十个请求）。
function dragWidth(e) { return window.innerWidth - e.clientX; }

resizer.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  e.preventDefault();
  resizer.setPointerCapture(e.pointerId);
  document.body.classList.add("resizing");
});
resizer.addEventListener("pointermove", (e) => {
  if (!resizer.hasPointerCapture(e.pointerId)) return;
  applyChatWidth(dragWidth(e));
});
function endResize(e) {
  if (!resizer.hasPointerCapture(e.pointerId)) return;
  resizer.releasePointerCapture(e.pointerId);
  document.body.classList.remove("resizing");
  const w = applyChatWidth(dragWidth(e));
  if (w) api("/api/config", { method: "POST", body: { chat_width: w } }).catch(() => {});
}
resizer.addEventListener("pointerup", endResize);
// 指针被系统收走（Alt+Tab、切窗口）时不会再有 pointerup，而且捕获已经自动释放 ——
// 走 endResize 会被 hasPointerCapture 挡回来，body 上的锁就再也解不掉了
resizer.addEventListener("pointercancel", () => document.body.classList.remove("resizing"));
// 窗口被拉窄时把记住的宽度夹回来，否则侧栏会把课件挤没
window.addEventListener("resize", () => applyChatWidth(chatW));

setMode("ask");
showView("home");
loadPrefs();
setInterval(ensureQuoteInjection, 600);
setInterval(trackProgress, 1200);
// 外壳重载后 .slide-frame 要等 viewer.js 跑完才建出来，光靠轮询会漏掉刚载入的头几百毫秒
viewer.addEventListener("load", () => {
  if (loadedDoc) {
    // 资料阅读器的 load 只说明外壳 HTML 到了，第一页还在路上。等它 postMessage
    // 报页（会提前收起遮罩）；万一读失败，900ms 后也必须把遮罩撤掉。
    setTimeout(hideDeckLoading, 900);
  } else {
    hideDeckLoading();
  }
  syncViewerTheme();
  for (const t of [0, 200, 600]) setTimeout(ensureQuoteInjection, t);
});
// 焦点在外层（比如刚点过顶栏）时，方向键也转发给资料阅读器。输入框里的键不抢。
document.addEventListener("keydown", (e) => {
  if (courseMode !== "docs") return;
  const t = e.target;
  if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
  if (!["ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End", " "].includes(e.key)) return;
  e.preventDefault();
  try { viewer.contentWindow.postMessage({ type: "doc-key", key: e.key }, "*"); }
  catch (err) { /* iframe 未就绪 */ }
});
loadCourses().then(async () => {
  // 启动时环境不齐就直接把向导摆出来（用户点过「不再提示」的缺失项不算）
  try {
    const w = await api("/api/wizard");
    if (w.should_prompt) {
      renderWizard(w.items || []);
      $("#wizard-modal").showModal();
    }
  } catch (e) { /* 检测失败不打扰 */ }
});
