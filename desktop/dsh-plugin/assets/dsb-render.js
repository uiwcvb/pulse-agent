/**
 * [Module Responsibility]
 * DSB 桌面端无状态视觉渲染库 (`assets/dsb-render.js`)。
 * 提供：矢量 SVG 图标集、灵体头像渲染、Markdown/代码着色/LaTeX 排版、单行工具胶囊与独立思考折叠条 HTML 生成。
 *
 * [Public Interface]
 * - `window.__DSB_RENDER__`: `{ PULSE_SHAPE_PATHS, iconSvg, escapeHtml, resolveChatMediaUrl, renderRichMarkdown, renderBotAvatarSvg, formatCompactTokens, friendlyToolLabel, renderToolCapsuleHtml, renderThoughtPillHtml }`
 */
(function () {
  "use strict";
  var PULSE_SHAPE_PATHS = {
    flower: "M50 4 C61 4 64 17 73 20 C82 23 93 16 97 25 C101 35 89 43 89 52 C89 61 100 68 95 78 C90 87 78 81 70 86 C62 91 61 98 50 98 C39 98 38 91 30 86 C22 81 10 87 5 78 C0 68 11 61 11 52 C11 43 -1 35 3 25 C7 16 18 23 27 20 C36 17 39 4 50 4 Z",
    circle: "M50 5 A45 45 0 1 0 50 95 A45 45 0 1 0 50 5 Z",
    oval: "M50 3 A38 47 0 1 0 50 97 A38 47 0 1 0 50 3 Z",
    square: "M22 8 H78 A14 14 0 0 1 92 22 V78 A14 14 0 0 1 78 92 H22 A14 14 0 0 1 8 78 V22 A14 14 0 0 1 22 8 Z",
    pill: "M34 6 H66 A28 28 0 0 1 94 34 V66 A28 28 0 0 1 66 94 H34 A28 28 0 0 1 6 66 V34 A28 28 0 0 1 34 6 Z",
    triangle: "M50 6 C57 6 61 12 65 20 L92 74 C96 82 91 92 81 92 H19 C9 92 4 82 8 74 L35 20 C39 12 43 6 50 6 Z",
    diamond: "M50 5 L95 50 L50 95 L5 50 Z",
    star: "M50 4 C54 33 67 46 96 50 C67 54 54 67 50 96 C46 67 33 54 4 50 C33 46 46 33 50 4 Z",
    drop: "M50 5 C71 29 88 47 88 67 A38 38 0 1 1 12 67 C12 47 29 29 50 5 Z",
    whale: "M16 56 C16 28 38 14 63 18 C81 21 92 34 94 50 C96 63 87 76 74 80 C56 85 33 84 20 74 C14 69 16 62 16 56 Z",
    whalechan: "M26 8 H74 A18 18 0 0 1 92 26 V74 A18 18 0 0 1 74 92 H26 A18 18 0 0 1 8 74 V26 A18 18 0 0 1 26 8 Z"
  };

  var ICON_PATHS = {
    sparkles: '<path d="M12 2.5L14.3 8.2L20 10.5L14.3 12.8L12 18.5L9.7 12.8L4 10.5L9.7 8.2L12 2.5Z" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"/><path d="M19 16.5L19.9 18.6L22 19.5L19.9 20.4L19 22.5L18.1 20.4L16 19.5L18.1 18.6L19 16.5Z" fill="currentColor"/>',
    expand: '<path d="M15 4H20V9M9 20H4V15M20 4L13.5 10.5M4 20L10.5 13.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    collapse: '<path d="M4 14H10V20M20 10H14V4M14 10L20.5 3.5M10 14L3.5 20.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    sun: '<circle cx="12" cy="12" r="4.2" stroke="currentColor" stroke-width="1.8"/><path d="M12 2.5V5M12 19V21.5M4.2 4.2L6 6M18 18L19.8 19.8M2.5 12H5M19 12H21.5M4.2 19.8L6 18M18 6L19.8 4.2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    moon: '<path d="M20.2 14.6A8.2 8.2 0 0 1 9.4 3.8A8.3 8.3 0 1 0 20.2 14.6Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
    vault: '<path d="M4 6.5A2.5 2.5 0 0 1 6.5 4H19.5V19.5H6.5A2.5 2.5 0 0 1 4 17V6.5Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M4 17A2.5 2.5 0 0 1 6.5 14.5H19.5" stroke="currentColor" stroke-width="1.6"/><path d="M8.5 8.5H15.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/>',
    sliders: '<path d="M4 7H10M14 7H20M10 4.5V9.5M4 17H13M17 17H20M13 14.5V19.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    compress: '<path d="M8 4V8H4M16 4V8H20M8 20V16H4M16 20V16H20" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    close: '<path d="M6 6L18 18M18 6L6 18" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>',
    plus: '<path d="M12 5V19M5 12H19" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/>',
    search: '<circle cx="10.8" cy="10.8" r="6.3" stroke="currentColor" stroke-width="1.8"/><path d="M15.6 15.6L20 20" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    pin: '<path d="M15.5 4.5L19.5 8.5L16.5 11.5L15.5 16L8 8.5L12.5 7.5L15.5 4.5Z" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"/><path d="M6 18L11.5 12.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2.2" stroke="currentColor" stroke-width="1.75"/><path d="M5.5 15.5H5A1.8 1.8 0 0 1 3.2 13.7V5A1.8 1.8 0 0 1 5 3.2H13.7A1.8 1.8 0 0 1 15.5 5V5.5" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"/>',
    reply: '<path d="M9.5 7L4.5 12L9.5 17" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round"/><path d="M5 12H14.5A5 5 0 0 1 19.5 17V18" stroke="currentColor" stroke-width="1.85" stroke-linecap="round"/>',
    edit: '<path d="M15.8 4.6L19.4 8.2L8.4 19.2H4.8V15.6L15.8 4.6Z" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"/>',
    refresh: '<path d="M19.5 8.5A7.8 7.8 0 0 0 6.2 6.8L4.5 8.5M4.5 5V8.5H8M4.5 15.5A7.8 7.8 0 0 0 17.8 17.2L19.5 15.5M19.5 19V15.5H16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    paperclip: '<path d="M18.4 11.3L10.6 19.1A5 5 0 0 1 3.5 12L11.8 3.7A3.3 3.3 0 0 1 16.5 8.4L8.4 16.5A1.6 1.6 0 0 1 6.1 14.2L13.5 6.8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    mic: '<rect x="9" y="3.5" width="6" height="10.5" rx="3" stroke="currentColor" stroke-width="1.8"/><path d="M5.5 11A6.5 6.5 0 0 0 18.5 11M12 17.5V20.8M9 20.8H15" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    send: '<path d="M12 19V5.5M6.5 10.5L12 5L17.5 10.5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
    stop: '<rect x="7" y="7" width="10" height="10" rx="2.2" fill="currentColor"/>',
    check: '<path d="M5 12.5L9.6 17L19 7.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    alert: '<circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="1.8"/><path d="M12 8V12.8" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/><circle cx="12" cy="16.2" r="1" fill="currentColor"/>',
    chevron: '<path d="M9 6.5L14.5 12L9 17.5" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>',
    share: '<path d="M7.5 8.5H17.5L14.2 5.2M16.5 15.5H6.5L9.8 18.8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    trash: '<path d="M5 7H19M9.5 7V5A1.5 1.5 0 0 1 11 3.5H13A1.5 1.5 0 0 1 14.5 5V7M7.5 7L8.2 19A1.8 1.8 0 0 0 10 20.5H14A1.8 1.8 0 0 0 15.8 19L16.5 7" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"/>',
    file: '<path d="M7 3.8H13.8L18 8V19.2A1.8 1.8 0 0 1 16.2 21H7A1.8 1.8 0 0 1 5.2 19.2V5.6A1.8 1.8 0 0 1 7 3.8Z" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"/><path d="M13.5 4V8.2H17.8" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"/>',
    smile: '<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.75"/><path d="M8.5 14.2C9.4 15.8 10.6 16.5 12 16.5C13.4 16.5 14.6 15.8 15.5 14.2" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"/><circle cx="9.2" cy="9.8" r="1.15" fill="currentColor"/><circle cx="14.8" cy="9.8" r="1.15" fill="currentColor"/>',
    arrowDown: '<path d="M12 5V18.5M6.5 13L12 18.8L17.5 13" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>',
    more: '<circle cx="5.5" cy="12" r="1.4" fill="currentColor"/><circle cx="12" cy="12" r="1.4" fill="currentColor"/><circle cx="18.5" cy="12" r="1.4" fill="currentColor"/>',
    command: '<path d="M15 6V18A3 3 0 1 0 18 15H6A3 3 0 1 0 9 18V6A3 3 0 1 0 6 9H18A3 3 0 1 0 15 6Z" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"/>'
  };

  function brandMarkSvg(size) {
    var sz = size || 18;
    return '<svg class="dsb-ico dsb-brand-svg" width="' + sz + '" height="' + sz + '" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<path d="M4.5 9.8L7.2 4L11.1 7.9H12.9L16.8 4L19.5 9.8V13.8C19.5 17.7 16.2 20.5 12 20.5C7.8 20.5 4.5 17.7 4.5 13.8V9.8Z" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"/>' +
      '<path d="M12 10.6L13.6 13.4L12 16.2L10.4 13.4L12 10.6Z" fill="#38BDF8"/>' +
      "</svg>";
  }
  function iconSvg(name, size, extraClass) {
    var sz = size || 16;
    var cls = "dsb-ico" + (extraClass ? " " + extraClass : "");
    return '<svg class="' + cls + '" width="' + sz + '" height="' + sz + '" viewBox="0 0 24 24" fill="none" aria-hidden="true">' + (ICON_PATHS[name] || ICON_PATHS.sparkles) + "</svg>";
  }
  function escapeHtml(str) {
    return String(str === null || str === undefined ? "" : str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function formatLatexMathHtml(tex, isBlock) {
    var s = String(tex || "").trim();
    if (!s) return "";
    var symMap = {
      "\\alpha": "α", "\\beta": "β", "\\gamma": "γ", "\\delta": "δ", "\\epsilon": "ε", "\\theta": "θ", "\\lambda": "λ",
      "\\mu": "μ", "\\pi": "π", "\\sigma": "σ", "\\tau": "τ", "\\phi": "φ", "\\omega": "ω", "\\Delta": "Δ", "\\Sigma": "Σ", "\\Omega": "Ω",
      "\\infty": "∞", "\\sum": "∑", "\\prod": "∏", "\\int": "∫", "\\partial": "∂", "\\nabla": "∇", "\\pm": "±", "\\times": "×", "\\div": "÷", "\\cdot": "·",
      "\\leq": "≤", "\\le": "≤", "\\geq": "≥", "\\ge": "≥", "\\neq": "≠", "\\ne": "≠", "\\approx": "≈", "\\equiv": "≡",
      "\\rightarrow": "→", "\\to": "→", "\\leftarrow": "←", "\\Rightarrow": "⇒", "\\Leftrightarrow": "⇔", "\\in": "∈", "\\notin": "∉", "\\sqrt": "√"
    };
    s = s.replace(/\\left\s*([(\[{|])/g, "$1").replace(/\\right\s*([)\]}|])/g, "$1").replace(/\\(?:text|mathrm|mathbf|mathit|operatorname)\s*\{([^{}]*)\}/g, "$1");
    for (var k = 0; k < 3; k++) s = s.replace(/\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, "($1)/($2)").replace(/\\sqrt\s*\{([^{}]*)\}/g, "√($1)");
    s = s.replace(/\\[a-zA-Z]+/g, function (cmd) { return Object.prototype.hasOwnProperty.call(symMap, cmd) ? symMap[cmd] : cmd.slice(1); });
    var escaped = escapeHtml(s)
      .replace(/\^\{([^{}]+)\}/g, "<sup>$1</sup>").replace(/\^([a-zA-Z0-9+\-])/g, "<sup>$1</sup>")
      .replace(/_\{([^{}]+)\}/g, "<sub>$1</sub>").replace(/_([a-zA-Z0-9+\-])/g, "<sub>$1</sub>");
    return isBlock ? '<div class="dsb-math-block">' + escaped + "</div>" : '<span class="dsb-math-inline">' + escaped + "</span>";
  }
  function sanitizeHref(rawUrl) {
    var u = String(rawUrl || "").trim();
    if (!u) return "";
    if (/^(?:javascript|vbscript|file):/i.test(u)) return "";
    if (/^data:(?!image\/(?:png|jpeg|jpg|webp|gif);base64,)/i.test(u)) return "";
    if (/^(?:https?:\/\/|mailto:|\/(?!\/)|#|\.\.?\/)/i.test(u)) return u;
    if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(u)) return "";
    return u;
  }
  function safeCssColor(rawColor, fallback) {
    var c = String(rawColor || "").trim();
    return /^#[0-9a-fA-F]{3,8}$/.test(c) ? c : (fallback || "#0A84FF");
  }
  function resolveChatMediaUrl(rawUrl, forceDownload) {
    var u = String(rawUrl || "").trim();
    if (!u) return "";
    if (/^https?:\/\//i.test(u) || /^data:image\/(?:png|jpeg|jpg|webp|gif);base64,/i.test(u)) return sanitizeHref(u);
    if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(u) || u.indexOf("//") === 0) return "";
    var relPath = u;
    if (relPath.indexOf("/workspace-media/") !== 0 && relPath.indexOf("/stickers/") !== 0 && relPath.indexOf("/static/") !== 0 && relPath.indexOf("/api/files/") !== 0) {
      var clean = relPath.replace(/^workspace\//i, "").replace(/^\/+/, "");
      if (clean.indexOf("/") === -1 && /\.(?:png|jpe?g|webp|gif|bmp|svg|pdf|docx?|xlsx?|pptx?|zip)$/i.test(clean)) {
        clean = "uploads/" + clean;
      }
      relPath = "/workspace-media/" + clean;
    }
    if (forceDownload) {
      relPath += (relPath.indexOf("?") !== -1 ? "&" : "?") + "download=1";
    }
    return "http://127.0.0.1:18195/proxy-media?path=" + encodeURIComponent(relPath);
  }
  function isImageMediaPath(fp) {
    return /\.(?:png|jpe?g|webp|gif|bmp|svg)(?:[?#].*)?$/i.test(String(fp || "").trim());
  }
  function formatFileSizeStr(bytes) {
    var n = Number(bytes || 0);
    if (!n || n <= 0) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }
  function renderFileAttachmentCardHtml(fp, meta) {
    var pathStr = String(fp || "").trim();
    if (!pathStr) return "";
    var name = (meta && meta.name) ? String(meta.name) : pathStr.split("/").pop();
    var ext = ((meta && meta.ext) || (name.indexOf(".") !== -1 ? name.split(".").pop() : "") || "FILE").toUpperCase().slice(0, 5);
    var tone = ext === "PDF" ? "is-pdf"
      : (/^(ZIP|RAR|7Z|TAR|GZ)$/.test(ext) ? "is-zip"
      : (/^(XLS|XLSX|CSV)$/.test(ext) ? "is-sheet"
      : (/^(DOC|DOCX|PPT|PPTX)$/.test(ext) ? "is-doc" : "is-code")));
    var typeLabel = ext === "PDF" ? "PDF 文档" : (ext + " 文件");
    var szStr = meta && meta.size ? formatFileSizeStr(meta.size) : "";
    var subText = szStr ? (typeLabel + " · " + szStr + " · 点击预览") : (typeLabel + " · 点击打开预览");
    var previewUrl = resolveChatMediaUrl(pathStr, false);
    var downloadUrl = resolveChatMediaUrl(pathStr, true);
    return '<div class="dsb-msg-file-card" data-open-file="' + escapeHtml(previewUrl) + '" data-file-name="' + escapeHtml(name) + '" data-file-ext="' + escapeHtml(ext) + '" data-download-url="' + escapeHtml(downloadUrl) + '">' +
      '<span class="dsb-msg-file-ext ' + tone + '">' + escapeHtml(ext) + '</span>' +
      '<span class="dsb-msg-file-meta"><span class="dsb-msg-file-name">' + escapeHtml(name) + '</span>' +
      '<span class="dsb-msg-file-sub">' + escapeHtml(subText) + '</span></span>' +
      '<button type="button" class="dsb-msg-file-dl" data-dl-file="' + escapeHtml(downloadUrl) + '" title="下载文件">' + iconSvg("file", 14) + '</button>' +
      "</div>";
  }
  function renderMediaGridHtml(imgPaths, alignEnd) {
    if (!imgPaths || !imgPaths.length) return "";
    var cls = "dsb-msg-media-grid" + (imgPaths.length === 1 ? " is-single" : " is-multi") + (alignEnd ? " is-end" : "");
    return '<div class="' + cls + '">' + imgPaths.map(function (fp) {
      var u = resolveChatMediaUrl(fp, false);
      var bn = String(fp || "").split("/").pop() || "图片";
      return '<img class="dsb-chat-img" data-lightbox-src="' + escapeHtml(u) + '" src="' + escapeHtml(u) + '" alt="' + escapeHtml(bn) + '" loading="lazy"/>';
    }).join("") + "</div>";
  }
  function renderInlineRichMarkdown(lineText) {
    var s = String(lineText || "");
    if (!s) return "";
    var saved = [];
    function stash(html) { var idx = saved.length; saved.push(html); return "\x01DSB" + idx + "E\x01"; }
    s = s.replace(/`([^`\n]+)`/g, function (_, c) { return stash('<code class="dsb-md-inline-code">' + escapeHtml(c) + "</code>"); });
    s = s.replace(/\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|!\[([^\]]*)\]\(([^)\s]+)\)|<image:([^\s>]+)\s*\/?>|\$([^\$\n]{1,160}?)\$/g, function (full, dm1, dm2, im1, imgAlt, imgU1, imgU2, im2) {
      if (dm1 !== undefined || dm2 !== undefined) return stash(formatLatexMathHtml(dm1 !== undefined ? dm1 : dm2, true));
      if (im1 !== undefined || im2 !== undefined) {
        var cand = im1 !== undefined ? im1 : im2;
        if (im2 !== undefined && (/^\d+(?:[.,]\d+)?$/.test(cand.trim()) || (/^\s*\d/.test(cand) && !/[\\^_{}=+\-*/<>]/.test(cand)))) return full;
        return stash(formatLatexMathHtml(cand, false));
      }
      var rawImgRef = String(imgU1 || imgU2 || "").trim();
      if (!rawImgRef || rawImgRef === "路径" || /\/路径$/.test(rawImgRef)) return full;
      var targetImg = resolveChatMediaUrl(rawImgRef, false);
      if (!targetImg) return "";
      return stash('<div class="dsb-inline-img-wrap"><img class="dsb-chat-img" data-lightbox-src="' + escapeHtml(targetImg) + '" src="' + escapeHtml(targetImg) + '" alt="' + escapeHtml(imgAlt || "图片") + '" loading="lazy"/></div>');
    });
    s = s.replace(/\[([^\]]+)\]\(([^\s)]+)\)/g, function (fullM, lbl, rawHref) {
      var safe = sanitizeHref(rawHref);
      if (!safe) return escapeHtml(lbl);
      return stash('<a href="' + escapeHtml(safe) + '" target="_blank" rel="noopener noreferrer" class="dsb-md-link">' + escapeHtml(lbl) + "</a>");
    });
    s = escapeHtml(s)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(>])\*([^*\s][^*\n]*?[^*\s]|[^*\s])\*(?=$|[\s.,;:!?)]|<)/g, "$1<em>$2</em>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    return s.replace(/\x01DSB(\d+)E\x01/g, function (_, idx) { return saved[Number(idx)] || ""; });
  }
  function highlightCodeHtml(codeText) {
    var src = String(codeText || "");
    var tokRe = /(\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|\b(function|return|const|let|var|if|else|for|while|async|await|import|from|export|default|class|new|try|catch|finally|throw|def|elif|None|True|False|true|false|null|interface|type|struct|pub|fn)\b|\b(\d+(?:\.\d+)?)\b/g;
    var out = "", lastIdx = 0, m;
    while ((m = tokRe.exec(src)) !== null) {
      if (m.index > lastIdx) out += escapeHtml(src.slice(lastIdx, m.index));
      if (m[1]) out += '<span class="dsb-tok-com">' + escapeHtml(m[1]) + "</span>";
      else if (m[2]) out += '<span class="dsb-tok-str">' + escapeHtml(m[2]) + "</span>";
      else if (m[3]) out += '<span class="dsb-tok-kw">' + escapeHtml(m[3]) + "</span>";
      else if (m[4]) out += '<span class="dsb-tok-num">' + escapeHtml(m[4]) + "</span>";
      lastIdx = tokRe.lastIndex;
    }
    if (lastIdx < src.length) out += escapeHtml(src.slice(lastIdx));
    return out;
  }
  function renderRichMarkdown(rawText) {
    var text = String(rawText || "").replace(/\r\n/g, "\n").trim();
    if (!text) return "";
    var savedBlocks = [];
    function stashBlock(html) { var idx = savedBlocks.length; savedBlocks.push(html); return "\n\x02DSBB" + idx + "E\x02\n"; }
    text = text.replace(/```([a-zA-Z0-9_+-]*)[ \t]*\n([\s\S]*?)```/g, function (_, lang, body) {
      var cleanCode = body.replace(/\n$/, "");
      var langLabel = (lang || "text").trim().toLowerCase();
      return stashBlock(
        '<div class="dsb-code-card"><div class="dsb-code-head"><span class="dsb-code-lang">' + escapeHtml(langLabel) +
        '</span><button type="button" class="dsb-code-copy" aria-label="复制代码" data-copy-code="' + encodeURIComponent(cleanCode) + '">' + iconSvg("copy", 12) + '<span>复制</span></button></div>' +
        '<pre class="dsb-code-pre"><code>' + highlightCodeHtml(cleanCode) + "</code></pre></div>"
      );
    });
    text = text.replace(/\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]/g, function (_, m1, m2) {
      return stashBlock(formatLatexMathHtml(m1 !== undefined ? m1 : m2, true));
    });
    var lines = text.split("\n"), htmlParts = [], i = 0;
    while (i < lines.length) {
      var prevI = i;
      var ln = lines[i], trimmed = ln.trim();
      if (!trimmed) { i++; continue; }
      var bm = trimmed.match(/^\x02DSBB(\d+)E\x02$/);
      if (bm) { htmlParts.push(savedBlocks[Number(bm[1])] || ""); i++; continue; }
      if (/^(?:---|\*\*\*|___)\s*$/.test(trimmed)) { htmlParts.push('<hr class="dsb-md-hr"/>'); i++; continue; }
      var hm = trimmed.match(/^(#{1,4})\s+(.+)$/);
      if (hm) { htmlParts.push('<div class="dsb-md-h dsb-md-h' + hm[1].length + '">' + renderInlineRichMarkdown(hm[2]) + "</div>"); i++; continue; }
      if (trimmed.indexOf("|") !== -1 && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1])) {
        var parseRow = function (r) { return r.trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); }); };
        var headers = parseRow(trimmed); i += 2; var rows = [];
        while (i < lines.length && lines[i].trim().indexOf("|") !== -1 && !/^\x02DSBB\d+E\x02$/.test(lines[i].trim())) { rows.push(parseRow(lines[i])); i++; }
        htmlParts.push('<div class="dsb-md-table-wrap"><table class="dsb-md-table"><thead><tr>' +
          headers.map(function (h) { return "<th>" + renderInlineRichMarkdown(h) + "</th>"; }).join("") + "</tr></thead><tbody>" +
          rows.map(function (r) { return "<tr>" + headers.map(function (_, ci) { return "<td>" + renderInlineRichMarkdown(r[ci] || "") + "</td>"; }).join("") + "</tr>"; }).join("") +
          "</tbody></table></div>");
        continue;
      }
      if (/^>\s?/.test(trimmed)) {
        var qLines = [];
        while (i < lines.length && /^>\s?/.test(lines[i].trim())) { qLines.push(lines[i].trim().replace(/^>\s?/, "")); i++; }
        htmlParts.push('<blockquote class="dsb-md-quote">' + qLines.map(renderInlineRichMarkdown).join("<br/>") + "</blockquote>"); continue;
      }
      if (/^(?:[-*+]\s+|\d+\.\s+)/.test(trimmed)) {
        var ordered = /^\d+\.\s+/.test(trimmed), items = [];
        while (i < lines.length && (ordered ? /^\d+\.\s+/ : /^[-*+]\s+/).test(lines[i].trim())) {
          items.push("<li>" + renderInlineRichMarkdown(lines[i].trim().replace(/^(?:[-*+]|\d+\.)\s+/, "")) + "</li>"); i++;
        }
        htmlParts.push("<" + (ordered ? "ol" : "ul") + ' class="dsb-md-list">' + items.join("") + "</" + (ordered ? "ol" : "ul") + ">"); continue;
      }
      var pLines = [];
      while (i < lines.length) {
        var cur = lines[i], ct = cur.trim();
        if (!ct || /^\x02DSBB\d+E\x02$/.test(ct) || /^(?:#{1,4}\s+|>\s?|[-*+]\s+|\d+\.\s+|(?:---|\*\*\*|___)\s*$)/.test(ct)) break;
        if (ct.indexOf("|") !== -1 && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1])) break;
        pLines.push(renderInlineRichMarkdown(cur)); i++;
      }
      if (pLines.length) htmlParts.push('<p class="dsb-md-p">' + pLines.join("<br/>") + "</p>");
      if (i === prevI) {
        htmlParts.push('<p class="dsb-md-p">' + renderInlineRichMarkdown(lines[i]) + "</p>");
        i++;
      }
    }
    return htmlParts.join("");
  }

  function renderBotAvatarSvg(bot, size, whaleAvatarUrl) {
    var sz = size || 28;
    var shapeKey = bot && bot.shape ? String(bot.shape).toLowerCase() : (bot && bot.id === "whalechan" ? "whalechan" : "flower");
    if ((shapeKey === "whalechan" || (bot && bot.id === "whalechan")) && whaleAvatarUrl) {
      return '<img src="' + escapeHtml(whaleAvatarUrl) + '" width="' + sz + '" height="' + sz + '" style="width:' + sz + "px;height:" + sz + 'px;border-radius:30%;object-fit:cover;display:block;flex-shrink:0;" alt=""/>';
    }
    var d = PULSE_SHAPE_PATHS[shapeKey] || PULSE_SHAPE_PATHS.circle;
    var fill = safeCssColor(bot && bot.color, "#0A84FF");
    var eye = '<circle cx="38" cy="49" r="5" fill="#FFFFFF"/><circle cx="62" cy="49" r="5" fill="#FFFFFF"/><circle cx="39" cy="49" r="2.3" fill="#18181B"/><circle cx="63" cy="49" r="2.3" fill="#18181B"/>';
    return '<svg width="' + sz + '" height="' + sz + '" viewBox="0 0 100 100" style="display:block;flex-shrink:0;" aria-hidden="true"><path d="' + d + '" fill="' + escapeHtml(fill) + '"/>' + eye + "</svg>";
  }
  function formatCompactTokens(n) {
    var v = Number(n || 0);
    if (v >= 1000000) return (v / 1000000).toFixed(1) + "M";
    if (v >= 1000) return (v / 1000).toFixed(1) + "k";
    return String(Math.max(0, Math.round(v)));
  }
  function friendlyToolLabel(name, args) {
    var raw = String(name || "").trim();
    if (!raw) return "执行工具";
    if (raw.indexOf("send_message →") === 0 || raw.indexOf("收到「") === 0 || raw.indexOf("已执行") === 0 || raw.indexOf("并行") === 0) return raw;
    var a = args && typeof args === "object" ? args : {};
    var op = String(a.op || a.action || "").trim().toLowerCase();
    var opTable = {
      reply: {
        sticker: "附带表情贴纸", send_sticker: "附带表情贴纸", emoji: "附带表情贴纸",
        choices: "附加快捷选项", present_choices: "附加快捷选项",
        send_image: "发送图片", image: "发送图片",
        send_file: "发送文件", file: "发送文件", send_pdf: "发送文件", send_doc: "发送文件",
        notify: "发送通知", notify_user: "发送通知", alarm: "紧急提醒", emergency_alarm: "紧急提醒",
        silent: "保持安静", stay_silent: "保持安静", dnd: "免打扰", complete_goal: "完成目标", finish_goal: "完成目标"
      },
      edit_file: { replace: "编辑替换文件", move: "移动重命名文件", rename: "移动重命名文件", delete: "删除文件" },
      search_files: { grep: "全文检索代码", glob: "按模式找文件", list: "列出目录文件" },
      bash: { run: "运行终端命令", bg_start: "启动后台任务", bg_logs: "查看后台日志", bg_stdin: "向后台输入", bg_kill: "终止后台任务", bg_list: "列出后台任务", watchdog_start: "启动终端哨兵", watchdog_stop: "停止终端哨兵" },
      run_bash: { run: "运行终端命令", bg_start: "启动后台任务", bg_logs: "查看后台日志" },
      web: { search: "联网搜索资料", fetch: "抓取网页正文" },
      memory: { search: "检索长期记忆", conversation: "检索对话历史", ctx: "上下文检索", pin: "钉入置顶记忆", unpin: "移除置顶记忆", list_pins: "查看置顶记忆" },
      blackboard: { post: "发布黑板协作", read: "读取黑板帖子", peek: "查看同行进度", claim: "认领黑板任务", complete: "提交任务完成", review: "复核黑板成果", deliver: "交付黑板成果", approve: "审批通过", reject: "退回修改", schedule: "安排定时黑板", alarm: "设置黑板闹钟", cancel: "取消黑板任务", list: "查看共享黑板", clear_done: "清理已完成帖" },
      phone: { tap: "手机虚拟屏点击", click: "手机虚拟屏点击", swipe: "手机虚拟屏滑动", type: "手机输入文字", key: "手机发送按键", ui_tree: "读取手机控件树", screenshot: "手机虚拟屏截图", launch: "手机打开应用", open_app: "手机打开应用", stop: "停止手机工作区", promote: "拉起至主屏幕" },
      phone_control: { tap: "手机虚拟屏点击", swipe: "手机虚拟屏滑动", type: "手机输入文字", ui_tree: "读取手机控件树", screenshot: "手机虚拟屏截图", launch: "手机打开应用" },
      pc: { status: "检查电脑端状态", shell: "电脑终端命令", fs: "读写电脑文件", screenshot: "截取电脑画面", click: "电脑鼠标点击", type_text: "电脑键盘输入", press_key: "电脑发送快捷键", cua: "电脑桌面操控", dispatch_session: "派发 DSH 会话" },
      pc_control: { status: "检查电脑端状态", shell: "电脑终端命令", screenshot: "截取电脑画面", click: "电脑鼠标点击", type_text: "电脑键盘输入" },
      cloud: { bash: "云电脑终端", snapshot: "云电脑截屏", open_url: "云浏览器打开网页", click: "云桌面点击", scroll: "云桌面滚动", type: "云桌面输入", zoom: "云屏幕局部放大", send_screenshot: "发送云桌面截图", download: "下载云电脑文件" },
      cloud_computer: { bash: "云电脑终端", snapshot: "云电脑截屏", open_url: "云浏览器打开网页", zoom: "云屏幕局部放大", send_screenshot: "发送云桌面截图" },
      custom_tool: { create: "创建自定义工具", run: "运行自定义工具", list: "列出自定义工具", delete: "删除自定义工具" },
      admin: { status: "查看系统状态", doctor: "运行系统诊断", context: "检查上下文详情", config: "读取或更新配置", bot: "管理助理配置", skill: "管理技能扩展", custom_tool: "管理自定义工具", models: "刷新可用模型", deploy_cloud: "部署云电脑" }
    };
    if (op && opTable[raw] && opTable[raw][op]) {
      var subBase = opTable[raw][op];
      if (raw === "reply" && (op === "sticker" || op === "send_sticker" || op === "emoji")) {
        var sc = String(a.code || a.sticker || a.name || "").trim();
        return sc ? (subBase + " · " + sc.slice(0, 20)) : subBase;
      }
      var subHint = a.to_bot_id || a.path || a.query || a.target || a.app || a.command || a.url || "";
      return subHint ? (subBase + " · " + String(subHint).replace(/\s+/g, " ").trim().slice(0, 24)) : subBase;
    }
    var map = {
      read_file: "读取文件", write_file: "写入文件", edit_file: "编辑文件", list_dir: "浏览目录",
      search_files: "搜索工作区文件", glob_files: "搜索文件", grep_search: "全文检索",
      bash: "终端与后台任务", run_bash: "终端命令", web: "联网搜索与网页", web_search: "联网搜索",
      web_fetch: "抓取网页", memory: "长期记忆与置顶", memory_search: "检索记忆", conversation_search: "历史检索", ctx_search: "上下文检索",
      memory_pin: "置顶记忆", blackboard: "共享黑板", peek_bots: "进度窥视", send_message: "协作传讯", send_message_callback: "协作回执",
      reply: "快捷选项/附媒体", present_choices: "附加快捷选项", subtask: "独立子任务研判",
      custom_tool: "自定义工具", admin: "系统运维配置", system_config: "系统配置", schedule: "定时日程编排", schedule_task: "定时日程",
      dsh_workspace: "电脑工作区", pc: "电脑协同", pc_control: "电脑桌面控制",
      cloud_desktop: "云端桌面", cloud_computer: "云电脑操作", cloud: "云端桌面",
      phone_workspace: "手机工作台", phone_control: "手机虚拟屏控制", phone: "手机协同",
      manage_bot: "管理助理", complete_goal: "完成目标", stay_silent: "静默收尾"
    };
    var base = map[raw] || raw;
    var hint = a.to_bot_id || a.path || a.query || a.op || a.action || a.command || a.goal || "";
    return hint ? (base + " · " + String(hint).replace(/\s+/g, " ").trim().slice(0, 26)) : base;
  }
  var humanizeToolName = friendlyToolLabel;
  function renderTurnProcessHtml(traceList, thoughtStr, turnSteps, opts) {
    var o = opts && typeof opts === "object" ? opts : {};
    var isStreaming = Boolean(o.isStreaming);
    var tl = Array.isArray(turnSteps) && turnSteps.length ? turnSteps : [];
    var exp = {};
    if (o.isOpen && o.msgId) exp[o.msgId] = true;
    return renderUnifiedTimelineBodyHtml(tl, thoughtStr || "", "", Array.isArray(traceList) ? traceList : [], isStreaming, o.msgId || "", exp);
  }
  function renderToolCapsuleHtml(traceList, isRunning, liveToolLabel) {
    return renderUnifiedTimelineBodyHtml([], "", liveToolLabel ? String(liveToolLabel) : "", Array.isArray(traceList) ? traceList : [], Boolean(isRunning), "", {});
  }
  function renderThoughtPillHtml(rawThought, apiCalls, isStreaming) {
    return renderUnifiedTimelineBodyHtml([], rawThought || "", "", [], Boolean(isStreaming), "", {});
  }

  function stripResidualControlTags(raw) {
    return String(raw || "")
      .replace(/<think>[\s\S]*?<\/think>/gi, "")
      .replace(/<\/?think>/gi, "")
      .replace(/<sticker\s*[:：]\s*[^>\n]+?\s*\/?>/gi, "")
      .replace(/<sticker\s+(?:code|name|id|label)\s*=\s*["']?[^"'>\s]+["']?\s*\/?>/gi, "")
      .replace(/<sticker>[^<\n]*<\/sticker>/gi, "")
      .replace(/\[(?:sticker|表情|贴纸)\s*[:：]\s*[^\]\n]+?\]/gi, "")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  function renderUnifiedTimelineBodyHtml(timelineArr, thoughtStr, fallbackText, fallbackTrace, isStreaming, procKey, expandedMap) {
    var cleanTh = String(thoughtStr || "").replace(/^[💭⚡]\s*/, "").trim();
    if (cleanTh === "正在向云端发送指令并建立实时流…" && !isStreaming) cleanTh = "";
    var pKey = procKey ? String(procKey) : "";
    var expObj = expandedMap && typeof expandedMap === "object" ? expandedMap : {};
    var proseSegs = [];
    var steps = [];
    var lastNote = "";
    var tl = Array.isArray(timelineArr) && timelineArr.length ? timelineArr : null;
    if (tl) {
      tl.forEach(function (item) {
        if (!item) return;
        if (item.type === "text") {
          var s = stripResidualControlTags(item.text || "");
          if (s) {
            proseSegs.push(s);
            var lines = s.split(/\r?\n/).filter(function (l) { return l.trim(); });
            var tail = lines.length ? lines[lines.length - 1].trim() : "";
            lastNote = tail.length > 32 ? tail.slice(0, 31) + "…" : tail;
          }
        } else if (item.type === "tool") {
          steps.push({
            tool: String(item.tool || "tool"),
            arguments: item.arguments,
            result: item.result,
            status: String(item.status || "done"),
            duration_ms: Number(item.duration_ms || 0),
            contextNote: lastNote
          });
        }
      });
    } else if (Array.isArray(fallbackTrace) && fallbackTrace.length) {
      fallbackTrace.forEach(function (tr) {
        if (!tr) return;
        steps.push({
          tool: String(tr.tool || "tool"),
          arguments: tr.arguments,
          result: tr.result,
          status: String(tr.status || "done"),
          duration_ms: Number(tr.duration_ms || 0),
          contextNote: String(tr.step_note || "")
        });
      });
    }

    var pureProse = proseSegs.length ? proseSegs.join("\n\n") : stripResidualControlTags(fallbackText || "");
    var hasProcess = Boolean(cleanTh || steps.length);
    var parts = [];
    if (hasProcess) {
      var totalDur = steps.reduce(function (acc, st) { return acc + (st.duration_ms || 0); }, 0);
      var durSuffix = totalDur >= 1000 ? " · " + (totalDur / 1000).toFixed(1) + "s" : (totalDur > 0 ? " · " + totalDur + "ms" : "");
      var hasErr = steps.some(function (st) {
        return st.status === "error" || (st.result && (st.result.error || st.result.ok === false));
      });
      var runningStep = null;
      if (isStreaming && steps.length) {
        for (var ri = steps.length - 1; ri >= 0; ri--) {
          if (steps[ri].status === "running") { runningStep = steps[ri]; break; }
        }
        if (!runningStep) runningStep = steps[steps.length - 1];
      }
      var eyebrowTxt = "";
      if (isStreaming) {
        if (runningStep) {
          eyebrowTxt = friendlyToolLabel(runningStep.tool, runningStep.arguments) + (steps.length > 1 ? " · 第 " + steps.length + " 步" : "");
        } else {
          eyebrowTxt = "正在思考…";
        }
      } else {
        if (cleanTh && steps.length) eyebrowTxt = "已思考并执行 " + steps.length + " 步" + durSuffix;
        else if (steps.length) eyebrowTxt = "已完成 " + steps.length + " 步" + durSuffix;
        else eyebrowTxt = "思考过程";
      }
      var dotCls = hasErr ? "is-error" : (isStreaming ? "is-live" : "");
      var prevNote = "";
      var stepsHtml = steps.map(function (st, stepIdx) {
        var noteHtml = "";
        if (st.contextNote && st.contextNote !== prevNote) {
          prevNote = st.contextNote;
          noteHtml = '<div class="dsb-apple-ctx">“' + escapeHtml(st.contextNote) + '”</div>';
        }
        var dLbl = st.status === "running" ? "进行中" : (st.duration_ms >= 1000 ? (st.duration_ms / 1000).toFixed(1) + "s" : (st.duration_ms > 0 ? st.duration_ms + "ms" : ""));
        var argsJson = st.arguments ? JSON.stringify(st.arguments, null, 2).slice(0, 500) : "";
        var resJson = st.result ? JSON.stringify(st.result, null, 2).slice(0, 700) : "";
        var stErr = st.status === "error" || (st.result && (st.result.error || st.result.ok === false));
        var stepKey = pKey ? (pKey + ":s" + stepIdx) : "";
        var stepOpenAttr = (stepKey && expObj[stepKey]) ? " open" : "";
        return noteHtml +
          '<details class="dsb-apple-step' + (stErr ? " is-err" : "") + '"' + (stepKey ? (' data-proc-key="' + escapeHtml(stepKey) + '"') : '') + stepOpenAttr + '>' +
          '<summary><span class="dsb-tl-dot is-' + escapeHtml(stErr ? "error" : st.status) + '"></span>' +
          '<span class="dsb-tl-tname">' + escapeHtml(friendlyToolLabel(st.tool, st.arguments)) + '</span>' +
          (dLbl ? '<span class="dsb-tl-tdur">' + escapeHtml(dLbl) + '</span>' : '') +
          '</summary>' +
          ((argsJson && argsJson !== "{}") || (resJson && resJson !== "{}")
            ? ('<div class="dsb-tl-detail">' +
                (argsJson && argsJson !== "{}" ? '<pre class="dsb-tl-pre">参数：' + escapeHtml(argsJson) + '</pre>' : '') +
                (resJson && resJson !== "{}" ? '<pre class="dsb-tl-pre">返回：' + escapeHtml(resJson) + '</pre>' : '') +
              '</div>')
            : '') +
          '</details>';
      }).join("");
      var procOpenAttr = (pKey && expObj[pKey]) ? " open" : "";
      parts.push(
        '<details class="dsb-apple-proc' + (isStreaming ? " is-live" : "") + '"' + (pKey ? (' data-proc-key="' + escapeHtml(pKey) + '"') : '') + procOpenAttr + '>' +
        '<summary class="dsb-apple-eyebrow">' +
        '<span class="dsb-tl-dot ' + dotCls + '"></span>' +
        '<span class="dsb-apple-lbl">' + escapeHtml(eyebrowTxt) + '</span>' +
        '<span class="dsb-apple-chev">›</span>' +
        '</summary>' +
        '<div class="dsb-apple-hairline' + (isStreaming ? " is-live" : "") + '"></div>' +
        '<div class="dsb-apple-drawer">' +
        (cleanTh ? '<div class="dsb-apple-thought">' + escapeHtml(cleanTh) + '</div>' : '') +
        (stepsHtml ? '<div class="dsb-apple-rail">' + stepsHtml + '</div>' : '') +
        '</div></details>'
      );
    }
    if (pureProse) {
      parts.push('<div class="dsb-apple-prose">' + renderRichMarkdown(pureProse) + '</div>');
    } else if (isStreaming && !hasProcess) {
      parts.push('<div class="dsb-apple-prose" style="color:var(--dsb-muted);font-size:13px;">正在思考…</div>');
    }
    return '<div class="dsb-tl-stack">' + parts.join("") + '</div>';
  }

  function buildPromptPreviewSectionHtml(pv) {
    if (!pv || !pv.segments) {
      return '<div style="font-size:11.5px;color:var(--dsb-faint);padding:4px 0;">正在拉取 System Prompt 5 段装配预览…</div>';
    }
    var segs = pv.segments;
    var items = [
      ["1. DeepSeek Bot 底座指令", segs.system_base || ""],
      ["2. 置顶记忆块 (Static Pinned)", segs.pinned_block || ""],
      ["3. 记忆快照 (Vault Context)", segs.vault_context || ""],
      ["4. 角色设定与节点身份", segs.persona_and_node || ""],
      ["5. 响应校准尾段 (Calibration)", segs.calibration_tail || ""]
    ];
    var summaryText = "总计 " + Number(pv.total_chars || 0) + " 字 (~" + Number(pv.estimated_tokens || 0) + " tok)" +
      (Number(pv.last_prompt_tokens || 0) > 0 ? " · 实测 " + pv.last_prompt_tokens + " / 缓存 " + Number(pv.last_cached_tokens || 0) : "");
    return '<details style="border:0.5px solid var(--dsb-line);border-radius:12px;padding:10px;background:var(--dsb-bg);">' +
      '<summary style="cursor:pointer;font-size:12.5px;font-weight:600;color:var(--dsb-ink);display:flex;justify-content:space-between;align-items:center;">' +
      '<span>提示词装配与记忆插槽预览</span><span style="font-size:11px;color:var(--dsb-muted);font-variant-numeric:tabular-nums;">' + escapeHtml(summaryText) + "</span></summary>" +
      '<div style="display:flex;flex-direction:column;gap:8px;margin-top:10px;">' +
      items.map(function (pair, idx) {
        return '<details class="dsb-prompt-seg"' + (idx >= 3 ? " open" : "") + ">" +
          "<summary><span>" + escapeHtml(pair[0]) + '</span><span style="font-size:10.5px;color:var(--dsb-faint);font-variant-numeric:tabular-nums;">' + pair[1].length + "字</span></summary>" +
          '<pre class="dsb-prompt-pre">' + escapeHtml(pair[1] || "（空）") + "</pre></details>";
      }).join("") +
      "</div></details>";
  }

  function buildStudioSheetHtml(state, activeBot, presetColors, shapeOpts) {
    var isCreate = state.studioMode === "create";
    var previewBot = { id: isCreate ? "new" : state.activeBotId, name: state.editBotName || "新助理", color: state.editBotColor, shape: state.editBotShape };
    var modelOpts = ['<option value="">跟随系统默认模型</option>'].concat(state.availableModels.map(function (m) {
      var mid = typeof m === "string" ? m : (m.id || "");
      return '<option value="' + escapeHtml(mid) + '"' + (mid === state.editBotModel ? " selected" : "") + ">" + escapeHtml(mid) + "</option>";
    })).join("");
    return '<div class="dsb-sheet-head"><span>' + (isCreate ? "新建专属助理" : "助理工作坊 · " + escapeHtml(activeBot.name)) + '</span><button type="button" class="dsb-icon-btn" aria-label="关闭" data-close-sheet="1">' + iconSvg("close", 16) + "</button></div>" +
      '<div class="dsb-sheet-body">' +
      '<div style="display:flex;align-items:center;gap:12px;padding:10px;border-radius:14px;background:var(--dsb-elevated);">' + renderBotAvatarSvg(previewBot, 48, state.whaleAvatarUrl) +
      '<div style="flex:1;min-width:0;"><div style="font-size:14px;font-weight:650;">' + escapeHtml(previewBot.name) + '</div><div style="font-size:11.5px;color:var(--dsb-muted);">独立对话房间 · 支持档案导出与恢复</div></div>' +
      '<div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;">' +
      '<button type="button" class="dsb-btn-sec" style="height:28px;font-size:11.5px;" data-import-bot="1" aria-label="导入助理档案文件">导入档案</button>' +
      (!isCreate ? '<button type="button" class="dsb-btn-sec" style="height:28px;font-size:11.5px;" data-export-bot="1" aria-label="导出当前助理档案">导出档案</button>' : "") +
      (!isCreate ? '<button type="button" class="dsb-btn-sec" style="height:28px;font-size:11.5px;" data-open-create-bot="1">+ 新建</button>' : "") + "</div></div>" +
      '<div><label class="dsb-field-lbl">名称</label><input id="dsb-st-name" class="dsb-inp" maxlength="32" value="' + escapeHtml(state.editBotName) + '" placeholder="例如：架构师 / 论文审稿人"/></div>' +
      '<div><label class="dsb-field-lbl">职能简介</label><input id="dsb-st-desc" class="dsb-inp" maxlength="120" value="' + escapeHtml(state.editBotDesc) + '" placeholder="一句话说明这位助理擅长什么"/></div>' +
      '<div><label class="dsb-field-lbl">专属模型</label><select id="dsb-st-model" class="dsb-sel">' + modelOpts + "</select></div>" +
      '<div><label class="dsb-field-lbl">代表色</label><div class="dsb-swatch-row">' + presetColors.map(function (c) {
        var sc = safeCssColor(c, "#0A84FF");
        return '<button type="button" class="dsb-swatch' + (sc.toLowerCase() === String(state.editBotColor).toLowerCase() ? " is-sel" : "") + '" style="background:' + sc + ';" aria-label="选择代表色 ' + escapeHtml(sc) + '" data-pick-color="' + escapeHtml(sc) + '"></button>';
      }).join("") + "</div></div>" +
      '<div><label class="dsb-field-lbl">灵体形态</label><div class="dsb-shape-grid">' + shapeOpts.map(function (sh) {
        return '<button type="button" class="dsb-shape-btn' + (sh.id === state.editBotShape ? " is-sel" : "") + '" data-pick-shape="' + sh.id + '">' + renderBotAvatarSvg({ color: state.editBotColor, shape: sh.id }, 20, state.whaleAvatarUrl) + "<span>" + sh.label + "</span></button>";
      }).join("") + "</div></div>" +
      '<div><label class="dsb-field-lbl">系统设定 / 人格指令 (System Persona)</label><textarea id="dsb-st-persona" class="dsb-ta" rows="5" placeholder="定义它的角色职责、说话风格与工具边界…">' + escapeHtml(state.editBotPersona) + "</textarea></div>" +
      '<label style="display:flex;align-items:center;gap:8px;font-size:12.5px;cursor:pointer;"><input id="dsb-st-curiosity" type="checkbox"' + (state.editBotCuriosity ? " checked" : "") + "/>允许空闲时主动巡检关怀</label>" +
      (!isCreate ? ('<div id="dsb-st-prompt-slot">' + buildPromptPreviewSectionHtml(state.promptPreviewData) + "</div>") : "") +
      '</div><div class="dsb-sheet-foot">' +
      (!isCreate && !activeBot.primary && state.activeBotId !== "pc_engineer" ? '<button type="button" class="dsb-btn-danger" data-delete-bot="1">' + iconSvg("trash", 14) + "<span>删除助理</span></button>" : "<span></span>") +
      '<div style="display:flex;gap:8px;"><button type="button" class="dsb-btn-sec" data-close-sheet="1">取消</button><button type="button" class="dsb-btn-pri" data-save-bot="1">' + (isCreate ? "创建助理" : "保存设定") + "</button></div></div>";
  }

  function buildSharedFilesTabHtml(state) {
    var sf = state.sharedFiles || { local: [], cloud: [], desktop: [] };
    var loc = Array.isArray(sf.local) ? sf.local : [];
    var cld = Array.isArray(sf.cloud) ? sf.cloud : [];
    var dsk = Array.isArray(sf.desktop) ? sf.desktop : [];
    function fmtSz(b) {
      var n = Number(b || 0);
      return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n >= 1024 ? (n / 1024).toFixed(1) + " KB" : n + " B";
    }
    function renderSection(title, items, mode) {
      var rows = items.slice(0, 30).map(function (it) {
        var p = String(it.path || it.name || "");
        var nm = String(it.name || p.split("/").pop() || p);
        var btns = mode === "local"
          ? '<button type="button" class="dsb-btn-sec" style="padding:3px 8px;font-size:11px;" data-sync-file="to_cloud" data-sync-path="' + escapeHtml(it.full_path || p) + '">推VPS</button>' +
            '<button type="button" class="dsb-btn-sec" style="padding:3px 8px;font-size:11px;" data-sync-file="to_desktop" data-sync-path="' + escapeHtml(it.full_path || p) + '">推云电脑</button>'
          : (mode === "cloud"
            ? '<button type="button" class="dsb-btn-sec" style="padding:3px 8px;font-size:11px;" data-sync-file="to_pc" data-sync-path="' + escapeHtml(p) + '">拉到本机</button>' +
              '<button type="button" class="dsb-btn-sec" style="padding:3px 8px;font-size:11px;" data-sync-file="to_desktop" data-sync-path="' + escapeHtml(p) + '">推云电脑</button>'
            : '');
        return '<div class="dsb-vault-file" style="cursor:default;gap:8px;"><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + escapeHtml(p) + '">' + escapeHtml(nm) + '</span><span style="font-size:11px;color:var(--dsb-faint);flex-shrink:0;">' + fmtSz(it.bytes) + '</span><span style="display:flex;gap:4px;flex-shrink:0;">' + btns + '</span></div>';
      }).join("");
      return '<div style="display:flex;flex-direction:column;gap:4px;"><div style="font-size:11.5px;font-weight:600;color:var(--dsb-muted);">' + title + " (" + items.length + ")</div>" + (rows || '<div style="font-size:11.5px;color:var(--dsb-faint);padding:4px 0;">暂无文件</div>') + "</div>";
    }
    return '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;"><span style="font-size:12px;color:var(--dsb-muted);">自动与 VPS 及云电脑共享 uploads 文件</span><button type="button" class="dsb-btn-sec" style="padding:4px 10px;font-size:11.5px;" data-refresh-files="1">刷新列表</button></div>' +
      '<div style="max-height:290px;overflow-y:auto;display:flex;flex-direction:column;gap:10px;">' +
      renderSection("本机 uploads/", loc, "local") +
      renderSection("VPS 云工作区", cld, "cloud") +
      renderSection("云电脑 workspace", dsk, "desktop") +
      "</div>";
  }

  function buildVaultSheetHtml(state) {
    var pins = Array.isArray(state.vaultPinned) ? state.vaultPinned : [];
    var files = state.vaultFiles.map(function (f) { return typeof f === "string" ? f : (f.path || ""); }).filter(Boolean);
    var sf = state.sharedFiles || { local: [], cloud: [], desktop: [] };
    var sharedTotal = (Array.isArray(sf.local) ? sf.local.length : 0) + (Array.isArray(sf.cloud) ? sf.cloud.length : 0);
    var tabBody = state.vaultTab === "pins"
      ? ('<div style="display:flex;gap:6px;"><input id="dsb-new-pin-inp" class="dsb-inp" placeholder="输入需要跨端永久记住的偏好或铁律…"/><button type="button" class="dsb-btn-pri" data-add-pin="1">钉住</button></div>' +
        '<div style="display:flex;flex-direction:column;gap:6px;">' + (pins.length ? pins.map(function (p) {
          return '<div class="dsb-vault-file" style="cursor:default;"><span style="word-break:break-word;padding-right:8px;">' + escapeHtml(p.text || "") + '</span><button type="button" class="dsb-icon-btn" aria-label="移除置顶" data-unpin-id="' + escapeHtml(p.id || p.text) + '">' + iconSvg("trash", 14) + "</button></div>";
        }).join("") : '<div style="color:var(--dsb-faint);font-size:12.5px;text-align:center;padding:24px 0;">暂无钉住的核心记忆</div>') + "</div>")
      : (state.vaultTab === "files"
        ? buildSharedFilesTabHtml(state)
        : ('<div style="max-height:150px;overflow-y:auto;display:flex;flex-direction:column;gap:4px;">' + (files.map(function (fp) {
            var clean = fp.replace(/^memory_vault\//, "");
            return '<button type="button" class="dsb-vault-file' + (clean === state.vaultSelectedPath ? " is-sel" : "") + '" data-vault-path="' + escapeHtml(clean) + '"><span>' + escapeHtml(clean) + "</span>" + iconSvg("chevron", 12) + "</button>";
          }).join("") || '<div style="font-size:12px;color:var(--dsb-faint);">正在加载记忆档案…</div>') + "</div>" +
          (state.vaultSelectedPath ? ('<div><label class="dsb-field-lbl">' + escapeHtml(state.vaultSelectedPath) + '</label><textarea id="dsb-vault-editor" class="dsb-ta" rows="11">' + escapeHtml(state.vaultSelectedContent) + "</textarea></div>") : "")));
    return '<div class="dsb-sheet-head"><span>记忆金库与跨端文件</span><button type="button" class="dsb-icon-btn" aria-label="关闭" data-close-sheet="1">' + iconSvg("close", 16) + "</button></div>" +
      '<div class="dsb-sheet-body"><div class="dsb-vault-tabs">' +
      '<button type="button" class="dsb-filter-chip' + (state.vaultTab === "notes" ? " is-active" : "") + '" data-vault-tab="notes">Markdown 档案 (' + files.length + ")</button>" +
      '<button type="button" class="dsb-filter-chip' + (state.vaultTab === "pins" ? " is-active" : "") + '" data-vault-tab="pins">置顶记忆 (' + pins.length + ")</button>" +
      '<button type="button" class="dsb-filter-chip' + (state.vaultTab === "files" ? " is-active" : "") + '" data-vault-tab="files">跨端文件 (' + sharedTotal + ")</button></div>" +
      tabBody + '</div><div class="dsb-sheet-foot"><span style="font-size:11.5px;color:var(--dsb-muted);">' + escapeHtml(state.vaultStatusMsg) + '</span>' +
      (state.vaultTab === "notes" && state.vaultSelectedPath ? '<button type="button" class="dsb-btn-pri" data-save-vault="1">保存文档</button>' : '<button type="button" class="dsb-btn-sec" data-close-sheet="1">完成</button>') +
      "</div>";
  }

  function buildMessageRowHtml(m, parsedUser, expandedMapOrBool) {
    if (m.kind === "compact_summary") {
      var meta = {};
      try { meta = JSON.parse(m.decision || "{}"); } catch (e) {}
      var bTok = Number(meta.before_tokens || 0), aTok = Number(meta.after_tokens || 0);
      return '<div class="dsb-compact-card" data-msg-id="' + escapeHtml(m.id || "") + '"><div class="dsb-compact-top"><span class="dsb-compact-tag">' + escapeHtml(meta.mode || "上下文浓缩归档") + '</span><span style="color:var(--dsb-faint);">' +
        (bTok > 0 ? formatCompactTokens(bTok) + " → " + formatCompactTokens(aTok) + " · " : "") + escapeHtml(String(m.time || "").slice(0, 5)) + "</span></div>" +
        '<details class="dsb-accordion" style="border:none;background:transparent;"><summary style="font-size:12px;color:var(--dsb-accent);font-weight:500;">展开查看浓缩摘要正文</summary>' +
        '<div style="padding-top:6px;font-size:13px;">' + renderRichMarkdown(m.text) + "</div></details></div>";
    }
    var isInboundCollab = m.kind === "bot_message";
    var isCollabReply = m.kind === "bot_reply";
    var isUser = m.role === "user" && !isInboundCollab;
    var timeLabel = String(m.time || "").slice(0, 5);
    if (isUser) {
      var parsed = parsedUser || { quote: "", body: String(m.text || ""), files: [] };
      var msgAtts = Array.isArray(m.attachments) ? m.attachments : [];
      var allPaths = [];
      var seenP = {};
      parsed.files.concat(msgAtts.map(function (a) { return a && a.path ? a.path : ""; })).forEach(function (fp) {
        var clean = String(fp || "").trim();
        if (clean && !seenP[clean]) { seenP[clean] = true; allPaths.push(clean); }
      });
      var imgPaths = allPaths.filter(isImageMediaPath);
      var docPaths = allPaths.filter(function (fp) { return !isImageMediaPath(fp); });
      var userStickers = [];
      if (m.sticker && String(m.sticker).indexOf("/stickers/") !== -1) userStickers.push(String(m.sticker));
      if (Array.isArray(m.stickers)) {
        m.stickers.forEach(function (st) {
          var sv = typeof st === "string" ? st : (st && (st.url || st.path) ? String(st.url || st.path) : "");
          if (sv && sv.indexOf("/stickers/") !== -1 && userStickers.indexOf(sv) === -1) userStickers.push(sv);
        });
      }
      var userStickerHtml = userStickers.map(function (stUrl) {
        return '<img class="dsb-sticker-img is-user" src="' + escapeHtml(resolveChatMediaUrl(stUrl, false)) + '" alt="表情贴纸"/>';
      }).join("");
      var userImgHtml = renderMediaGridHtml(imgPaths, true);
      var userDocHtml = docPaths.length ? ('<div class="dsb-msg-file-list is-end">' + docPaths.map(function (fp) {
        var meta = msgAtts.find(function (a) { return a && a.path === fp; });
        return renderFileAttachmentCardHtml(fp, meta);
      }).join("") + "</div>") : "";
      var isPureStickerText = userStickers.length > 0 && /^\[(sticker|表情|贴纸)\s*[:：][^\]]+\]$/i.test(String(parsed.body || "").trim());
      var effectiveUserBody = isPureStickerText ? "" : parsed.body;
      var hasUserBubble = Boolean(parsed.quote || effectiveUserBody || (!allPaths.length && !userStickers.length));
      var userBubbleHtml = hasUserBubble
        ? ('<div class="dsb-bubble" data-dbl-reply="' + escapeHtml(m.id) + '">' +
          (parsed.quote ? '<div class="dsb-quote-box">' + escapeHtml(parsed.quote) + "</div>" : "") +
          (effectiveUserBody ? renderRichMarkdown(effectiveUserBody) : "") +
          '<div class="dsb-bubble-foot"><span>' + escapeHtml(timeLabel) + "</span></div></div>")
        : ('<div class="dsb-media-time-foot"><span>' + escapeHtml(timeLabel) + "</span></div>");
      return '<div class="dsb-msg-row is-user" data-msg-id="' + escapeHtml(m.id) + '">' +
        userStickerHtml + userImgHtml + userDocHtml + userBubbleHtml +
        '<div class="dsb-msg-actions">' +
        '<button type="button" class="dsb-act-btn" data-act="copy" data-mid="' + escapeHtml(m.id) + '">' + iconSvg("copy", 12) + "<span>复制</span></button>" +
        '<button type="button" class="dsb-act-btn" data-act="reply" data-mid="' + escapeHtml(m.id) + '">' + iconSvg("reply", 12) + "<span>引用</span></button>" +
        '<button type="button" class="dsb-act-btn" data-act="edit" data-mid="' + escapeHtml(m.id) + '">' + iconSvg("edit", 12) + "<span>编辑</span></button>" +
        '<button type="button" class="dsb-act-btn" data-act="reroll" data-mid="' + escapeHtml(m.id) + '">' + iconSvg("refresh", 12) + "<span>重发</span></button>" +
        "</div></div>";
    }
    var traceArr = isInboundCollab ? [] : (Array.isArray(m.tool_trace) ? m.tool_trace : []);
    var stepsArr = isInboundCollab ? [] : (Array.isArray(m.turn_steps) ? m.turn_steps : []);
    var thoughtStr = isInboundCollab ? "" : String(m.thought || "");
    var collabBanner = isInboundCollab
      ? ('<div class="dsb-collab-head">' + iconSvg("share", 13) + "<span>来自「" + escapeHtml(m.from_bot_name || m.from_bot_id || "同事助理") + "」的协作委托</span></div>")
      : (isCollabReply && m.to_bot_name ? ('<div class="dsb-collab-head">' + iconSvg("share", 13) + "<span>回传给「" + escapeHtml(m.to_bot_name) + "」的协作结论</span></div>") : "");
    var validStickers = [];
    var pushAiSticker = function (rawS) {
      var sv = String(rawS || "").trim();
      if (sv && !/\/路径$/.test(sv) && validStickers.indexOf(sv) === -1) validStickers.push(sv);
    };
    if (m.sticker) pushAiSticker(m.sticker);
    if (Array.isArray(m.stickers)) {
      m.stickers.forEach(function (st) {
        pushAiSticker(typeof st === "string" ? st : (st && (st.url || st.path) ? (st.url || st.path) : ""));
      });
    }
    var validSticker = validStickers[0] || "";
    var stickerHtml = validStickers.map(function (stUrl) {
      return stUrl.indexOf("/stickers/") !== -1
        ? ('<img class="dsb-sticker-img" src="' + escapeHtml(resolveChatMediaUrl(stUrl, false)) + '" alt="表情贴纸"/>')
        : ('<img class="dsb-chat-img" data-lightbox-src="' + escapeHtml(resolveChatMediaUrl(stUrl, false)) + '" src="' + escapeHtml(resolveChatMediaUrl(stUrl, false)) + '" alt="图像"/>');
    }).join("");
    var aiAtts = Array.isArray(m.attachments) ? m.attachments : [];
    var aiImgPaths = aiAtts.filter(function (a) { return a && a.path && isImageMediaPath(a.path) && validStickers.indexOf(a.path) === -1; }).map(function (a) { return a.path; });
    var aiDocAtts = aiAtts.filter(function (a) { return a && a.path && !isImageMediaPath(a.path); });
    var aiAttImgHtml = renderMediaGridHtml(aiImgPaths, false);
    var aiAttDocHtml = aiDocAtts.length ? ('<div class="dsb-msg-file-list">' + aiDocAtts.map(function (a) { return renderFileAttachmentCardHtml(a.path, a); }).join("") + "</div>") : "";
    var perfParts = [];
    if (Number(m.cache_hit_pct) > 0) perfParts.push("缓存" + Math.round(Number(m.cache_hit_pct)) + "%");
    if (Number(m.ttft_ms) > 0) {
      var ttftVal = Number(m.ttft_ms);
      perfParts.push(ttftVal >= 1000 ? ("首字" + (ttftVal / 1000).toFixed(1) + "s") : ("首字" + Math.round(ttftVal) + "ms"));
    }
    if (Number(m.tps) > 0) perfParts.push(Number(m.tps).toFixed(0) + "t/s");
    if (Number(m.api_calls) > 1) perfParts.push(Number(m.api_calls) + "轮");
    var telemetryPillHtml = perfParts.length ? ('<span class="dsb-hover-telemetry">' + escapeHtml(perfParts.join(" · ")) + '</span>') : "";
    var choicesHtml = Array.isArray(m.choices) && m.choices.length ? ('<div class="dsb-choices-wrap">' + m.choices.map(function (c) {
      var lbl = (c && (c.label || c.text || c)) || "", val = (c && (c.value || c.label || c)) || "";
      return '<button type="button" class="dsb-choice-btn" data-send-choice="' + escapeHtml(val) + '">' + escapeHtml(lbl) + "</button>";
    }).join("") + "</div>") : "";
    var tlArr = isInboundCollab ? [] : (Array.isArray(m.timeline) ? m.timeline : []);
    var hasTimelineView = Boolean(tlArr.length || traceArr.length || thoughtStr || String(m.text || "").trim());
    var expMap = (expandedMapOrBool && typeof expandedMapOrBool === "object")
      ? expandedMapOrBool
      : (expandedMapOrBool && m.id ? (function () { var o = {}; o[m.id] = true; return o; })() : {});
    var bubbleBodyHtml = hasTimelineView
      ? renderUnifiedTimelineBodyHtml(tlArr, thoughtStr, m.text || "", traceArr, false, m.id || "", expMap)
      : "";
    var varsArr = !isInboundCollab && Array.isArray(m.variants) && m.variants.length > 1 ? m.variants : null;
    var activeVIdx = varsArr ? Math.max(0, Math.min(varsArr.length - 1, Number(m.active_variant_idx !== undefined ? m.active_variant_idx : varsArr.length - 1))) : 0;
    var variantBarHtml = "";
    if (varsArr) {
      var prevVIdx = activeVIdx > 0 ? activeVIdx - 1 : varsArr.length - 1;
      var nextVIdx = (activeVIdx + 1) % varsArr.length;
      var numPills = varsArr.length <= 6
        ? varsArr.map(function (_, idx) {
            return '<button type="button" class="dsb-var-btn' + (idx === activeVIdx ? " is-active" : "") + '" data-switch-variant="1" data-mid="' + escapeHtml(m.id) + '" data-vidx="' + idx + '" title="切换到第 ' + (idx + 1) + ' 版回复">' + (idx + 1) + "</button>";
          }).join("")
        : ('<span class="dsb-var-count">' + (activeVIdx + 1) + " / " + varsArr.length + "</span>");
      variantBarHtml = '<div class="dsb-variant-bar" aria-label="回复版本切换">' +
        '<button type="button" class="dsb-var-nav" data-switch-variant="1" data-mid="' + escapeHtml(m.id) + '" data-vidx="' + prevVIdx + '" title="上一版">‹</button>' +
        numPills +
        '<button type="button" class="dsb-var-nav" data-switch-variant="1" data-mid="' + escapeHtml(m.id) + '" data-vidx="' + nextVIdx + '" title="下一版">›</button>' +
        "</div>";
    }
    var actionsBarHtml = !isInboundCollab ? ('<div class="dsb-msg-actions">' +
        '<button type="button" class="dsb-act-btn" data-act="copy" data-mid="' + escapeHtml(m.id) + '">' + iconSvg("copy", 12) + "<span>复制</span></button>" +
        '<button type="button" class="dsb-act-btn" data-act="reply" data-mid="' + escapeHtml(m.id) + '">' + iconSvg("reply", 12) + "<span>引用</span></button>" +
        '<button type="button" class="dsb-act-btn" data-act="reroll" data-mid="' + escapeHtml(m.id) + '">' + iconSvg("refresh", 12) + "<span>重答</span></button>" +
        '<button type="button" class="dsb-act-btn" data-act="pin" data-mid="' + escapeHtml(m.id) + '">' + iconSvg("pin", 12) + "<span>钉住</span></button>" +
        telemetryPillHtml + "</div>") : "";
    var footToolsHtml = (variantBarHtml || actionsBarHtml)
      ? ('<div class="dsb-ai-foot-row">' + variantBarHtml + actionsBarHtml + "</div>")
      : "";
    return '<div class="dsb-msg-row is-ai' + (isInboundCollab || isCollabReply ? " is-collab" : "") + (m.kind === "error" ? " is-err" : "") + '" data-msg-id="' + escapeHtml(m.id) + '">' +
      stickerHtml + aiAttImgHtml +
      (hasTimelineView ? ('<div class="dsb-bubble" data-dbl-reply="' + escapeHtml(m.id) + '">' + collabBanner + bubbleBodyHtml +
        (timeLabel ? '<div class="dsb-bubble-foot"><span>' + escapeHtml(timeLabel) + "</span></div>" : "") + "</div>") : "") +
      aiAttDocHtml +
      choicesHtml + footToolsHtml + "</div>";
  }

  function buildHeaderBotPillsHtml(bots, activeBotId, streamsByBot, sendingByBot) {
    return (bots || []).map(function (b) {
      var isAct = b.id === activeBotId;
      var isRun = Boolean((streamsByBot && streamsByBot[b.id]) || (sendingByBot && sendingByBot[b.id]));
      var dotCol = safeCssColor(b.color, "#0A84FF");
      return '<button type="button" class="dsb-qbot-pill' + (isAct ? " is-active" : "") + (isRun ? " is-running" : "") + '" data-select-bot="' + escapeHtml(b.id) + '" title="' + escapeHtml(b.description || b.name) + '">' +
        '<span class="dsb-qbot-dot" style="background:' + dotCol + ';"></span><span>' + escapeHtml(b.name) + '</span>' +
        (isRun ? '<span class="dsb-qbot-pulse"></span>' : "") + '</button>';
    }).join("");
  }

  function exportBotArchive(apiCall, botId, showToast) {
    showToast("正在打包助理档案…");
    apiCall("/local/bots/export", "POST", { bot_id: botId, include_messages: true }).then(function (r) {
      if (!r || !r.ok || !r.bundle) { showToast((r && r.error) || "导出失败"); return; }
      var fn = String(r.filename || (botId + ".dsb-bot.json"));
      var blob = new Blob([JSON.stringify(r.bundle, null, 2)], { type: "application/json;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url; a.download = fn; document.body.appendChild(a); a.click();
      setTimeout(function () { if (a.parentNode) a.parentNode.removeChild(a); URL.revokeObjectURL(url); }, 400);
      showToast("已导出档案：" + fn);
    });
  }

  function pickAndImportBotArchive(apiCall, onDone, showToast) {
    var inp = document.createElement("input");
    inp.type = "file"; inp.accept = ".json,.dsb-bot.json,application/json"; inp.style.display = "none";
    inp.addEventListener("change", function () {
      var f = inp.files && inp.files[0];
      if (inp.parentNode) inp.parentNode.removeChild(inp);
      if (!f) return;
      var reader = new FileReader();
      reader.onload = function () {
        var parsed = null;
        try { parsed = JSON.parse(String(reader.result || "")); } catch (e) { showToast("档案文件不是合法的 JSON"); return; }
        showToast("正在导入助理档案…");
        apiCall("/local/bots/import", "POST", { bundle: parsed, mode: "merge", import_messages: true }).then(function (r) {
          if (!r || !r.ok) { showToast((r && r.error) || "导入失败"); return; }
          if (typeof onDone === "function") onDone(r);
          showToast("已导入助理「" + ((r.bot && r.bot.name) || r.bot_id || "") + "」");
        });
      };
      reader.readAsText(f, "utf-8");
    });
    document.body.appendChild(inp);
    inp.click();
  }

  function buildStickerPopoverHtml(packs, activePackId, loading) {
    var list = Array.isArray(packs) ? packs.filter(function (p) { return p && p.enabled !== false && Array.isArray(p.stickers) && p.stickers.length; }) : [];
    if (loading && !list.length) return '<div class="dsb-stk-empty"><span class="dsb-mini-spin"></span><span>加载表情包…</span></div>';
    if (!list.length) return '<div class="dsb-stk-empty">暂无可用贴纸包</div>';
    var activePack = list.find(function (p) { return p.id === activePackId; }) || list[0];
    var tabsHtml = list.length > 1 ? ('<div class="dsb-stk-tabs" role="tablist">' + list.map(function (pk) {
      var isAct = pk.id === activePack.id;
      return '<button type="button" role="tab" aria-selected="' + (isAct ? "true" : "false") + '" class="dsb-stk-tab' + (isAct ? " is-active" : "") + '" data-sticker-pack="' + escapeHtml(pk.id || "") + '">' + escapeHtml(pk.name || pk.id || "表情包") + '</button>';
    }).join("") + '</div>') : '<div class="dsb-stk-head">' + escapeHtml(activePack.name || "表情贴纸") + '</div>';
    var gridHtml = '<div class="dsb-stk-grid">' + activePack.stickers.map(function (st) {
      var code = String(st.code || st.id || "").trim();
      var label = String(st.label || code || "贴纸").trim();
      var url = String(st.url || "").trim();
      if (!code || !url) return "";
      return '<button type="button" class="dsb-stk-btn" data-send-sticker="' + escapeHtml(code) + '" data-sticker-url="' + escapeHtml(url) + '" title="' + escapeHtml(label) + '" aria-label="发送表情 ' + escapeHtml(label) + '">' +
        '<img src="' + escapeHtml(resolveChatMediaUrl(url, false)) + '" alt="' + escapeHtml(label) + '" loading="lazy"/><span class="dsb-stk-lbl">' + escapeHtml(label) + '</span></button>';
    }).join("") + '</div>';
    return tabsHtml + gridHtml;
  }

  function buildEmptyStateHtml(bot, whaleAvatarUrl) {
    var kind = String((bot && (bot.kind || bot.id)) || "").toLowerCase();
    var starters = kind.indexOf("research") !== -1 ? [
      { label: "深度调研某个技术方案与开源竞品", prompt: "帮我系统调研一下当前主流方案的优缺点与适用场景，给出核心对比结论：" },
      { label: "汇总阅读一份资料或网页并提炼要点", prompt: "请帮我抓紧提炼下面这份资料的核心观点、关键数据与潜在问题：" },
      { label: "起草一份结构严谨的分析报告框架", prompt: "请围绕下面这个主题，帮我设计一份清晰的研究分析报告提纲：" }
    ] : (kind.indexOf("review") !== -1 ? [
      { label: "对当前改动做一次全面安全与边界审校", prompt: "请对近期代码改动做一次严格的代码审查，重点排查并发竞争、空值和边界情况。" },
      { label: "检查架构设计里有没有隐藏隐患或死代码", prompt: "请审视这一版设计方案或模块结构，指出最容易踩坑的地方和简化建议。" },
      { label: "排查为什么某项测试或行为不如预期", prompt: "我遇到一个奇怪的根因问题，请帮我按证据链定位真正触发原因：" }
    ] : (kind.indexOf("companion") !== -1 || kind.indexOf("whale") !== -1 ? [
      { label: "润色一段表达或把想法整理成文稿", prompt: "帮我把下面这段零散想法润色成自然得体、逻辑清晰的表达：" },
      { label: "列出今天接下来的待办与轻重缓急", prompt: "帮我梳理今天需要推进的事情，按优先级排出一个轻松不焦虑的节奏。" },
      { label: "打开记忆金库看看最近的长期备注", prompt: "/pins" }
    ] : [
      { label: "检查中枢连接、节点与上下文健康状态", prompt: "/status" },
      { label: "排查代码或终端报错并直接给出修复", prompt: "请查看下面报错与上下文，定位根因并给出最小可验证修复：" },
      { label: "在手机虚拟屏或云电脑里协同操作一项任务", prompt: "帮我在协同工作区里查看当前界面状态并继续推进这项操作：" }
    ]));
    var chips = starters.map(function (it) {
      return '<button type="button" class="dsb-starter-chip" data-send-starter="' + escapeHtml(it.prompt) + '"><span>' + escapeHtml(it.label) + '</span><span>›</span></button>';
    }).join("");
    return '<div class="dsb-empty-state" data-empty-bot="' + escapeHtml((bot && bot.id) || "") + '">' +
      renderBotAvatarSvg(bot, 52, whaleAvatarUrl) +
      '<div class="dsb-empty-title">' + escapeHtml((bot && bot.name) || "DeepSeek Bot") + '</div>' +
      '<div class="dsb-empty-sub">' + escapeHtml((bot && bot.description) || "独立会话与思维链空间，支持拖入图片文件与 Ctrl+K 快捷切换。") + '</div>' +
      '<div class="dsb-starter-grid">' + chips + "</div></div>";
  }

  function buildHeaderMoreMenuHtml(state) {
    var st = state || {};
    var themeSub = !st.themeManual ? "跟随系统" : (st.theme === "dark" ? "深色" : "浅色");
    return '<button type="button" class="dsb-more-item" data-more-act="theme"><span class="dsb-more-item-main">' + iconSvg(st.theme === "dark" ? "moon" : "sun", 14) + '<span>外观主题</span></span><span class="dsb-more-item-sub">' + escapeHtml(themeSub) + '</span></button>' +
      '<button type="button" class="dsb-more-item" data-more-act="files"><span class="dsb-more-item-main">' + iconSvg("paperclip", 14) + '<span>跨端共享文件</span></span><span class="dsb-more-item-sub">三端</span></button>' +
      '<button type="button" class="dsb-more-item" data-more-act="compact"><span class="dsb-more-item-main">' + iconSvg("compress", 14) + '<span>浓缩归档上下文</span></span><span class="dsb-more-item-sub">/compact</span></button>' +
      '<div class="dsb-more-sep"></div>' +
      '<button type="button" class="dsb-more-item" data-more-act="create"><span class="dsb-more-item-main">' + iconSvg("plus", 14) + '<span>新建专属助理</span></span></button>' +
      '<button type="button" class="dsb-more-item" data-more-act="cmdk"><span class="dsb-more-item-main">' + iconSvg("command", 14) + '<span>快捷命令面板</span></span><span class="dsb-more-item-sub">Ctrl+K</span></button>';
  }

  function getCommandPaletteItems(query, state, slashCommands) {
    var q = String(query || "").trim().toLowerCase(), st = state || {}, items = [];
    (st.bots || []).forEach(function (b) {
      var hay = (String(b.name || "") + " " + String(b.description || "")).toLowerCase();
      if (!q || hay.indexOf(q) !== -1) {
        items.push({ kind: "bot", id: b.id, title: b.name, sub: b.id === st.activeBotId ? "当前对话" : (b.description || ""), color: safeCssColor(b.color, "#0A84FF") });
      }
    });
    var actions = [
      { id: "vault", ico: "vault", title: "打开记忆金库", sub: "Markdown 笔记与置顶规则" },
      { id: "files", ico: "paperclip", title: "查看跨端共享文件", sub: "本机 / VPS / 云电脑" },
      { id: "studio", ico: "sliders", title: "当前助理设置", sub: "名称、模型与 System Prompt" },
      { id: "create", ico: "plus", title: "新建专属助理", sub: "创建独立对话房间" },
      { id: "theme", ico: st.theme === "dark" ? "moon" : "sun", title: "切换日夜外观主题", sub: !st.themeManual ? "跟随系统" : (st.theme === "dark" ? "当前深色" : "当前浅色") },
      { id: "compact", ico: "compress", title: "浓缩归档上下文", sub: "释放 Token 窗口并写入金库" }
    ];
    actions.forEach(function (a) {
      if (!q || (a.title + " " + a.sub).toLowerCase().indexOf(q) !== -1) items.push({ kind: "act", id: a.id, ico: a.ico, title: a.title, sub: a.sub });
    });
    (slashCommands || []).forEach(function (sc) {
      var hay = (String(sc.cmd || "") + " " + String(sc.desc || "")).toLowerCase();
      if (!q || hay.indexOf(q) !== -1) items.push({ kind: "slash", id: sc.cmd, ico: "command", title: String(sc.cmd || "").trim(), sub: sc.desc || "" });
    });
    return items.slice(0, 14);
  }

  function buildCommandPaletteHtml(query, state, slashCommands, selectedIdx) {
    var items = getCommandPaletteItems(query, state, slashCommands);
    if (!items.length) return '<div style="padding:26px 12px;text-align:center;color:var(--dsb-faint);font-size:12.5px;">没有匹配的助理或快捷指令</div>';
    var sel = Math.max(0, Math.min(items.length - 1, Number(selectedIdx || 0))), lastSec = "";
    return items.map(function (it, idx) {
      var sec = it.kind === "bot" ? "切换助理" : (it.kind === "act" ? "工作台操作" : "斜杠指令");
      var hdr = sec !== lastSec ? '<div class="dsb-cmdk-sec">' + sec + "</div>" : "";
      lastSec = sec;
      var badge = it.kind === "bot" ? '<span class="dsb-qbot-dot" style="background:' + it.color + ';"></span>' : iconSvg(it.ico, 14);
      return hdr + '<button type="button" role="option" aria-selected="' + (idx === sel ? "true" : "false") + '" class="dsb-cmdk-item' + (idx === sel ? " is-selected" : "") + '" data-cmdk-idx="' + idx + '"><span class="dsb-cmdk-left">' + badge + '<span>' + escapeHtml(it.title) + '</span></span><span class="dsb-cmdk-sub">' + escapeHtml(it.sub) + "</span></button>";
    }).join("");
  }

  window.__DSB_RENDER__ = {
    PULSE_SHAPE_PATHS: PULSE_SHAPE_PATHS,
    brandMarkSvg: brandMarkSvg,
    iconSvg: iconSvg,
    escapeHtml: escapeHtml,
    sanitizeHref: sanitizeHref,
    safeCssColor: safeCssColor,
    resolveChatMediaUrl: resolveChatMediaUrl,
    renderRichMarkdown: renderRichMarkdown,
    renderBotAvatarSvg: renderBotAvatarSvg,
    formatCompactTokens: formatCompactTokens,
    friendlyToolLabel: friendlyToolLabel,
    humanizeToolName: humanizeToolName,
    stripResidualControlTags: stripResidualControlTags,
    renderToolCapsuleHtml: renderToolCapsuleHtml,
    renderThoughtPillHtml: renderThoughtPillHtml,
    renderTurnProcessHtml: renderTurnProcessHtml,
    renderUnifiedTimelineBodyHtml: renderUnifiedTimelineBodyHtml,
    buildPromptPreviewSectionHtml: buildPromptPreviewSectionHtml,
    buildStickerPopoverHtml: buildStickerPopoverHtml,
    buildStudioSheetHtml: buildStudioSheetHtml,
    buildVaultSheetHtml: buildVaultSheetHtml,
    buildEmptyStateHtml: buildEmptyStateHtml,
    buildHeaderMoreMenuHtml: buildHeaderMoreMenuHtml,
    getCommandPaletteItems: getCommandPaletteItems,
    buildCommandPaletteHtml: buildCommandPaletteHtml,
    buildMessageRowHtml: buildMessageRowHtml,
    buildHeaderBotPillsHtml: buildHeaderBotPillsHtml,
    exportBotArchive: exportBotArchive,
    pickAndImportBotArchive: pickAndImportBotArchive
  };
})();
