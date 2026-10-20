"use strict";

// 上传资料阅读器：PDF / PPTX / Markdown。
//
// 三种阅读方式：
//   scroll  连续阅读（默认）——所有页自上而下排开，滚轮直接往下翻；
//   single  单页——滚轮 / 方向键一次一页；
//   tile    缩略图总览，点一张进入连续阅读的对应位置。
//
// PDF 页面是服务端渲染的 PNG，上面盖一层「透明文字」：字符框来自 pypdfium2，
// 位置按页宽/页高的百分比摆放，所以缩放和连续滚动都不会让文字和图片错位。
// 页码通过 window.__reader 和 postMessage 报给外层，AI 助手据此拼上下文。

const $ = (s) => document.querySelector(s);
const params = new URLSearchParams(location.search);
const docId = params.get("doc") || "";

const pageArea = $("#page-area");
const scrollView = $("#scroll-view");
const tileGrid = $("#tile-grid");
const pageBox = $("#page-box");
const pageCanvas = $("#page-canvas");
const pageImg = $("#page-img");
const textLayerEl = $("#text-layer");
const imageNote = $("#image-note");

let meta = null;
let pages = [];
let index = 0;
let mode = "pdf";              // pdf | slide | md
let viewMode = "scroll";       // scroll | single | tile
let zoom = 1;                  // 1 = 适应宽度
let fitW = 900;
let renderSeq = 0;             // 防止慢的旧请求覆盖新页
let singleLayer = null;        // 单页模式的文本层
let scrollParts = new Map();   // i -> {shell, canvas, img, slide, md, text, note, layer, activated}
let scrollObserver = null;
let scrollRaf = 0;
let wheelAt = 0;
let singleIndex = -1;          // 上一次单页渲染的页码，用来决定翻页动画方向

// ------------------------------------------------------------------ 主题
function applyTheme(t) {
  if (t === "light" || t === "dark") {
    document.documentElement.dataset.theme = t;
  }
}
try {
  applyTheme(parent.document.documentElement.dataset.theme);
} catch (e) { /* 直接开这个页面时没有父窗口 */ }
applyTheme(params.get("theme"));

window.addEventListener("message", (e) => {
  const d = e.data;
  if (!d || typeof d !== "object") return;
  if (d.type === "theme") applyTheme(d.theme);
  else if (d.type === "doc-key") handleKey(d.key);
});

// ------------------------------------------------------------------ 加载
async function init() {
  if (!docId) {
    showError("没有指定资料。请回到主界面重新选择。");
    return;
  }
  try {
    const res = await fetch("/api/doc/" + encodeURIComponent(docId) + "/meta");
    if (!res.ok) {
      let msg = "HTTP " + res.status;
      try {
        const j = await res.json();
        if (j && j.error) msg = j.error;
      } catch (e) { /* 不是 JSON */ }
      throw new Error(msg);
    }
    const data = await res.json();
    meta = data.document || {};
    pages = data.pages || [];
    if (!pages.length) throw new Error("这份资料没有可显示的页面");

    mode = meta.mode === "slide" ? "slide" : (meta.mode === "md" ? "md" : "pdf");
    document.title = (meta.title || "资料") + " - ICS-Reader";
    $("#page-input").max = pages.length;

    pageImg.addEventListener("load", () => {
      pageImg.classList.add("is-ready");
      if (viewMode === "single") $("#doc-loading").hidden = true;
      layoutPage();
      layoutTextLayer(pageCanvas, textLayerEl, singleLayer);
    });
    pageImg.addEventListener("error", () => {
      pageImg.classList.remove("is-ready");
      if (viewMode === "single" && !pageImg.hidden) {
        showError("这一页渲染失败。如果是扫描件，文字可能也抽不出来。");
      }
    });

    pageArea.addEventListener("scroll", onScroll, { passive: true });
    pageArea.addEventListener("wheel", onWheel, { passive: false });
    window.addEventListener("resize", () => requestAnimationFrame(layoutPage));

    let start = parseInt(params.get("page") || location.hash.replace(/^#/, ""), 10);
    if (isNaN(start)) start = 1;
    await show(start - 1);
  } catch (e) {
    showError(e.message || String(e));
  }
}

function showError(message) {
  $("#doc-loading").hidden = true;
  const el = $("#doc-error");
  el.textContent = message || "加载失败";
  el.hidden = false;
}

function clearError() {
  $("#doc-error").hidden = true;
}

function pageNumber(i) {
  const p = pages[i];
  return (p && p.number) || (i + 1);
}

function publish() {
  const p = pages[index];
  if (!p) return;
  const number = pageNumber(index);
  window.__reader = {
    doc: docId, page: p.id, index: index, number: number, title: p.title || "",
  };
  try {
    parent.postMessage({
      type: "doc-page", doc: docId, page: p.id, index: index,
      number: number, title: p.title || "",
    }, "*");
  } catch (e) { /* 直接开这个页面时没有父窗口 */ }
}

function setIndex(i, opts) {
  if (!pages.length) return;
  i = Math.max(0, Math.min(pages.length - 1, Number(i) || 0));
  if (i === index && opts && opts.fromScroll) return;
  index = i;
  const number = pageNumber(i);
  $("#page-info").textContent = "第 " + number + " / " + pages.length + " 页";
  $("#page-input").value = number;
  $("#btn-prev").disabled = i === 0;
  $("#btn-next").disabled = i === pages.length - 1;
  clearError();
  publish();
  try {
    history.replaceState(null, "", "?doc=" + encodeURIComponent(docId) + "&page=" + number);
  } catch (e) { /* file:// 之类不支持 */ }
}

async function show(i, opts) {
  opts = opts || {};
  if (!pages.length) return;
  i = Math.max(0, Math.min(pages.length - 1, Number(i) || 0));
  setIndex(i);
  if (viewMode === "single") return renderSingle(i);
  // 连续阅读和缩略图都有自己的骨架占位；中间那层单页遮罩只属于单页模式，
  // 不收起的话它会一直盖在页面上，看起来像永远加载不完。
  $("#doc-loading").hidden = true;
  if (viewMode === "tile") return renderTiles();
  ensureScroll();
  scrollToPage(i, !!opts.smooth);
}

// ------------------------------------------------------------------ 宽度 / 缩放
function computeFitW() {
  const avail = pageArea.clientWidth - 48;     // 页区左右各 24px 内边距
  return Math.max(200, Math.floor(avail));
}

function applyWidths() {
  fitW = computeFitW();
  const w = Math.round(fitW * zoom);
  pageCanvas.style.width = w + "px";
  scrollView.style.setProperty("--page-w", w + "px");
  $("#btn-zoom-fit").textContent = zoom === 1 ? "适应" : Math.round(zoom * 100) + "%";
  return w;
}

function layoutPage() {
  applyWidths();
  if (mode === "slide") {
    if (viewMode === "single") layoutSlide(pageCanvas);
    if (viewMode === "scroll") {
      for (const part of scrollParts.values()) {
        if (part.activated && !part.slide.hidden) layoutSlide(part.canvas);
      }
    }
  }
  if (viewMode === "single") layoutTextLayer(pageCanvas, textLayerEl, singleLayer);
  if (viewMode === "scroll") {
    for (const part of scrollParts.values()) {
      layoutTextLayer(part.canvas, part.text, part.layer);
    }
  }
}

function setZoom(z) {
  zoom = Math.max(0.5, Math.min(3, Math.round(z * 100) / 100));
  layoutPage();
}

// ------------------------------------------------------------------ 连续阅读
function ensureScroll() {
  if (scrollParts.size) {
    scrollView.dataset.mode = mode;
    applyWidths();
    return;
  }
  scrollView.dataset.mode = mode;
  scrollView.innerHTML = "";
  scrollParts.clear();
  if (scrollObserver) scrollObserver.disconnect();

  scrollObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const i = Number(entry.target.dataset.index);
      activateScrollPage(i);
    }
  }, { root: pageArea, rootMargin: "600px 0px" });

  for (let i = 0; i < pages.length; i++) {
    const p = pages[i];
    const number = pageNumber(i);

    const shell = document.createElement("div");
    shell.className = "doc-scroll-page mode-" + mode;
    shell.dataset.index = String(i);

    const canvas = document.createElement("div");
    canvas.className = "doc-page-canvas doc-scroll-canvas";

    const img = document.createElement("img");
    img.className = "doc-canvas-img";
    img.alt = "第 " + number + " 页";
    img.hidden = true;

    const slide = document.createElement("div");
    slide.className = "doc-slide-fit doc-canvas-slide";
    slide.hidden = true;

    const md = document.createElement("div");
    md.className = "doc-md-page doc-canvas-md markdown-body";
    md.hidden = true;

    const text = document.createElement("div");
    text.className = "doc-text-layer";
    text.hidden = true;

    const note = document.createElement("div");
    note.className = "doc-image-note";
    note.textContent = "本页为图片，没有可复制的文字";
    note.hidden = true;

    canvas.append(img, slide, md, text, note);
    shell.appendChild(canvas);
    scrollView.appendChild(shell);

    const part = {
      shell, canvas, img, slide, md, text, note,
      layer: null, activated: false,
    };
    scrollParts.set(i, part);

    img.addEventListener("load", () => {
      const ratio = img.naturalHeight && img.naturalWidth
        ? img.naturalHeight / img.naturalWidth : 0;
      if (ratio && !scrollView.style.getPropertyValue("--page-ratio")) {
        scrollView.style.setProperty("--page-ratio", String(ratio));
      }
      // 图片解码完成再淡入；shell 同时去掉骨架屏
      img.classList.add("is-ready");
      part.shell.classList.remove("is-loading");
      part.shell.classList.add("is-ready");
      layoutTextLayer(part.canvas, part.text, part.layer);
    });
    img.addEventListener("error", () => {
      part.shell.classList.remove("is-loading");
      part.shell.classList.add("is-ready");
      note.textContent = "这一页渲染失败";
      note.hidden = false;
    });

    scrollObserver.observe(shell);
  }
  applyWidths();
}

function activateScrollPage(i) {
  const part = scrollParts.get(i);
  if (!part || part.activated) return;
  part.activated = true;
  const number = pageNumber(i);
  if (mode === "pdf") {
    part.shell.classList.add("is-loading");
    part.img.src = "/api/doc/" + encodeURIComponent(docId) +
      "/page/" + number + ".png?w=1600";
    part.img.hidden = false;
  } else if (mode === "md") {
    fillMarkdown(part.md, i, null);
    part.md.hidden = false;
  } else {
    fillSlide(part.canvas, i, null);
    part.slide.hidden = false;
  }
  if (mode === "pdf") loadPartLayer(part, i);
}

function scrollToPage(i, smooth) {
  const part = scrollParts.get(i);
  if (!part) return;
  activateScrollPage(i);
  // offsetTop 的参照物会随 offsetParent 变化；用两块 rect 相减最稳。
  const top = part.shell.getBoundingClientRect().top -
    pageArea.getBoundingClientRect().top + pageArea.scrollTop;
  pageArea.scrollTo({ top: Math.max(0, top), behavior: smooth ? "smooth" : "auto" });
}

function onScroll() {
  if (viewMode !== "scroll" || scrollRaf) return;
  scrollRaf = requestAnimationFrame(() => {
    scrollRaf = 0;
    pickActivePage();
  });
}

function pickActivePage() {
  if (viewMode !== "scroll" || !scrollParts.size) return;
  const box = pageArea.getBoundingClientRect();
  const top = box.top;
  const bottom = box.bottom;
  let best = index;
  let bestVisible = -1;
  for (const [i, part] of scrollParts) {
    const r = part.shell.getBoundingClientRect();
    const visible = Math.min(r.bottom, bottom) - Math.max(r.top, top);
    if (visible > bestVisible) {
      bestVisible = visible;
      best = i;
    }
  }
  if (bestVisible > 0) setIndex(best, { fromScroll: true });
}

// ------------------------------------------------------------------ 单页
async function renderSingle(i) {
  const seq = ++renderSeq;
  const number = pageNumber(i);
  if (i !== singleIndex) {
    // 单页换页：按方向做一小段位移 + 淡入；首次进入不播动画。
    const dir = singleIndex < 0 ? 0 : (i > singleIndex ? 1 : -1);
    singleIndex = i;
    pageCanvas.classList.remove("turn-next", "turn-prev");
    if (dir) {
      void pageCanvas.offsetWidth;
      pageCanvas.classList.add(dir > 0 ? "turn-next" : "turn-prev");
    }
  }
  pageBox.hidden = false;
  scrollView.hidden = true;
  tileGrid.hidden = true;
  clearError();
  applyWidths();

  if (mode === "pdf") {
    pageImg.hidden = false;
    $("#slide-fit").hidden = true;
    $("#md-page").hidden = true;
    const url = "/api/doc/" + encodeURIComponent(docId) +
      "/page/" + number + ".png?w=1600";
    if (pageImg.dataset.url !== url) {
      pageImg.classList.remove("is-ready");
      $("#doc-loading").hidden = false;
      pageImg.dataset.url = url;
      pageImg.src = url;
    } else {
      $("#doc-loading").hidden = true;
    }
    await loadSingleLayer(number, seq);
  } else if (mode === "md") {
    pageImg.hidden = true;
    $("#slide-fit").hidden = true;
    $("#doc-loading").hidden = true;
    await fillMarkdown($("#md-page"), i, seq);
  } else {
    pageImg.hidden = true;
    $("#md-page").hidden = true;
    $("#doc-loading").hidden = false;
    await fillSlide(pageCanvas, i, seq);
  }
  if (seq !== renderSeq) return;
  // PDF 的图片可能比文字层慢：等 pageImg 的 load 自己收起遮罩，
  // 否则大页面会先露出一块空白。
  if (mode !== "pdf") $("#doc-loading").hidden = true;
  layoutPage();
}

async function fillMarkdown(mdEl, i, seq) {
  if (!mdEl) return;
  mdEl.hidden = false;
  mdEl.innerHTML = '<div class="doc-md-loading">读取中…</div>';
  try {
    const res = await fetch("/api/doc/" + encodeURIComponent(docId) +
      "/text/" + pageNumber(i));
    const data = await res.json();
    if (seq != null && seq !== renderSeq) return;
    mdEl.innerHTML = renderMarkdown(data.text || "");
    mdEl.classList.remove("is-fresh");
    void mdEl.offsetWidth;
    mdEl.classList.add("is-fresh");
  } catch (e) {
    if (seq != null && seq !== renderSeq) return;
    mdEl.innerHTML = '<div class="doc-md-loading">读取失败</div>';
  }
}

async function fillSlide(canvas, i, seq) {
  const fit = canvas.querySelector(".doc-canvas-slide");
  if (!fit) return;
  fit.hidden = false;
  const number = pageNumber(i);
  try {
    const res = await fetch("/api/doc/" + encodeURIComponent(docId) +
      "/slide/" + number);
    if (!res.ok) throw new Error("第 " + number + " 页渲染失败");
    const data = await res.json();
    if (seq != null && seq !== renderSeq) return;
    fit.innerHTML = data.html || "";
    layoutSlide(canvas);
    fit.classList.remove("is-fresh");
    void fit.offsetWidth;
    fit.classList.add("is-fresh");
  } catch (e) {
    if (seq != null && seq !== renderSeq) return;
    fit.innerHTML = '<div class="doc-md-loading">' + escapeText(e.message || "渲染失败") +
      "</div>";
  }
}

function escapeText(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function layoutSlide(canvas) {
  const fit = canvas.querySelector(".doc-canvas-slide");
  if (!fit || fit.hidden) return;
  const art = fit.firstElementChild;
  if (!art || !art.style) return;
  if (!art.offsetWidth) {
    art.style.position = "absolute";
    art.style.left = "0";
    art.style.top = "0";
    art.style.transformOrigin = "top left";
  }
  const w = art.offsetWidth || 1280;
  const h = art.offsetHeight || 720;
  const scale = Math.max(0.2, (canvas.clientWidth || fitW) / w);
  art.style.position = "absolute";
  art.style.left = "0";
  art.style.top = "0";
  art.style.transformOrigin = "top left";
  art.style.transform = "scale(" + scale + ")";
  fit.style.position = "relative";
  fit.style.width = Math.round(w * scale) + "px";
  fit.style.height = Math.round(h * scale) + "px";
  canvas.style.height = Math.round(h * scale) + "px";
}

// ------------------------------------------------------------------ 文本层
function buildTextLayer(el, layer) {
  if (!el) return;
  el.innerHTML = "";
  el.hidden = true;
  if (!layer || !(layer.lines || []).length) return;
  const frag = document.createDocumentFragment();
  const H = layer.height || 1;
  const W = layer.width || 1;
  for (const line of layer.lines) {
    for (const sp of line.spans || []) {
      const span = document.createElement("span");
      span.className = "tl-span tl-" + (sp.style || "text");
      span.textContent = sp.text;
      span.style.fontSize = "calc(" + ((sp.h / H) * 100) + "cqh)";
      span.style.left = (sp.x / W) * 100 + "%";
      span.style.top = (sp.y / H) * 100 + "%";
      span.style.width = (sp.w / W) * 100 + "%";
      span.style.height = (sp.h / H) * 100 + "%";
      frag.appendChild(span);
    }
  }
  el.appendChild(frag);
  el.hidden = false;
}

function layoutTextLayer(canvas, el, layer) {
  // 字符框的宽高用百分比，字号必须跟着画布高度缩放。WebView2 里 cqh 依赖
  // 容器尺寸包含，而画布高度是图片撑开的，所以统一用 px 重算一遍。
  if (!layer || !el || el.hidden) return;
  const hpx = canvas.clientHeight || 1;
  const H = layer.height || 1;
  let i = 0;
  for (const line of layer.lines || []) {
    for (const sp of line.spans || []) {
      const span = el.children[i++];
      if (span) span.style.fontSize = ((sp.h / H) * hpx * 0.94).toFixed(2) + "px";
    }
  }
}

async function loadPartLayer(part, i) {
  const number = pageNumber(i);
  part.text.innerHTML = "";
  part.text.hidden = true;
  part.note.hidden = true;
  part.layer = null;
  try {
    const res = await fetch("/api/doc/" + encodeURIComponent(docId) +
      "/layer/" + number);
    if (!res.ok) {
      part.note.hidden = false;        // 扫描页 / 纯图片页
      return;
    }
    part.layer = await res.json();
    buildTextLayer(part.text, part.layer);
    if (!(part.layer.lines || []).length) {
      part.note.hidden = false;
      return;
    }
    layoutTextLayer(part.canvas, part.text, part.layer);
  } catch (e) {
    part.note.hidden = false;
  }
}

async function loadSingleLayer(number, seq) {
  textLayerEl.innerHTML = "";
  textLayerEl.hidden = true;
  imageNote.hidden = true;
  singleLayer = null;
  if (mode !== "pdf") return;
  try {
    const res = await fetch("/api/doc/" + encodeURIComponent(docId) +
      "/layer/" + number);
    if (seq !== renderSeq) return;
    if (!res.ok) {
      imageNote.hidden = false;
      return;
    }
    singleLayer = await res.json();
    buildTextLayer(textLayerEl, singleLayer);
    if (!(singleLayer.lines || []).length) {
      imageNote.hidden = false;
      return;
    }
    layoutTextLayer(pageCanvas, textLayerEl, singleLayer);
  } catch (e) {
    if (seq === renderSeq) imageNote.hidden = false;
  }
}

// ------------------------------------------------------------------ 缩略图
function renderTiles() {
  tileGrid.innerHTML = "";
  for (let i = 0; i < pages.length; i++) {
    const p = pages[i];
    const number = pageNumber(i);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "doc-tile" + (i === index ? " current" : "");
    btn.setAttribute("aria-label", "第 " + number + " 页");
    // 只让前几张错开入场，后面的统一延迟，避免几十页时动画排到几秒后。
    btn.style.animationDelay = Math.min(i, 18) * 12 + "ms";
    if (mode === "pdf") {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.addEventListener("load", () => img.classList.add("is-ready"));
      img.addEventListener("error", () => img.classList.add("is-ready"));
      img.src = "/api/doc/" + encodeURIComponent(docId) +
        "/page/" + number + ".png?w=400";
      img.alt = "第 " + number + " 页";
      btn.appendChild(img);
    } else {
      const ph = document.createElement("div");
      ph.className = "doc-tile-text";
      ph.textContent = p.title || "第 " + number + " 页";
      btn.appendChild(ph);
    }
    const cap = document.createElement("span");
    cap.className = "doc-tile-cap";
    cap.textContent = number;
    btn.appendChild(cap);
    btn.addEventListener("click", () => {
      // 点缩略图后回到连续阅读并定位到这一页。
      viewMode = "scroll";
      setModeButtons();
      tileGrid.hidden = true;
      scrollView.hidden = false;
      pageBox.hidden = true;
      ensureScroll();
      show(i);
    });
    tileGrid.appendChild(btn);
  }
}

// ------------------------------------------------------------------ 模式 / 导航
function setModeButtons() {
  $("#btn-mode-scroll").classList.toggle("active", viewMode === "scroll");
  $("#btn-mode-single").classList.toggle("active", viewMode === "single");
  $("#btn-mode-tile").classList.toggle("active", viewMode === "tile");
}

function setViewMode(m) {
  if (m !== "scroll" && m !== "single" && m !== "tile") return;
  if (m === viewMode) return;
  const i = index;
  viewMode = m;
  setModeButtons();
  scrollView.hidden = m !== "scroll";
  pageBox.hidden = m !== "single";
  tileGrid.hidden = m !== "tile";
  if (m !== "single") $("#doc-loading").hidden = true;
  // 连续模式可能已经滚到很下面；切到单页/缩略图时容器还是那个滚动位置，
  // 不归零的话单页会从半截开始显示。
  if (m !== "scroll") pageArea.scrollTop = 0;
  if (m === "scroll") {
    ensureScroll();
    requestAnimationFrame(() => {
      layoutPage();
      scrollToPage(i, false);
    });
  } else if (m === "single") {
    renderSingle(i);
  } else {
    renderTiles();
  }
}

function step(delta) {
  if (!pages.length) return;
  const target = Math.max(0, Math.min(pages.length - 1, index + delta));
  if (target === index) return;
  if (viewMode === "scroll") show(target, { smooth: true });
  else show(target);
}

function onWheel(e) {
  // 连续模式让滚轮正常滚动；单页模式滚一格翻一页，这才是「滚轮翻页」。
  if (viewMode !== "single") return;
  if (e.ctrlKey) {
    e.preventDefault();
    setZoom(zoom + (e.deltaY < 0 ? 0.1 : -0.1));
    return;
  }
  if (!e.deltaY && !e.deltaX) return;
  e.preventDefault();
  const now = Date.now();
  if (now - wheelAt < 420) return;
  wheelAt = now;
  step(e.deltaY > 0 ? 1 : -1);
}

function handleKey(key) {
  if (key === "ArrowLeft" || key === "PageUp") step(-1);
  else if (key === "ArrowRight" || key === "PageDown" || key === " ") step(1);
  else if (key === "Home") show(0);
  else if (key === "End") show(pages.length - 1);
}

document.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t && (/^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(t.tagName) || t.isContentEditable)) return;
  if (!["ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End", " "].includes(e.key)) return;
  e.preventDefault();
  handleKey(e.key);
});

// 外层跳页（检索结果）用
window.goTo = (n) => {
  const i = Math.max(0, Math.min(pages.length - 1, (Number(n) || 1) - 1));
  if (viewMode === "tile") setViewMode("scroll");
  show(i);
};

$("#btn-prev").addEventListener("click", () => step(-1));
$("#btn-next").addEventListener("click", () => step(1));
$("#page-input").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  const n = parseInt($("#page-input").value, 10);
  if (!isNaN(n)) show(n - 1);
});
$("#btn-mode-scroll").addEventListener("click", () => setViewMode("scroll"));
$("#btn-mode-single").addEventListener("click", () => setViewMode("single"));
$("#btn-mode-tile").addEventListener("click", () => setViewMode("tile"));
$("#btn-zoom-in").addEventListener("click", () => setZoom(zoom + 0.15));
$("#btn-zoom-out").addEventListener("click", () => setZoom(zoom - 0.15));
$("#btn-zoom-fit").addEventListener("click", () => setZoom(1));

init();
