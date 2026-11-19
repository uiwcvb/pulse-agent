/**
 * DSB 桌面端伴侣与双栏全屏工作台 (`client.js`) — 右侧磁性入口、快捷切换栏、Today 编排与严密 1-on-1 流式工作台。
 * Export: `window.__ModuleLoader__.load({ id: "dsh-deepseek-claw", factory })` -> `{ name, apply(ctx) }`
 */
window.__ModuleLoader__.load({
  id: "dsh-deepseek-claw",
  factory: function (require) {
    "use strict";
    var cordisClient = null;
    try { cordisClient = require("@cordisjs/client"); } catch (e) {}
    var React = null;
    try { React = require("react"); } catch (e) {}
    var hostLayout = null;
    var activeSlotHost = null;

    var DEFAULT_BOTS = [
      { id: "pc_engineer", name: "司工", color: "#0A84FF", shape: "square", primary: true, kind: "engineer", description: "电脑端主控总工程师，代码架构、Git Bash 与云电脑操控" },
      { id: "pc_research", name: "探微", color: "#3478F6", shape: "circle", primary: false, kind: "research", description: "深度技术调研、文献考据与源码审计专家" },
      { id: "pc_reviewer", name: "审校", color: "#FF8A3D", shape: "diamond", primary: false, kind: "reviewer", description: "独立代码审查、漏洞审视与方案验收" },
      { id: "pc_companion", name: "云汐", color: "#2EC4B6", shape: "flower", primary: false, kind: "companion", description: "轻快灵感陪伴、文稿润色与日常协理" }
    ];
    var PRESET_COLORS = ["#0A84FF", "#B568F7", "#2EC4B6", "#3478F6", "#FF8A3D", "#30D158", "#FF453A", "#5E5CE6", "#FF375F"];
    var SHAPE_OPTIONS = [{ id: "flower", label: "花朵" }, { id: "circle", label: "正圆" }, { id: "oval", label: "椭圆" }, { id: "square", label: "方角" }, { id: "pill", label: "胶囊" }, { id: "triangle", label: "三角" }, { id: "diamond", label: "菱形" }, { id: "star", label: "星芒" }, { id: "drop", label: "水滴" }, { id: "whale", label: "鲸跃" }, { id: "whalechan", label: "鲸娘" }];
    var SLASH_COMMANDS = [
      { cmd: "/goal ", desc: "启动引擎级持久目标模式，自动推进直至完成" }, { cmd: "/compact", desc: "立即浓缩归档当前对话历史，释放上下文空间" },
      { cmd: "/pin ", desc: "将关键偏好或规则钉入永久置顶记忆" }, { cmd: "/pins", desc: "查看所有已钉住的跨端核心记忆" },
      { cmd: "/status", desc: "查看当前中枢、节点在线率与上下文指标" }, { cmd: "/doctor", desc: "运行全链路通道健康自检" }, { cmd: "/undo", desc: "撤回上一轮对话记录" }
    ];
    var savedTheme = (typeof document !== "undefined" && document.body && document.body.hasAttribute("data-ds-dark-theme"))
      ? "dark"
      : ((window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light");
    var savedThemeManual = false;
    try {
      var rawT = window.localStorage && window.localStorage.getItem("dsb_theme");
      if (rawT === "light" || rawT === "dark") { savedTheme = rawT; savedThemeManual = true; }
    } catch (e) {}
    var state = {
      bots: DEFAULT_BOTS.slice(), activeBotId: "pc_engineer", viewActive: false, fullWorkspace: true,
      theme: savedTheme, themeManual: savedThemeManual, paneWidth: 420, botSearch: "", botFilter: "all", botSwitcherOpen: false,
      pinBarExpanded: false, messages: [], messagesEpoch: 1, streamsByBot: {}, sendingByBot: {}, botContextById: {},
      vpsOnline: true, rttMs: 0, phoneOnline: false, editingMsgId: null, editingText: "", replyingToMsg: null,
      pendingAttachments: [], uploadingCount: 0, slashMenuOpen: false, lightboxUrl: "", toastMsg: "",
      studioOpen: false, studioMode: "edit", editBotName: "", editBotDesc: "", editBotColor: "#0A84FF",
      editBotShape: "square", editBotPersona: "", editBotModel: "", editBotCuriosity: false, availableModels: [],
      promptPreviewData: null, promptPreviewLoading: false,
      vaultOpen: false, vaultTab: "notes", vaultCategory: "all", vaultQuery: "", vaultFiles: [], vaultPinned: [],
      sharedFiles: { local: [], cloud: [], desktop: [] },
      vaultSelectedPath: "", vaultSelectedContent: "", vaultLoading: false, vaultStatusMsg: "",
      expandedProcessIds: {},
      compactBusy: false, whaleAvatarUrl: "", lastStreamSig: "", lastLiveSig: "", isComposing: false,
      lastCompositionEndAt: 0, recordingVoice: false, transcribingVoice: false, voiceStatusHint: ""
    };
    var voiceRecorder = { stream: null, audioCtx: null, source: null, processor: null, chunks: [] }, toastTimer = null;

    function R() { return window.__DSB_RENDER__ || null; }
    function escapeHtml(s) { return R() ? R().escapeHtml(s) : String(s === null || s === undefined ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
    function iconSvg(name, sz, cls) { return R() ? R().iconSvg(name, sz, cls) : ""; }
    function resolveChatMediaUrl(u) { return R() ? R().resolveChatMediaUrl(u) : String(u || ""); }
    function renderRichMarkdown(t) { return R() ? R().renderRichMarkdown(t) : escapeHtml(t); }
    function renderBotAvatarSvg(bot, sz) {
      if (R()) return R().renderBotAvatarSvg(bot, sz, state.whaleAvatarUrl);
      return '<span style="width:' + (sz || 22) + "px;height:" + (sz || 22) + "px;border-radius:50%;background:" + escapeHtml((bot && bot.color) || "#B568F7") + ';display:inline-block;flex-shrink:0;"></span>';
    }
    function formatCompactTokens(n) { return R() ? R().formatCompactTokens(n) : String(n || 0); }
    function friendlyToolLabel(n, a) { return R() ? R().friendlyToolLabel(n, a) : String(n || ""); }
    function renderToolCapsuleHtml(tr, run, lbl) { return R() ? R().renderToolCapsuleHtml(tr, run, lbl) : ""; }
    function renderThoughtPillHtml(th, calls, stream) { return R() ? R().renderThoughtPillHtml(th, calls, stream) : ""; }
    function getActiveBot() {
      for (var i = 0; i < state.bots.length; i++) { if (state.bots[i].id === state.activeBotId) return state.bots[i]; }
      return state.bots[0] || DEFAULT_BOTS[0];
    }

    function injectStyles(fullCss) {
      var el = document.getElementById("dsb-companion-styles");
      if (!el) {
        el = document.createElement("style"); el.id = "dsb-companion-styles";
        el.textContent = '#dsb-split-pane{display:none;width:100%;height:100%;flex:1;min-width:0;min-height:0;}#dsb-split-pane.is-open{display:flex;}';
        document.head.appendChild(el);
      }
      if (fullCss && el.getAttribute("data-loaded") !== "1") { el.textContent = fullCss; el.setAttribute("data-loaded", "1"); }
    }

    function showToast(msg) {
      state.toastMsg = String(msg || "");
      var el = document.getElementById("dsb-toast-el");
      if (el) { el.textContent = state.toastMsg; el.style.display = state.toastMsg ? "block" : "none"; }
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(function () { state.toastMsg = ""; var e2 = document.getElementById("dsb-toast-el"); if (e2) e2.style.display = "none"; }, 2400);
    }
    function U() { return window.__DSB_CLIENT_UI__ || null; }
    function uiCtx() {
      return {
        state: state, apiCall: apiCall, showToast: showToast, autoFitTextarea: autoFitTextarea,
        renderComposerBanners: renderComposerBanners, updateComposerButtons: updateComposerButtons,
        renderSheets: renderSheets, renderHeaderAndPins: renderHeaderAndPins,
        renderHomeRail: renderHomeRail, renderRightLauncher: renderRightLauncher, getActiveBot: getActiveBot,
        escapeHtml: escapeHtml, getRoomMessages: getRoomMessages, friendlyToolLabel: friendlyToolLabel,
        renderBotAvatarSvg: renderBotAvatarSvg, PRESET_COLORS: PRESET_COLORS,
      };
    }
    function apiCall(localPath, method, body) {
      var payload = JSON.stringify({ localPath: localPath, cloudPath: "", method: method || "POST", body: body || {} });
      return fetch("/api/dsh-deepseek-claw", { method: "POST", headers: { "Content-Type": "application/json" }, body: payload })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)); })
        .catch(function () {
          return fetch("http://127.0.0.1:18195/api/dsh-deepseek-claw", { method: "POST", headers: { "Content-Type": "application/json" }, body: payload })
            .then(function (r2) { return r2.ok ? r2.json() : Promise.reject(new Error("Gateway HTTP " + r2.status)); });
        })
        .catch(function (err) { return { ok: false, error: String((err && err.message) || err) }; });
    }
    function uploadFileObject(file) {
      if (U() && U().uploadFileObject) U().uploadFileObject(uiCtx(), file);
      else showToast("上传模块加载中，请稍后再试");
    }
    function toggleVoiceDictation() {
      if (U() && U().toggleVoiceDictation) U().toggleVoiceDictation(uiCtx(), voiceRecorder);
      else showToast("语音模块加载中，请稍后再试");
    }

    function getRoomMessages(botId) {
      var target = botId || state.activeBotId, out = [];
      for (var i = 0; i < state.messages.length; i++) {
        var m = state.messages[i];
        if (!m || m.bot_id !== target) continue;
        if (m.kind === "bot_message" && m.direction === "outbound") continue;
        if (m.role === "assistant" && m.from_bot_id && m.from_bot_id !== target && m.kind !== "bot_message") continue;
        out.push(m);
      }
      out.sort(function (a, b) {
        var ao = Number(a.order_seq || a.seq || 0), bo = Number(b.order_seq || b.seq || 0);
        if (ao > 0 && bo > 0 && ao !== bo) return ao - bo;
        var normTs = function (m) {
          var v = Number(m.created || (m.created_at ? Number(m.created_at) * 1000 : 0) || 0);
          return (v > 0 && v < 1e11) ? Math.round(v * 1000) : v;
        };
        var ac = normTs(a), bc = normTs(b);
        if (ac > 0 && bc > 0 && ac !== bc) return ac - bc;
        if (ao !== bo) return ao - bo;
        return Number(a.seq || 0) - Number(b.seq || 0);
      });
      return out;
    }
    function parseUserQuoteAndAttachments(rawText) {
      var s = String(rawText || ""), quote = "", files = [];
      if (s.indexOf("> ") === 0 && s.indexOf("\n\n") !== -1) {
        var splitAt = s.indexOf("\n\n");
        quote = s.slice(0, splitAt).split("\n").map(function (l) { return l.replace(/^>\s?/, "").trim(); }).join(" ");
        s = s.slice(splitAt + 2);
      }
      var marker = "\n\n附件（工作区相对路径", mIdx = s.indexOf(marker);
      if (mIdx !== -1) {
        var tail = s.slice(mIdx + marker.length), colonIdx = tail.indexOf("：\n");
        if (colonIdx !== -1) files = tail.slice(colonIdx + 2).split("\n").map(function (p) { return p.trim(); }).filter(Boolean);
        s = s.slice(0, mIdx).trim();
      }
      if (s === "请查看这些附件。" && files.length) s = "";
      return { quote: quote, body: s, files: files };
    }

    function renderNativeSidebarMount() {
      ["dsb-native-sidebar-sec", "dsb-sidebar-dock", "dsb-floating-trigger", "dsb-right-launcher"].forEach(function (id) {
        var old = document.getElementById(id);
        if (old && old.parentNode) old.parentNode.removeChild(old);
      });
    }
    var renderRightLauncher = renderNativeSidebarMount;
    function resolveHostTheme() {
      if (state.themeManual) return state.theme;
      if (typeof document !== "undefined" && document.body) {
        return document.body.hasAttribute("data-ds-dark-theme") ? "dark" : "light";
      }
      return state.theme || "dark";
    }
    function applyLayoutClasses() {
      renderNativeSidebarMount();
      state.theme = resolveHostTheme();
      var pane = document.getElementById("dsb-split-pane");
      var embedded = Boolean(activeSlotHost && activeSlotHost.isConnected);
      if (pane) {
        if (embedded && pane.parentNode !== activeSlotHost) {
          activeSlotHost.appendChild(pane);
        } else if (!embedded && pane.parentNode && pane.parentNode.classList && pane.parentNode.classList.contains("dsb-native-main-slot")) {
          document.body.appendChild(pane);
        }
        pane.setAttribute("data-theme", state.theme);
        pane.classList.toggle("is-embedded", embedded);
        pane.classList.toggle("is-open", Boolean(state.viewActive));
      }
      if (document.body) {
        document.body.classList.remove("dsb-pane-open", "dsb-pane-full");
      }
      var mainEl = document.querySelector("main");
      if (mainEl && mainEl.style.marginRight) mainEl.style.marginRight = "";
    }
    function closeWorkspacePanel() {
      state.viewActive = false;
      state.botSwitcherOpen = false;
      if (hostLayout && typeof hostLayout.selectPanel === "function") {
        try { hostLayout.selectPanel(null); } catch (e) {}
      }
      applyLayoutClasses();
    }
    function autoFitTextarea(ta) {
      if (!ta) return;
      if (!(window.CSS && CSS.supports && CSS.supports("field-sizing", "content"))) {
        ta.style.height = "auto";
        ta.style.height = Math.min(148, Math.max(22, ta.scrollHeight)) + "px";
      }
    }
    function renderHomeRail() {}
    function renderHeaderAndPins() {
      var bot = getActiveBot();
      var ctx = state.botContextById[bot.id] || {};
      var liveSt = state.streamsByBot[bot.id];
      var isBusy = Boolean(liveSt || state.sendingByBot[bot.id]);
      var curTok = Number(ctx.current_tokens || 0);
      var maxTok = Math.max(1, Number(ctx.threshold_tokens || 100000));
      var hitPct = Math.round(Number(ctx.cache_hit_ratio || 0) * 100);
      var tps = Number(ctx.tps || 0);
      var modelLabel = (bot.model || "DeepSeek Bot").split("/").pop();
      var tokSummary = curTok > 0 ? (formatCompactTokens(curTok) + "/" + formatCompactTokens(maxTok)) : "--";
      var subText = isBusy
        ? (liveSt && liveSt.tool ? "执行 · " + friendlyToolLabel(liveSt.tool, {}) : "思考中…")
        : (tokSummary + (hitPct > 0 ? " · 缓存" + hitPct + "%" : "") + (tps > 0 ? " · " + tps.toFixed(0) + "t/s" : "") + (!state.vpsOnline ? " · 离线" : ""));

      var statusEl = document.getElementById("dsb-head-status");
      if (statusEl) {
        statusEl.textContent = subText;
        statusEl.title = modelLabel + " · 上下文 " + curTok + " / " + maxTok + (hitPct > 0 ? " · 缓存命中 " + hitPct + "%" : "");
        statusEl.classList.toggle("is-live", isBusy);
      }
      var fillEl = document.getElementById("dsb-ctx-fill");
      if (fillEl) fillEl.style.width = Math.min(100, Math.round((curTok / maxTok) * 100)) + "%";
      var thBtn = document.getElementById("dsb-btn-theme");
      if (thBtn) {
        thBtn.innerHTML = iconSvg(state.theme === "dark" ? "sun" : "moon", 15);
        thBtn.title = state.theme === "dark" ? "切换浅色模式" : "切换深色模式";
      }
      var cpBtn = document.getElementById("dsb-btn-compact");
      if (cpBtn) cpBtn.innerHTML = state.compactBusy ? '<span class="dsb-mini-spin"></span>' : iconSvg("compress", 15);

      var qEl = document.getElementById("dsb-quick-bots");
      if (qEl && R() && R().buildHeaderBotPillsHtml) {
        qEl.innerHTML = R().buildHeaderBotPillsHtml(state.bots, bot.id, state.streamsByBot, state.sendingByBot);
      }
      var pinBar = document.getElementById("dsb-pin-bar");
      if (pinBar) {
        var pins = Array.isArray(state.vaultPinned) ? state.vaultPinned : [];
        if (!pins.length) {
          pinBar.style.display = "none";
        } else {
          pinBar.style.display = "block";
          var latestPin = String((pins[pins.length - 1] && pins[pins.length - 1].text) || "").replace(/^\[来源:[^\]]+\]\s*/, "");
          var rows = state.pinBarExpanded ? ('<div class="dsb-pin-list">' + pins.map(function (p) {
            var cleanP = String(p.text || "").replace(/^\[来源:[^\]]+\]\s*/, "");
            return '<div class="dsb-pin-row"><span>' + escapeHtml(cleanP) + '</span><button type="button" class="dsb-icon-btn" style="width:20px;height:20px;" data-unpin-id="' + escapeHtml(p.id || p.text) + '" title="移除置顶">' + iconSvg("close", 12) + "</button></div>";
          }).join("") + "</div>") : "";
          pinBar.innerHTML = '<div class="dsb-pin-head" data-toggle-pinbar="1"><span class="dsb-pin-badge">' + iconSvg("pin", 13) + "<span>置顶记忆 #" + pins.length + '</span></span><span class="dsb-pin-preview">' + escapeHtml(latestPin) + "</span>" + iconSvg("chevron", 13) + "</div>" + rows;
        }
      }
    }

    function renderSingleMessageHtml(m) {
      if (!R()) return '<div class="dsb-msg-row"><div class="dsb-bubble">' + escapeHtml(m && m.text) + "</div></div>";
      var parsed = (m && m.role === "user" && m.kind !== "bot_message") ? parseUserQuoteAndAttachments(m.text) : null;
      return R().buildMessageRowHtml(m, parsed, Boolean(m && m.id && state.expandedProcessIds[m.id]));
    }

    function renderChatStream(forceScroll) {
      var historyEl = document.getElementById("dsb-history-box");
      var liveEl = document.getElementById("dsb-live-box");
      var scrollEl = document.getElementById("dsb-chat-scroll");
      if (!historyEl || !liveEl || !scrollEl) return;
      var nearBottom = forceScroll || (scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 120);
      var bot = getActiveBot();
      var roomMsgs = getRoomMessages(bot.id);
      var histSig = bot.id + "|" + state.messagesEpoch + "|" + roomMsgs.map(function (m) {
        var vLen = Array.isArray(m.variants) ? m.variants.length : 0;
        var vIdx = m.active_variant_idx !== undefined ? m.active_variant_idx : -1;
        var cLen = Array.isArray(m.choices) ? m.choices.length : 0;
        return [m.id, m.text.length, m.text.slice(0, 12), (m.thought || "").length, (m.tool_trace || []).length, (m.turn_steps || []).length, (m.timeline || []).length, m.tps || 0, vLen, vIdx, cLen].join(":");
      }).join(",");
      if (histSig !== state.lastStreamSig) {
        state.lastStreamSig = histSig;
        if (!roomMsgs.length) {
          historyEl.innerHTML = '<div class="dsb-empty-state">' + renderBotAvatarSvg(bot, 48) +
            '<div class="dsb-empty-title">' + escapeHtml(bot.name) + '</div><div class="dsb-empty-sub">' +
            escapeHtml(bot.description || "独立会话与专属思维链空间，支持拖拽/粘贴图片、引用回复与跨端协同。") + "</div></div>";
        } else {
          historyEl.innerHTML = roomMsgs.map(function (m) { return renderSingleMessageHtml(m); }).join("");
        }
      }

      var liveSt = state.streamsByBot[bot.id];
      var isSending = Boolean(state.sendingByBot[bot.id]);
      var liveSig = JSON.stringify(liveSt || null) + "|" + isSending;
      if (liveSig !== state.lastLiveSig) {
        state.lastLiveSig = liveSig;
        if (liveSt || isSending) {
          var stText = liveSt ? String(liveSt.text || "").trim() : "";
          var stThought = liveSt ? String(liveSt.thought || "").trim() : "";
          var stStepText = liveSt ? String(liveSt.step_text || "").trim() : "";
          var stTrace = liveSt && Array.isArray(liveSt.trace) ? liveSt.trace : [];
          var stTurnSteps = liveSt && Array.isArray(liveSt.turn_steps) ? liveSt.turn_steps : [];
          var stTool = liveSt ? String(liveSt.tool || "") : "";
          var stTimeline = liveSt && Array.isArray(liveSt.timeline) ? liveSt.timeline : [];
          var liveBodyHtml = (R() && R().renderUnifiedTimelineBodyHtml && (stTimeline.length || stTrace.length || stThought || stText))
            ? R().renderUnifiedTimelineBodyHtml(stTimeline, stThought, stText, stTrace, true)
            : ('<div style="font-size:13px;color:var(--dsb-muted);">' + escapeHtml(stStepText || stTool || "正在思考…") + "</div>");
          liveEl.innerHTML = '<div class="dsb-msg-row is-ai"><div class="dsb-bubble">' + liveBodyHtml + "</div></div>";
        } else {
          liveEl.innerHTML = "";
        }
      }
      if (nearBottom) scrollEl.scrollTop = scrollEl.scrollHeight;
    }

    function renderComposerBanners() {
      var bannerBox = document.getElementById("dsb-composer-banners");
      var slashEl = document.getElementById("dsb-slash-pop");
      if (bannerBox) {
        var parts = [];
        if (state.editingMsgId) {
          parts.push('<div class="dsb-ctx-banner"><span style="font-weight:600;color:var(--dsb-accent);">编辑重发</span><span class="dsb-ctx-banner-txt">' + escapeHtml(state.editingText) + '</span><button type="button" class="dsb-icon-btn" style="width:22px;height:22px;" data-cancel-edit="1">' + iconSvg("close", 13) + "</button></div>");
        } else if (state.replyingToMsg) {
          var who = state.replyingToMsg.role === "user" ? "我" : (getActiveBot().name || "助理");
          parts.push('<div class="dsb-ctx-banner"><span style="font-weight:600;color:var(--dsb-accent);">引用 ' + escapeHtml(who) + '</span><span class="dsb-ctx-banner-txt">' + escapeHtml(String(state.replyingToMsg.text || "").replace(/\s+/g, " ").slice(0, 90)) + '</span><button type="button" class="dsb-icon-btn" style="width:22px;height:22px;" data-cancel-reply="1">' + iconSvg("close", 13) + "</button></div>");
        }
        if (state.pendingAttachments.length) {
          parts.push('<div class="dsb-att-strip">' + state.pendingAttachments.map(function (a, idx) {
            var pct = Math.max(0, Math.min(100, Number(a.progress || 0)));
            if (a.isImage && a.thumbUrl) {
              var overlay = a.uploading
                ? '<div class="dsb-att-img-mask"><span class="dsb-mini-spin"></span><span>' + pct + '%</span></div>'
                : (a.error ? '<button type="button" class="dsb-att-img-mask is-err" data-retry-att="' + idx + '" title="点击重试">重试</button>' : '');
              return '<div class="dsb-att-card dsb-att-img-card' + (a.error ? ' is-err' : '') + '">'
                + '<img src="' + escapeHtml(a.thumbUrl) + '" alt="' + escapeHtml(a.name) + '" data-preview-att="' + idx + '" />'
                + overlay
                + '<button type="button" class="dsb-att-rm" data-rm-att="' + idx + '" title="移除">' + iconSvg("close", 11) + '</button>'
                + (a.uploading ? '<div class="dsb-att-prog"><i style="width:' + pct + '%"></i></div>' : '')
                + '</div>';
            }
            var sub = a.error
              ? '<button type="button" class="dsb-att-retry-txt" data-retry-att="' + idx + '">' + escapeHtml(a.error) + ' · 重试</button>'
              : (a.uploading ? (pct + '%' + (a.sizeLabel ? ' · ' + escapeHtml(a.sizeLabel) : '')) : ((a.sizeLabel ? escapeHtml(a.sizeLabel) + ' · ' : '') + '已就绪'));
            return '<div class="dsb-att-card dsb-att-file-card' + (a.error ? ' is-err' : '') + '">'
              + '<span class="dsb-att-ext is-' + escapeHtml(a.extCls || 'file') + '">' + escapeHtml(a.ext || 'FILE') + '</span>'
              + '<div class="dsb-att-meta"><div class="dsb-att-name" title="' + escapeHtml(a.name) + '">' + escapeHtml(a.name) + '</div><div class="dsb-att-sub">' + sub + '</div></div>'
              + '<button type="button" class="dsb-att-rm" data-rm-att="' + idx + '" title="移除">' + iconSvg("close", 11) + '</button>'
              + (a.uploading ? '<div class="dsb-att-prog"><i style="width:' + pct + '%"></i></div>' : '')
              + '</div>';
          }).join("") + "</div>");
        }
        bannerBox.innerHTML = parts.join("");
      }
      if (slashEl) {
        slashEl.style.display = state.slashMenuOpen ? "block" : "none";
        if (state.slashMenuOpen) {
          slashEl.innerHTML = SLASH_COMMANDS.map(function (sc) {
            return '<button type="button" class="dsb-slash-item" data-pick-slash="' + escapeHtml(sc.cmd) + '"><span class="dsb-slash-cmd">' + escapeHtml(sc.cmd.trim()) + '</span><span style="color:var(--dsb-muted);font-size:11.5px;">' + escapeHtml(sc.desc) + "</span></button>";
          }).join("");
        }
      }
      updateComposerButtons();
    }
    function updateComposerButtons() {
      var bot = getActiveBot();
      var isBusy = Boolean(state.streamsByBot[bot.id] || state.sendingByBot[bot.id]);
      var sendBtn = document.getElementById("dsb-send-btn");
      if (sendBtn) {
        sendBtn.classList.toggle("is-stop", isBusy);
        sendBtn.type = isBusy ? "button" : "submit";
        sendBtn.title = isBusy ? "立即停止生成" : "发送消息 (Enter)";
        sendBtn.innerHTML = isBusy ? iconSvg("stop", 15) : iconSvg("send", 16);
      }
      var micBtn = document.getElementById("dsb-mic-btn");
      if (micBtn) {
        micBtn.classList.toggle("is-rec", state.recordingVoice);
        micBtn.innerHTML = state.transcribingVoice ? '<span class="dsb-mini-spin"></span>' : iconSvg("mic", 16);
      }
    }

    // === [9. Bot Studio & Memory Vault Slide-Over Sheets] ===
    function openBotStudio(mode) { if (U() && U().openBotStudio) U().openBotStudio(uiCtx(), mode); }
    function loadSharedFiles() { if (U() && U().loadSharedFiles) U().loadSharedFiles(uiCtx()); }
    function openVaultSheet(initialPath, initialTab) { if (U() && U().openVaultSheet) U().openVaultSheet(uiCtx(), initialPath, initialTab); }
    function loadVaultFile(relPath) { if (U() && U().loadVaultFile) U().loadVaultFile(uiCtx(), relPath); }
    function renderSheets() {
      var host = document.getElementById("dsb-sheet-host");
      if (!host) return;
      if (!state.studioOpen && !state.vaultOpen) { host.innerHTML = ""; return; }
      if (!R()) return;
      host.innerHTML = state.studioOpen ? R().buildStudioSheetHtml(state, getActiveBot(), PRESET_COLORS, SHAPE_OPTIONS) : R().buildVaultSheetHtml(state);
    }

    // === [10. Turn Submission, Sync Loop & Event Delegation] ===
    function submitTurn() {
      var ta = document.getElementById("dsb-chat-input");
      if (!ta) return;
      var raw = String(ta.value || "").trim();
      var readyAtts = state.pendingAttachments.filter(function (a) { return !a.uploading && a.path; });
      if (!raw && !readyAtts.length) return;
      if (state.uploadingCount > 0) { showToast("附件正在上传，请稍候…"); return; }

      var bot = getActiveBot();
      var finalText = raw || "请查看这些附件。";
      if (state.replyingToMsg && !state.editingMsgId) {
        var qWho = state.replyingToMsg.role === "user" ? "我" : bot.name;
        var qLine = String(state.replyingToMsg.text || "").replace(/\s+/g, " ").trim().slice(0, 120);
        finalText = "> [" + qWho + "] " + qLine + "\n\n" + finalText;
      }
      if (readyAtts.length) {
        finalText += "\n\n附件（工作区相对路径，可直接读取或分析）：\n" + readyAtts.map(function (a) { return a.path; }).join("\n");
      }

      var editId = state.editingMsgId;
      ta.value = ""; autoFitTextarea(ta);
      state.editingMsgId = null; state.editingText = ""; state.replyingToMsg = null;
      if (U() && U().revokeItemThumb) state.pendingAttachments.forEach(U().revokeItemThumb);
      state.pendingAttachments = []; state.slashMenuOpen = false; state.sendingByBot[bot.id] = true;
      renderComposerBanners(); renderRightLauncher();

      if (!editId) {
        var optimisticId = "pc-" + Date.now();
        var roomNow = getRoomMessages(bot.id);
        var maxRoomSeq = roomNow.reduce(function (mx, item) { return Math.max(mx, Number((item && (item.order_seq || item.seq)) || 0)); }, 0);
        var nowMsOpt = Date.now();
        state.messages.push({
          id: optimisticId, client_msg_id: optimisticId, role: "user", kind: "chat", text: finalText, bot_id: bot.id,
          source: "pc", order_seq: maxRoomSeq > 0 ? (maxRoomSeq + 0.01) : 0, created: nowMsOpt,
          time: new Date(nowMsOpt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })
        });
        renderChatStream(true);
        apiCall("/local/infinite-chat/send", "POST", { bot_id: bot.id, text: finalText, client_msg_id: optimisticId }).then(function (res) {
          delete state.sendingByBot[bot.id];
          if (!res || res.ok === false) {
            showToast("发送失败: " + ((res && res.error) || "连接异常"));
            updateComposerButtons();
          }
          applySyncPayload(res);
          renderChatStream(true);
        });
      } else {
        apiCall("/local/infinite-chat/edit", "POST", { bot_id: bot.id, message_id: editId, text: finalText }).then(function (res) {
          delete state.sendingByBot[bot.id];
          if (!res || res.ok === false) {
            showToast("编辑重发失败: " + ((res && res.error) || "连接异常"));
            updateComposerButtons();
          }
          applySyncPayload(res);
          renderChatStream(true);
        });
      }
    }

    function applySyncPayload(res) {
      if (U() && U().applySyncPayload) return U().applySyncPayload(uiCtx(), res);
      if (!res || !res.ok) return;
      if (Array.isArray(res.bots) && res.bots.length) state.bots = res.bots;
      if (Array.isArray(res.messages)) state.messages = res.messages;
      if (res.messages_epoch) state.messagesEpoch = Number(res.messages_epoch) || 1;
      if (res.streams_by_bot && typeof res.streams_by_bot === "object") state.streamsByBot = res.streams_by_bot;
      renderRightLauncher(); renderHomeRail(); renderHeaderAndPins(); updateComposerButtons();
    }

    function syncNow(force) {
      if (!state.viewActive && !force) return;
      apiCall("/local/infinite-chat/sync", "POST", { force: Boolean(force) }).then(function (res) {
        applySyncPayload(res);
        renderChatStream(false);
      });
    }

    function selectBot(botId) {
      if (!botId) return;
      state.activeBotId = botId; state.botSwitcherOpen = false; state.editingMsgId = null; state.replyingToMsg = null;
      apiCall("/local/bots/select", "POST", { bot_id: botId }).then(function (r) {
        if (r && r.ok === false && r.error) showToast("切换助理同步失败: " + r.error);
      });
      renderRightLauncher(); renderHomeRail(); renderHeaderAndPins(); renderComposerBanners(); renderChatStream(true);
    }

    function ensureDomMounted(slotHostEl) {
      injectStyles();
      renderNativeSidebarMount();
      if (slotHostEl) activeSlotHost = slotHostEl;
      var pane = document.getElementById("dsb-split-pane");
      if (!pane) {
        pane = document.createElement("section");
        pane.id = "dsb-split-pane";
        pane.setAttribute("aria-label", "DSB · DeepSeek Bot 跨端工作台");
        var brandBadge = (R() && R().brandMarkSvg)
          ? R().brandMarkSvg(18)
          : '<svg class="dsb-ico" width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M5 10L7.5 4.2L11.2 8.2H12.8L16.5 4.2L19 10V13.8C19 17.5 15.9 20 12 20C8.1 20 5 17.5 5 13.8V10Z" stroke="currentColor" stroke-width="1.65" stroke-linejoin="round"/><path d="M12 10.8L13.4 13.4L12 16L10.6 13.4L12 10.8Z" fill="#0A84FF"/></svg>';
        pane.innerHTML =
          '<div class="dsb-chat-stage">' +
            '<header class="dsb-header">' +
              '<div class="dsb-head-left">' +
                '<div class="dsb-brand-chip" title="DSB · DeepSeek Bot">' + brandBadge + '<span class="dsb-brand-word">DSB</span></div>' +
                '<span class="dsb-head-sep" aria-hidden="true"></span>' +
                '<div class="dsb-nav-pills" id="dsb-quick-bots"></div>' +
                '<button type="button" class="dsb-add-bot-mini" data-open-create-bot="1" title="新建专属助理">' + iconSvg("plus", 14) + '</button>' +
              '</div>' +
              '<div class="dsb-head-right">' +
                '<span class="dsb-head-status" id="dsb-head-status"></span>' +
                '<div class="dsb-head-actions">' +
                  '<button type="button" class="dsb-icon-btn" id="dsb-btn-theme" title="切换日夜主题">' + iconSvg("sun", 15) + '</button>' +
                  '<button type="button" class="dsb-icon-btn" id="dsb-btn-files" title="跨端文件 (本机 / VPS / 云电脑)">' + iconSvg("paperclip", 15) + '</button>' +
                  '<button type="button" class="dsb-icon-btn" id="dsb-btn-vault" title="记忆金库与置顶规则">' + iconSvg("vault", 15) + '</button>' +
                  '<button type="button" class="dsb-icon-btn" id="dsb-btn-studio" title="助理设置">' + iconSvg("sliders", 15) + '</button>' +
                  '<button type="button" class="dsb-icon-btn" id="dsb-btn-compact" title="浓缩归档上下文 (/compact)">' + iconSvg("compress", 15) + '</button>' +
                  '<span class="dsb-act-sep" aria-hidden="true"></span>' +
                  '<button type="button" class="dsb-icon-btn" id="dsb-btn-close" title="隐藏工作台">' + iconSvg("close", 15) + '</button>' +
                '</div>' +
              '</div>' +
              '<div class="dsb-ctx-hairline"><div class="dsb-ctx-hairline-fill" id="dsb-ctx-fill" style="width:0%"></div></div>' +
            '</header>' +
            '<div class="dsb-pin-bar" id="dsb-pin-bar" style="display:none;"></div>' +
            '<div class="dsb-chat-scroll" id="dsb-chat-scroll"><div class="dsb-chat-inner"><div id="dsb-history-box" style="display:flex;flex-direction:column;gap:20px;"></div><div id="dsb-live-box"></div></div></div>' +
            '<div class="dsb-composer-wrap"><div class="dsb-slash-pop" id="dsb-slash-pop" style="display:none;"></div><form id="dsb-composer-form" class="dsb-composer-inner"><div id="dsb-composer-banners" style="display:flex;flex-direction:column;gap:5px;"></div>' +
              '<div class="dsb-composer-row"><input id="dsb-file-picker" type="file" multiple style="display:none;"/><button type="button" class="dsb-tool-circle" id="dsb-attach-btn" title="上传图片或文件附件">' + iconSvg("paperclip", 16) + '</button>' +
              '<div class="dsb-input-capsule"><label for="dsb-chat-input" class="dsb-sr-only">输入消息</label><textarea id="dsb-chat-input" rows="1" placeholder="发消息、粘贴截图，输入 / 唤出指令…"></textarea></div>' +
              '<button type="button" class="dsb-tool-circle" id="dsb-mic-btn" title="语音听写">' + iconSvg("mic", 16) + '</button><button type="submit" class="dsb-send-circle" id="dsb-send-btn" title="发送消息">' + iconSvg("send", 16) + '</button></div></form></div>' +
            '<div id="dsb-toast-el" class="dsb-toast" style="display:none;"></div><div id="dsb-sheet-host"></div>' +
          '</div>';
        (slotHostEl || document.body).appendChild(pane);
        bindPaneDomEvents(pane);
      } else if (slotHostEl && pane.parentNode !== slotHostEl) {
        slotHostEl.appendChild(pane);
      }
      applyLayoutClasses(); renderHeaderAndPins(); renderComposerBanners();
    }

    function bindPaneDomEvents(pane) {
      if (!pane || pane.getAttribute("data-dsb-bound") === "1") return;
      pane.setAttribute("data-dsb-bound", "1");

      pane.addEventListener("toggle", function (ev) {
        var dt = ev.target;
        if (dt && dt.classList && dt.classList.contains("dsb-turn-process")) {
          var mid = dt.getAttribute("data-turn-mid");
          if (mid) {
            if (dt.open) state.expandedProcessIds[mid] = true;
            else delete state.expandedProcessIds[mid];
          }
        }
      }, true);

      // IME-safe Enter Submit (`ime-safe-enter-submit`) + Auto-fit (`form-fields-automatically-fit-contents`) + Clipboard Image Paste
      var formEl = document.getElementById("dsb-composer-form");
      var taEl = document.getElementById("dsb-chat-input");
      taEl.addEventListener("compositionstart", function () { state.isComposing = true; });
      taEl.addEventListener("compositionend", function (ev) { state.isComposing = false; state.lastCompositionEndAt = ev.timeStamp || performance.now(); });
      taEl.addEventListener("keydown", function (ev) {
        if (ev.key === "Escape") {
          if (state.slashMenuOpen) { state.slashMenuOpen = false; renderComposerBanners(); return; }
          if (state.editingMsgId || state.replyingToMsg) { state.editingMsgId = null; state.replyingToMsg = null; renderComposerBanners(); return; }
        }
        if (ev.key !== "Enter" || ev.shiftKey) return;
        var justEndedIme = ((ev.timeStamp || performance.now()) - state.lastCompositionEndAt) < 50;
        if (ev.isComposing || state.isComposing || ev.keyCode === 229 || justEndedIme) return;
        ev.preventDefault();
        var curBot = getActiveBot();
        if (state.streamsByBot[curBot.id] || state.sendingByBot[curBot.id]) return;
        formEl.requestSubmit();
      });
      taEl.addEventListener("input", function () {
        autoFitTextarea(taEl);
        var v = taEl.value;
        var wantSlash = v.indexOf("/") === 0 && v.indexOf(" ") === -1 && v.length <= 14;
        if (wantSlash !== state.slashMenuOpen) { state.slashMenuOpen = wantSlash; renderComposerBanners(); }
      });
      taEl.addEventListener("paste", function (ev) {
        var items = ev.clipboardData && ev.clipboardData.items;
        if (!items) return;
        for (var i = 0; i < items.length; i++) {
          if (items[i].kind === "file") {
            var f = items[i].getAsFile();
            if (f) { ev.preventDefault(); uploadFileObject(f); }
          }
        }
      });
      formEl.addEventListener("submit", function (ev) { ev.preventDefault(); submitTurn(); });

      // Drag & Drop File Upload with Modern Dropzone Overlay, Search, Double-Click Quote
      var dragDepth = 0;
      function ensureDropOverlay(show) {
        pane.classList.toggle("is-drag-over", show);
        var ov = pane.querySelector(".dsb-drop-overlay");
        if (show && !ov) {
          ov = document.createElement("div");
          ov.className = "dsb-drop-overlay";
          ov.innerHTML = '<div class="dsb-drop-box">' + iconSvg("file", 28) + '<div class="dsb-drop-title">松手即可添加图片或文件</div><div class="dsb-drop-sub">支持多选拖入，自动上传并实时预览</div></div>';
          pane.appendChild(ov);
        }
        if (ov) ov.style.display = show ? "flex" : "none";
      }
      pane.addEventListener("dragenter", function (ev) {
        if (ev.dataTransfer && Array.prototype.indexOf.call(ev.dataTransfer.types || [], "Files") !== -1) {
          ev.preventDefault();
          dragDepth++;
          ensureDropOverlay(true);
        }
      });
      pane.addEventListener("dragleave", function () {
        dragDepth = Math.max(0, dragDepth - 1);
        if (dragDepth === 0) ensureDropOverlay(false);
      });
      pane.addEventListener("dragover", function (ev) { ev.preventDefault(); });
      pane.addEventListener("drop", function (ev) {
        ev.preventDefault();
        dragDepth = 0;
        ensureDropOverlay(false);
        var files = ev.dataTransfer && ev.dataTransfer.files;
        if (files && files.length) { for (var i = 0; i < files.length; i++) uploadFileObject(files[i]); }
      });
      var filePicker = document.getElementById("dsb-file-picker");
      filePicker.addEventListener("change", function () {
        if (filePicker.files) { for (var i = 0; i < filePicker.files.length; i++) uploadFileObject(filePicker.files[i]); }
        filePicker.value = "";
      });
      var searchInp = document.getElementById("dsb-bot-search");
      if (searchInp) searchInp.addEventListener("input", function (ev) { state.botSearch = ev.target.value || ""; });
      pane.addEventListener("dblclick", function (ev) {
        var bub = ev.target.closest("[data-dbl-reply]");
        if (!bub) return;
        var mid = bub.getAttribute("data-dbl-reply"), hit = state.messages.find(function (x) { return x.id === mid; });
        if (hit) { state.replyingToMsg = hit; state.editingMsgId = null; renderComposerBanners(); taEl.focus(); }
      });

      pane.addEventListener("click", function (ev) {
        var t = ev.target;
        var togProc = t.closest("[data-toggle-turn-mid]");
        if (togProc) {
          var procCard = togProc.closest(".dsb-turn-process");
          var midProc = togProc.getAttribute("data-toggle-turn-mid");
          if (procCard) {
            var nowOpen = procCard.classList.toggle("is-open");
            if (midProc) {
              if (nowOpen) state.expandedProcessIds[midProc] = true;
              else delete state.expandedProcessIds[midProc];
            }
          }
          return;
        }
        var imgEl = t.closest("[data-lightbox-src]"), copyCodeBtn = t.closest("[data-copy-code]");
        if (imgEl) { openLightbox(imgEl.getAttribute("data-lightbox-src")); return; }
        var dlFileBtn = t.closest("[data-dl-file]");
        if (dlFileBtn) {
          ev.stopPropagation();
          var card = dlFileBtn.closest("[data-open-file]");
          if (U() && U().triggerFileDownload) U().triggerFileDownload(dlFileBtn.getAttribute("data-dl-file"), card ? card.getAttribute("data-file-name") : "");
          return;
        }
        var openFileCard = t.closest("[data-open-file]");
        if (openFileCard) {
          if (U() && U().openFilePreview) {
            U().openFilePreview(escapeHtml, openFileCard.getAttribute("data-open-file"), openFileCard.getAttribute("data-file-name"), openFileCard.getAttribute("data-file-ext"), openFileCard.getAttribute("data-download-url"));
          }
          return;
        }
        if (copyCodeBtn) { navigator.clipboard.writeText(decodeURIComponent(copyCodeBtn.getAttribute("data-copy-code") || "")); showToast("代码已复制"); return; }
        if (t.closest("#dsb-btn-theme")) { state.themeManual = true; state.theme = state.theme === "dark" ? "light" : "dark"; try { window.localStorage.setItem("dsb_theme", state.theme); } catch (e) {} applyLayoutClasses(); renderHeaderAndPins(); return; }
        if (t.closest("#dsb-btn-files")) { openVaultSheet("", "files"); return; }
        if (t.closest("#dsb-btn-vault")) { openVaultSheet("", "notes"); return; }
        if (t.closest("#dsb-btn-studio")) { openBotStudio("edit"); return; }
        if (t.closest("#dsb-btn-compact")) {
          if (state.compactBusy) return; state.compactBusy = true; renderHeaderAndPins();
          apiCall("/local/infinite-chat/compact", "POST", { bot_id: state.activeBotId }).then(function (r) {
            state.compactBusy = false;
            if (!r || r.ok === false) { renderHeaderAndPins(); showToast("上下文浓缩失败: " + ((r && r.error) || "网络异常")); return; }
            applySyncPayload(r); renderChatStream(true); showToast("上下文浓缩归档完成");
          });
          return;
        }
        if (t.closest("#dsb-btn-close")) { closeWorkspacePanel(); return; }
        if (t.closest("#dsb-attach-btn")) { filePicker.click(); return; }
        if (t.closest("#dsb-mic-btn")) { toggleVoiceDictation(); return; }
        if (t.closest("#dsb-send-btn") && t.closest("#dsb-send-btn").classList.contains("is-stop")) {
          ev.preventDefault(); var bId = state.activeBotId; delete state.sendingByBot[bId]; delete state.streamsByBot[bId];
          apiCall("/local/infinite-chat/stop", "POST", { bot_id: bId }).then(function (r) { applySyncPayload(r); renderChatStream(false); });
          updateComposerButtons(); renderChatStream(false); return;
        }
        var selBot = t.closest("[data-select-bot]"); if (selBot) { selectBot(selBot.getAttribute("data-select-bot")); return; }
        var fltBtn = t.closest("[data-bot-filter]"); if (fltBtn) { state.botFilter = fltBtn.getAttribute("data-bot-filter"); renderHomeRail(); return; }
        if (t.closest("[data-open-create-bot]")) { openBotStudio("create"); return; }
        if (t.closest("[data-toggle-pinbar]")) { state.pinBarExpanded = !state.pinBarExpanded; renderHeaderAndPins(); return; }
        var unpinBtn = t.closest("[data-unpin-id]");
        if (unpinBtn) {
          ev.stopPropagation();
          apiCall("/local/vault", "POST", { op: "unpin", pin_id: unpinBtn.getAttribute("data-unpin-id") }).then(function (r) {
            if (!r || r.ok === false) { showToast("移除置顶记忆失败: " + ((r && r.error) || "网络异常")); return; }
            if (Array.isArray(r.pins)) state.vaultPinned = r.pins; renderHeaderAndPins(); if (state.vaultOpen) renderSheets(); showToast("已移除置顶记忆");
          });
          return;
        }
        var slashPick = t.closest("[data-pick-slash]");
        if (slashPick) {
          var cmd = slashPick.getAttribute("data-pick-slash") || ""; state.slashMenuOpen = false;
          if (cmd.slice(-1) === " ") { taEl.value = cmd; autoFitTextarea(taEl); renderComposerBanners(); taEl.focus(); } else { taEl.value = cmd; submitTurn(); }
          return;
        }
        if (t.closest("[data-cancel-edit]")) { state.editingMsgId = null; state.editingText = ""; taEl.value = ""; autoFitTextarea(taEl); renderComposerBanners(); return; }
        if (t.closest("[data-cancel-reply]")) { state.replyingToMsg = null; renderComposerBanners(); return; }
        var rmAtt = t.closest("[data-rm-att]");
        if (rmAtt) {
          var rmIdx = Number(rmAtt.getAttribute("data-rm-att"));
          if (U() && U().removePendingAttachment) U().removePendingAttachment(uiCtx(), rmIdx);
          else { state.pendingAttachments.splice(rmIdx, 1); renderComposerBanners(); }
          return;
        }
        var retryAtt = t.closest("[data-retry-att]");
        if (retryAtt) {
          if (U() && U().retryAttachment) U().retryAttachment(uiCtx(), Number(retryAtt.getAttribute("data-retry-att")));
          return;
        }
        var prevAtt = t.closest("[data-preview-att]");
        if (prevAtt) {
          var pItem = state.pendingAttachments[Number(prevAtt.getAttribute("data-preview-att"))];
          if (pItem && pItem.thumbUrl) openLightbox(pItem.thumbUrl);
          return;
        }
        var choiceBtn = t.closest("[data-send-choice]"); if (choiceBtn) { taEl.value = choiceBtn.getAttribute("data-send-choice") || ""; submitTurn(); return; }

        var swVarBtn = t.closest("[data-switch-variant]");
        if (swVarBtn) {
          var vMid = swVarBtn.getAttribute("data-mid"), vIdx = Number(swVarBtn.getAttribute("data-vidx"));
          var targetM = state.messages.find(function (x) { return x && x.id === vMid; });
          if (targetM && Array.isArray(targetM.variants) && targetM.variants[vIdx]) {
            var selV = targetM.variants[vIdx];
            targetM.active_variant_idx = vIdx;
            targetM.text = String(selV.text || "");
            targetM.thought = String(selV.thought || "");
            targetM.tool_trace = Array.isArray(selV.trace) ? selV.trace : [];
            targetM.turn_steps = Array.isArray(selV.turn_steps) ? selV.turn_steps : [];
            targetM.timeline = Array.isArray(selV.timeline) ? selV.timeline : [];
            targetM.sticker = String(selV.sticker || "");
            targetM.attachments = Array.isArray(selV.attachments) ? selV.attachments : [];
            targetM.choices = Array.isArray(selV.choices) ? selV.choices : [];
            targetM.tps = Number(selV.tps || 0);
            targetM.ttft_ms = Number(selV.ttft_ms || 0);
            targetM.cache_hit_pct = Number(selV.cache_hit_pct || 0);
            state.lastStreamSig = "";
            renderChatStream(false);
          }
          apiCall("/local/infinite-chat/variant", "POST", { bot_id: state.activeBotId, message_id: vMid, variant_idx: vIdx }).then(function (r) {
            if (!r || r.ok === false) { showToast("切换版本失败: " + ((r && r.error) || "网络异常")); return; }
            applySyncPayload(r);
            renderChatStream(false);
          });
          return;
        }

        var actBtn = t.closest("[data-act]");
        if (actBtn) {
          var act = actBtn.getAttribute("data-act"), mid = actBtn.getAttribute("data-mid"), msgObj = state.messages.find(function (x) { return x.id === mid; });
          if (!msgObj) return;
          if (act === "copy") { navigator.clipboard.writeText(msgObj.text || ""); showToast("已复制到剪贴板"); }
          else if (act === "reply") { state.replyingToMsg = msgObj; state.editingMsgId = null; renderComposerBanners(); taEl.focus(); }
          else if (act === "edit") { var cleanEdit = parseUserQuoteAndAttachments(msgObj.text).body || msgObj.text; state.editingMsgId = msgObj.id; state.editingText = cleanEdit; state.replyingToMsg = null; taEl.value = cleanEdit; autoFitTextarea(taEl); renderComposerBanners(); taEl.focus(); }
          else if (act === "reroll") { state.sendingByBot[state.activeBotId] = true; updateComposerButtons(); apiCall("/local/infinite-chat/reroll", "POST", { bot_id: state.activeBotId, message_id: msgObj.id }).then(function (r) { delete state.sendingByBot[state.activeBotId]; if (!r || r.ok === false) showToast("重发失败: " + ((r && r.error) || "网络异常")); applySyncPayload(r); renderChatStream(true); }); }
          else if (act === "pin") { apiCall("/local/vault", "POST", { op: "pin", text: String(msgObj.text || "").slice(0, 360) }).then(function (r) { if (!r || r.ok === false) { showToast("置顶失败: " + ((r && r.error) || "网络异常")); return; } if (Array.isArray(r.pins)) state.vaultPinned = r.pins; renderHeaderAndPins(); showToast("已钉入跨端置顶记忆"); }); }
          return;
        }

        // Sheet actions
        var closeS = t.closest("[data-close-sheet]");
        if (closeS && (closeS.getAttribute("data-close-sheet") !== "mask" || t === closeS)) { state.studioOpen = false; state.vaultOpen = false; renderSheets(); renderHeaderAndPins(); return; }

        var pickCol = t.closest("[data-pick-color]"); if (pickCol) { syncStudioDraftFromInputs(); state.editBotColor = pickCol.getAttribute("data-pick-color"); renderSheets(); return; }
        var pickSh = t.closest("[data-pick-shape]"); if (pickSh) { syncStudioDraftFromInputs(); state.editBotShape = pickSh.getAttribute("data-pick-shape"); renderSheets(); return; }
        if (t.closest("[data-export-bot]")) { if (R() && R().exportBotArchive) R().exportBotArchive(apiCall, state.activeBotId, showToast); return; }
        if (t.closest("[data-import-bot]")) {
          if (R() && R().pickAndImportBotArchive) R().pickAndImportBotArchive(apiCall, function (r) {
            if (r && Array.isArray(r.bots)) state.bots = r.bots; if (r && r.active_bot_id) state.activeBotId = r.active_bot_id; if (r && Array.isArray(r.messages)) state.messages = r.messages;
            state.studioOpen = false; renderSheets(); renderRightLauncher(); renderHomeRail(); renderHeaderAndPins(); renderChatStream(true);
          }, showToast);
          return;
        }
        if (t.closest("[data-save-bot]")) {
          syncStudioDraftFromInputs(); if (!state.editBotName.trim()) { showToast("请输入助理名称"); return; }
          var isCr = state.studioMode === "create";
          apiCall(isCr ? "/local/bots/create" : "/local/bots/update", "POST", {
            bot_id: isCr ? "" : state.activeBotId, name: state.editBotName.trim(), description: state.editBotDesc.trim(),
            color: state.editBotColor, shape: state.editBotShape, instructions: state.editBotPersona, persona: state.editBotPersona, model: state.editBotModel, curiosity_enabled: state.editBotCuriosity
          }).then(function (r) {
            if (!r || r.ok === false) { showToast("保存失败: " + ((r && r.error) || "网络异常")); return; }
            if (Array.isArray(r.bots)) state.bots = r.bots; if (isCr && r.bot && r.bot.id) state.activeBotId = r.bot.id;
            state.studioOpen = false; renderSheets(); renderRightLauncher(); renderHomeRail(); renderHeaderAndPins(); renderChatStream(true);
            showToast(r.cloud_warning ? "本地已保存（云端同步异常: " + r.cloud_warning + "）" : (isCr ? "已创建新助理" : "助理设定已保存"));
          });
          return;
        }
        if (t.closest("[data-delete-bot]")) {
          apiCall("/local/bots/delete", "POST", { bot_id: state.activeBotId }).then(function (r) {
            if (!r || r.ok === false) { showToast("删除失败: " + ((r && r.error) || "操作失败")); return; }
            if (Array.isArray(r.bots)) state.bots = r.bots; state.activeBotId = r.active_bot_id || "pc_engineer";
            state.studioOpen = false; renderSheets(); renderRightLauncher(); renderHomeRail(); renderHeaderAndPins(); renderChatStream(true); showToast("助理已删除");
          });
          return;
        }
        var vTab = t.closest("[data-vault-tab]"); if (vTab) { state.vaultTab = vTab.getAttribute("data-vault-tab"); if (state.vaultTab === "files") loadSharedFiles(); else renderSheets(); return; }
        if (t.closest("[data-refresh-files]")) { loadSharedFiles(); return; }
        var syncBtn = t.closest("[data-sync-file]");
        if (syncBtn) {
          var dir = syncBtn.getAttribute("data-sync-file"), sp = syncBtn.getAttribute("data-sync-path"); state.vaultStatusMsg = "正在同步 " + sp + "…"; renderSheets();
          apiCall("/local/files/sync", "POST", { direction: dir, path: sp }).then(function (r) {
            state.vaultStatusMsg = r && r.ok ? "同步完成: " + (r.workspace_path || r.name || sp) : "同步失败: " + ((r && r.error) || "unknown");
            showToast(r && r.ok ? "跨端文件同步成功" : state.vaultStatusMsg); loadSharedFiles();
          });
          return;
        }
        var vPath = t.closest("[data-vault-path]"); if (vPath) { loadVaultFile(vPath.getAttribute("data-vault-path")); return; }
        if (t.closest("[data-save-vault]")) {
          var ed = document.getElementById("dsb-vault-editor"); if (!ed || !state.vaultSelectedPath) return; state.vaultSelectedContent = ed.value;
          apiCall("/local/vault", "POST", { op: "write", path: state.vaultSelectedPath, content: ed.value }).then(function (r) {
            state.vaultStatusMsg = r && r.ok ? "已保存 " + new Date().toLocaleTimeString("zh-CN", { hour12: false }) : "保存失败: " + ((r && r.error) || "网络异常");
            renderSheets(); showToast(r && r.ok ? "记忆文档已保存" : state.vaultStatusMsg);
          });
          return;
        }
        if (t.closest("[data-add-pin]")) {
          var pinInp = document.getElementById("dsb-new-pin-inp"), pTxt = pinInp ? pinInp.value.trim() : ""; if (!pTxt) return;
          apiCall("/local/vault", "POST", { op: "pin", text: pTxt }).then(function (r) {
            if (!r || r.ok === false) { showToast("添加置顶失败: " + ((r && r.error) || "网络异常")); return; }
            if (Array.isArray(r.pins)) state.vaultPinned = r.pins; renderHeaderAndPins(); renderSheets(); showToast("已添加置顶记忆");
          });
        }
      });
      applyLayoutClasses(); renderHeaderAndPins(); renderComposerBanners();
    }
    function syncStudioDraftFromInputs() { if (U() && U().syncStudioDraftFromInputs) U().syncStudioDraftFromInputs(state); }
    function openLightbox(src) { if (U() && U().openLightbox) U().openLightbox(escapeHtml, src); }
    var assetsLoading = false;
    function loadUiAssets() {
      if (assetsLoading) return; assetsLoading = true;
      apiCall("/local/ui-assets", "GET", null).then(function (r) {
        assetsLoading = false;
        if (r && r.css) injectStyles(r.css);
        if (r && r.render_js && !window.__DSB_RENDER__) { var s = document.createElement("script"); s.textContent = r.render_js; document.head.appendChild(s); }
        if (r && r.client_ui_js && !window.__DSB_CLIENT_UI__) { var s2 = document.createElement("script"); s2.textContent = r.client_ui_js; document.head.appendChild(s2); }
        if (r && r.data_url) state.whaleAvatarUrl = r.data_url;
        var oldPane = document.getElementById("dsb-split-pane");
        if (oldPane && window.__DSB_RENDER__ && !oldPane.querySelector("svg")) oldPane.remove();
        state.lastStreamSig = ""; state.lastLiveSig = "";
        if (state.viewActive || activeSlotHost) ensureDomMounted(activeSlotHost);
        applyLayoutClasses(); syncNow(true);
      });
    }
    function registerDshNativeSlots(ctx) {
      if (!ctx || !ctx.slots || !React) return false;
      ctx.slots.inject("sidebar.panellist", function () {
        return ctx.slots.register(
          { name: "sidebar.panellist", id: "dsb-companion", order: 60, label: "DeepSeek Bot" },
          function DsbSidebarNavIcon(props) {
            var sz = (props && props.size) || 16;
            return React.createElement(
              "svg",
              { width: sz, height: sz, viewBox: "0 0 24 24", fill: "none", "aria-hidden": "true" },
              React.createElement("path", {
                d: "M4.5 9.8L7.2 4L11.1 7.9H12.9L16.8 4L19.5 9.8V13.8C19.5 17.7 16.2 20.5 12 20.5C7.8 20.5 4.5 17.7 4.5 13.8V9.8Z",
                stroke: "currentColor", strokeWidth: 1.7, strokeLinejoin: "round"
              }),
              React.createElement("path", {
                d: "M12 10.8L13.5 13.4L12 16L10.5 13.4L12 10.8Z",
                fill: "currentColor"
              })
            );
          }
        );
      });
      ctx.slots.inject("main", function () {
        return ctx.slots.register(
          { name: "main", key: "dsb-companion" },
          function DsbMainPanelHost() {
            var hostRef = React.useRef(null);
            React.useEffect(function () {
              var hostEl = hostRef.current;
              if (!hostEl) return;
              activeSlotHost = hostEl;
              state.viewActive = true;
              ensureDomMounted(hostEl);
              applyLayoutClasses();
              loadUiAssets();
              syncNow(true);
              renderChatStream(true);
              return function () {
                if (activeSlotHost === hostEl) activeSlotHost = null;
                state.viewActive = false;
                var pane = document.getElementById("dsb-split-pane");
                if (pane) pane.classList.remove("is-open");
              };
            }, []);
            return React.createElement("div", {
              ref: hostRef,
              className: "dsb-native-main-slot",
              style: { width: "100%", height: "100%", flex: 1, minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }
            });
          }
        );
      });
      return true;
    }
    var pluginExport = {
      name: "dsh-deepseek-claw",
      inject: ["slots", "layout"],
      apply: function (ctx) {
        if (window.__dsbTimer) clearInterval(window.__dsbTimer);
        hostLayout = (ctx && ctx.layout) || null;
        renderNativeSidebarMount();
        registerDshNativeSlots(ctx);
        var themeObs = null;
        if (typeof MutationObserver !== "undefined" && document.body) {
          themeObs = new MutationObserver(function () {
            if (!state.themeManual) {
              var nextT = resolveHostTheme();
              if (nextT !== state.theme) { applyLayoutClasses(); renderHeaderAndPins(); }
            }
          });
          themeObs.observe(document.body, { attributes: true, attributeFilter: ["data-ds-dark-theme"] });
        }
        var boot = function () { renderNativeSidebarMount(); loadUiAssets(); };
        if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else setTimeout(boot, 30);
        var tick = 0, timer = setInterval(function () {
          tick++;
          renderNativeSidebarMount();
          if ((!window.__DSB_RENDER__ || !window.__DSB_CLIENT_UI__) && tick % 3 === 0) loadUiAssets();
          if (state.viewActive || Object.keys(state.sendingByBot).length > 0 || Object.keys(state.streamsByBot).length > 0) syncNow(false);
        }, 1200);
        window.__dsbTimer = timer;
        var dispose = function () {
          clearInterval(timer);
          if (window.__dsbTimer === timer) window.__dsbTimer = null;
          if (themeObs) { try { themeObs.disconnect(); } catch (e) {} themeObs = null; }
        };
        if (ctx && typeof ctx.effect === "function") ctx.effect(function () { return dispose; });
        else if (ctx && typeof ctx.on === "function") ctx.on("dispose", dispose);
      }
    };
    return { default: pluginExport, name: "dsh-deepseek-claw", inject: pluginExport.inject, apply: pluginExport.apply };
  }
});
