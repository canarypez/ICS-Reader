"use strict";

// 资料阅读器里的 Markdown 渲染。比 app.js 的 md() 简单：不引 KaTeX，公式按原文
// 显示（AI 回答里才有公式渲染的需求）。所有文本先转义，代码块单独抠出来。
function renderMarkdown(src) {
  const blocks = [];
  const inlines = [];
  let s = String(src == null ? "" : src).replace(/\r\n?/g, "\n");

  s = s.replace(/```([^\n]*)\n?([\s\S]*?)```/g, (_, info, code) => {
    const lang = String(info || "").trim().split(/\s+/)[0];
    blocks.push('<pre class="md-code"' + (lang ? ' data-lang="' + esc(lang) + '"' : "") +
      "><code>" + esc(code.replace(/^\n/, "")) + "</code></pre>");
    return "\n\n\u0000F" + (blocks.length - 1) + "\u0000\n\n";
  });
  s = s.replace(/`([^`\n]+)`/g, (_, code) => {
    inlines.push("<code>" + esc(code) + "</code>");
    return "\u0000I" + (inlines.length - 1) + "\u0000";
  });

  s = esc(s);
  s = tables(s);
  s = lists(s);
  s = s.replace(/^&gt;\s?.*(?:\n&gt;\s?.*)*/gm, (m) =>
    "<blockquote>" + m.replace(/^&gt;\s?/gm, "").replace(/\n/g, "<br>") + "</blockquote>");
  s = s.replace(/^(?:-{3,}|\*{3,}|_{3,})\s*$/gm, "<hr>");
  s = s.replace(/^###### (.*)$/gm, "<h6>$1</h6>");
  s = s.replace(/^##### (.*)$/gm, "<h5>$1</h5>");
  s = s.replace(/^#### (.*)$/gm, "<h4>$1</h4>");
  s = s.replace(/^### (.*)$/gm, "<h3>$1</h3>");
  s = s.replace(/^## (.*)$/gm, "<h2>$1</h2>");
  s = s.replace(/^# (.*)$/gm, "<h1>$1</h1>");

  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  s = s.replace(/\[([^\]\n]*)\]\(([^)\s"'<>]+)\)/g, (m, text, url) =>
    /^(?:https?:|mailto:|#|\/)/i.test(url)
      ? '<a href="' + url + '" target="_blank" rel="noreferrer noopener">' + text + "</a>"
      : m);

  s = s.replace(/^\n+|\n+$/g, "");
  s = s.replace(/\n{2,}/g, "</p><p>");
  s = "<p>" + s + "</p>";
  s = s.replace(/<p>(<(?:ul|ol|pre|h[1-6]|table|blockquote|hr)\b)/g, "$1");
  s = s.replace(/<p>(\u0000F\d+\u0000)<\/p>/g, "$1");
  s = s.replace(/(<\/(?:ul|ol|pre|h[1-6]|table|blockquote)>|<hr>|\u0000F\d+\u0000)<\/p>/g, "$1");
  s = s.replace(/\u0000I(\d+)\u0000/g, (_, i) => inlines[+i]);
  s = s.replace(/\u0000F(\d+)\u0000/g, (_, i) => blocks[+i]);
  return s;
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function tables(s) {
  const lines = s.split("\n");
  const out = [];
  const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "")
    .split("|").map((c) => c.trim());
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

function lists(s) {
  const lines = s.split("\n");
  const out = [];
  const item = /^([ \t]*)([-*+]|\d+\.)[ \t]+(.*)$/;
  const stack = [];
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
    while (stack.length && indent < top().indent) {
      closeLi(); out.push("</" + stack.pop().tag + ">");
    }
    if (!stack.length) { out.push("", "<" + tag + ">"); stack.push({ tag, indent, pend: false }); }
    else if (indent > top().indent) { out.push("<" + tag + ">"); stack.push({ tag, indent, pend: false }); }
    else if (tag !== top().tag) {
      closeLi(); out.push("</" + stack.pop().tag + ">", "<" + tag + ">");
      stack.push({ tag, indent, pend: false });
    } else closeLi();
    out.push("<li>" + m[3]);
    top().pend = true;
  }
  closeAll();
  return out.join("\n");
}
