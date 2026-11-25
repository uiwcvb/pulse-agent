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
      pinBarExpanded: false, messages: [], messagesEpoch: 1, syncGen: 0, streamsByBot: {}, sendingByBot: {}, botContextById: {},
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
      lastCompositionEndAt: 0, recordingVoice: false, transcribingVoice: false, voiceStatusHint: "",
      stickerOpen: false, stickerPacks: [], stickerLoading: false, activeStickerPackId: "",
      slashActiveIdx: 0, syncInFlight: false, paneSkipped: false, unreadWhileScrolledUp: false
    };
    var voiceRecorder = { stream: null, audioCtx: null, source: null, processor: null, chunks: [] }, toastTimer = null, syncKickFn = null;

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
      if (el) {
        if (state.toastMsg) { el.textContent = state.toastMsg; el.classList.add("is-visible"); }
        else el.classList.remove("is-visible");
      }
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(function () { state.toastMsg = ""; var e2 = document.getElementById("dsb-toast-el"); if (e2) e2.classList.remove("is-visible"); }, 2400);
    }
    function toggleTheme() {
      if (!state.themeManual) { state.themeManual = true; state.theme = "dark"; try { window.localStorage.setItem("dsb_theme", "dark"); } catch (e) {} }
      else if (state.theme === "dark") { state.themeManual = true; state.theme = "light"; try { window.localStorage.setItem("dsb_theme", "light"); } catch (e) {} }
      else { state.themeManual = false; try { window.localStorage.removeItem("dsb_theme"); } catch (e) {} }
      applyLayoutClasses(); renderHeaderAndPins();
      showToast(!state.themeManual ? "外观：跟随系统" : (state.theme === "dark" ? "外观：深色" : "外观：浅色"));
    }
    function runCompact() {
      if (state.compactBusy) return; state.compactBusy = true; renderHeaderAndPins();
      apiCall("/local/infinite-chat/compact", "POST", { bot_id: state.activeBotId }).then(function (r) {
        state.compactBusy = false;
        if (!r || r.ok === false) { renderHeaderAndPins(); showToast("上下文浓缩失败: " + ((r && r.error) || "网络异常")); return; }
        applySyncPayload(r); renderChatStream(true); showToast("上下文浓缩归档完成");
      });
    }
    function U() { return window.__DSB_CLIENT_UI__ || null; }
    function uiCtx() {
      return {
        state: state, apiCall: apiCall, showToast: showToast, autoFitTextarea: autoFitTextarea,
        renderComposerBanners: renderComposerBanners, updateComposerButtons: updateComposerButtons,
        renderSheets: renderSheets, renderHeaderAndPins: renderHeaderAndPins,
        renderRightLauncher: renderRightLauncher, getActiveBot: getActiveBot,
        escapeHtml: escapeHtml, getRoomMessages: getRoomMessages, friendlyToolLabel: friendlyToolLabel,
        renderBotAvatarSvg: renderBotAvatarSvg, renderSingleMessageHtml: renderSingleMessageHtml,
        renderChatStream: renderChatStream, submitTurn: submitTurn, selectBot: selectBot,
        openBotStudio: openBotStudio, openVaultSheet: openVaultSheet, toggleTheme: toggleTheme,
        runCompact: runCompact, R: R, PRESET_COLORS: PRESET_COLORS, SHAPE_OPTIONS: SHAPE_OPTIONS,
        SLASH_COMMANDS: SLASH_COMMANDS,
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
      if (typeof document !== "undefined" && document.body && document.body.hasAttribute("data-ds-dark-theme")) {
        return "dark";
      }
      if (typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
        return "dark";
      }
      return "light";
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
        if (state.themeManual) pane.setAttribute("data-theme-manual", "1");
        else pane.removeAttribute("data-theme-manual");
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
        ? (liveSt && liveSt.tool ? "执行 · " + friendlyToolLabel(liveSt.tool, liveSt.tool_args || liveSt.args || liveSt.arguments || {}) : "思考中…")
        : (tokSummary + (hitPct > 0 ? " · 缓存" + hitPct + "%" : "") + (tps > 0 ? " · " + tps.toFixed(0) + "t/s" : "") + (!state.vpsOnline ? " · 离线" : ""));

      var statusEl = document.getElementById("dsb-head-status");
      if (statusEl) {
        statusEl.textContent = !state.vpsOnline ? "离线 · 点击重连" : subText;
        statusEl.title = !state.vpsOnline ? "网关与云端断开，点击立即重连" : (modelLabel + " · 上下文 " + curTok + " / " + maxTok + (hitPct > 0 ? " · 缓存命中 " + hitPct + "%" : ""));
        statusEl.classList.toggle("is-live", isBusy);
        statusEl.classList.toggle("is-offline", !state.vpsOnline);
      }
      var fillEl = document.getElementById("dsb-ctx-fill");
      if (fillEl) fillEl.style.width = Math.min(100, Math.round((curTok / maxTok) * 100)) + "%";
      var morePop = document.getElementById("dsb-more-pop");
      if (morePop && R() && R().buildHeaderMoreMenuHtml) {
        morePop.innerHTML = R().buildHeaderMoreMenuHtml(state);
      }

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
            return '<div class="dsb-pin-row"><span>' + escapeHtml(cleanP) + '</span><button type="button" class="dsb-icon-btn" style="width:28px;height:28px;" aria-label="移除置顶记忆" data-unpin-id="' + escapeHtml(p.id || p.text) + '" title="移除置顶">' + iconSvg("close", 12) + "</button></div>";
          }).join("") + "</div>") : "";
          pinBar.innerHTML = '<div class="dsb-pin-head" data-toggle-pinbar="1"><span class="dsb-pin-badge">' + iconSvg("pin", 13) + "<span>置顶记忆 #" + pins.length + '</span></span><span class="dsb-pin-preview">' + escapeHtml(latestPin) + "</span>" + iconSvg("chevron", 13) + "</div>" + rows;
        }
      }
    }

    function renderSingleMessageHtml(m) {
      if (!R()) return '<div class="dsb-msg-row"><div class="dsb-bubble">' + escapeHtml(m && m.text) + "</div></div>";
      var parsed = (m && m.role === "user" && m.kind !== "bot_message") ? parseUserQuoteAndAttachments(m.text) : null;
      return R().buildMessageRowHtml(m, parsed, state.expandedProcessIds);
    }

    function renderChatStream(forceScroll) {
      var historyEl = document.getElementById("dsb-history-box"), liveEl = document.getElementById("dsb-live-box"), scrollEl = document.getElementById("dsb-chat-scroll");
      if (!historyEl || !liveEl || !scrollEl) return;
      var nearBottom = forceScroll || (scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 120);
      var bot = getActiveBot(), roomMsgs = getRoomMessages(bot.id), histAddedNew = false;
      if (U() && U().reconcileChatHistory) {
        histAddedNew = U().reconcileChatHistory(uiCtx(), historyEl, roomMsgs, bot).addedNew;
      } else {
        var histSig = bot.id + "|" + state.messagesEpoch + "|" + roomMsgs.map(function (m) { return [m.id, m.text.length, m.text.slice(0, 12), (m.timeline || []).length].join(":"); }).join(",");
        if (histSig !== state.lastStreamSig) {
          state.lastStreamSig = histSig; histAddedNew = roomMsgs.length > 0;
          historyEl.innerHTML = roomMsgs.length ? roomMsgs.map(renderSingleMessageHtml).join("") : ('<div class="dsb-empty-state">' + renderBotAvatarSvg(bot, 48) + '<div class="dsb-empty-title">' + escapeHtml(bot.name) + '</div><div class="dsb-empty-sub">' + escapeHtml(bot.description || "") + "</div></div>");
        }
      }
      var liveSt = state.streamsByBot[bot.id], isSending = Boolean(state.sendingByBot[bot.id]), liveChanged = false;
      var liveSig = JSON.stringify(liveSt || null) + "|" + isSending;
      if (liveSig !== state.lastLiveSig) {
        state.lastLiveSig = liveSig; liveChanged = Boolean(liveSt || isSending);
        if (liveSt || isSending) {
          var stText = liveSt ? String(liveSt.text || "").trim() : "", stThought = liveSt ? String(liveSt.thought || "").trim() : "", stStepText = liveSt ? String(liveSt.step_text || "").trim() : "";
          var stTrace = liveSt && Array.isArray(liveSt.trace) ? liveSt.trace : [], stTool = liveSt ? String(liveSt.tool || "") : "", stTimeline = liveSt && Array.isArray(liveSt.timeline) ? liveSt.timeline : [];
          var liveBodyHtml = (R() && R().renderUnifiedTimelineBodyHtml && (stTimeline.length || stTrace.length || stThought || stText))
            ? R().renderUnifiedTimelineBodyHtml(stTimeline, stThought, stText, stTrace, true, "live", state.expandedProcessIds)
            : ('<div style="font-size:13px;color:var(--dsb-muted);">' + escapeHtml(stStepText || stTool || "正在思考…") + "</div>");
          liveEl.innerHTML = '<div class="dsb-msg-row is-ai"><div class="dsb-bubble">' + liveBodyHtml + "</div></div>";
        } else liveEl.innerHTML = "";
      }
      if (nearBottom) { scrollEl.scrollTop = scrollEl.scrollHeight; state.unreadWhileScrolledUp = false; }
      if (U() && U().updateScrollBottomPill) U().updateScrollBottomPill(uiCtx(), !nearBottom && (histAddedNew || liveChanged));
    }

    function renderComposerBanners() {
      var bannerBox = document.getElementById("dsb-composer-banners");
      var slashEl = document.getElementById("dsb-slash-pop");
      if (bannerBox) {
        var parts = [];
        if (state.editingMsgId) {
          parts.push('<div class="dsb-ctx-banner"><span style="font-weight:600;color:var(--dsb-accent);">编辑重发</span><span class="dsb-ctx-banner-txt">' + escapeHtml(state.editingText) + '</span><button type="button" class="dsb-icon-btn" style="width:28px;height:28px;" aria-label="取消编辑" data-cancel-edit="1">' + iconSvg("close", 13) + "</button></div>");
        } else if (state.replyingToMsg) {
          var who = state.replyingToMsg.role === "user" ? "我" : (getActiveBot().name || "助理");
          parts.push('<div class="dsb-ctx-banner"><span style="font-weight:600;color:var(--dsb-accent);">引用 ' + escapeHtml(who) + '</span><span class="dsb-ctx-banner-txt">' + escapeHtml(String(state.replyingToMsg.text || "").replace(/\s+/g, " ").slice(0, 90)) + '</span><button type="button" class="dsb-icon-btn" style="width:28px;height:28px;" aria-label="取消引用" data-cancel-reply="1">' + iconSvg("close", 13) + "</button></div>");
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
      var slashEl = document.getElementById("dsb-slash-pop"), taEl = document.getElementById("dsb-chat-input");
      if (U() && U().renderSlashMenu) {
        U().renderSlashMenu(uiCtx(), taEl ? taEl.value : "", SLASH_COMMANDS);
      } else if (slashEl) {
        slashEl.style.display = state.slashMenuOpen ? "block" : "none";
      }
      updateComposerButtons();
    }
    function updateComposerButtons() {
      var bot = getActiveBot(), isBusy = Boolean(state.streamsByBot[bot.id] || state.sendingByBot[bot.id]);
      var ta = document.getElementById("dsb-chat-input");
      var hasDraft = Boolean((ta && ta.value.trim()) || state.pendingAttachments.length);
      var sendBtn = document.getElementById("dsb-send-btn");
      if (sendBtn) {
        sendBtn.classList.toggle("is-stop", isBusy);
        sendBtn.classList.toggle("is-idle", !isBusy && !hasDraft);
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
      if (U() && U().renderSheets) { U().renderSheets(uiCtx()); return; }
    }

    // === [10. Turn Submission, Sync Loop & Event Delegation] ===
    function submitTurn(opts) {
      var ta = document.getElementById("dsb-chat-input"), hasOv = Boolean(opts && opts.overrideText);
      if (!ta && !hasOv) return;
      var raw = hasOv ? String(opts.overrideText).trim() : String(ta.value || "").trim();
      var readyAtts = hasOv ? [] : state.pendingAttachments.filter(function (a) { return !a.uploading && a.path; });
      if (!raw && !readyAtts.length) return;
      if (!hasOv && state.uploadingCount > 0) { showToast("附件正在上传，请稍候…"); return; }

      var bot = getActiveBot(), finalText = raw || "请查看这些附件。";
      if (!hasOv && state.replyingToMsg && !state.editingMsgId) {
        var qWho = state.replyingToMsg.role === "user" ? "我" : bot.name;
        var qLine = String(state.replyingToMsg.text || "").replace(/\s+/g, " ").trim().slice(0, 120);
        finalText = "> [" + qWho + "] " + qLine + "\n\n" + finalText;
      }
      if (!hasOv && readyAtts.length) {
        finalText += "\n\n附件（工作区相对路径，可直接读取或分析）：\n" + readyAtts.map(function (a) { return a.path; }).join("\n");
      }

      var editId = hasOv ? null : state.editingMsgId;
      if (!hasOv && ta) {
        ta.value = ""; autoFitTextarea(ta);
        state.editingMsgId = null; state.editingText = ""; state.replyingToMsg = null;
        if (U() && U().revokeItemThumb) state.pendingAttachments.forEach(U().revokeItemThumb);
        state.pendingAttachments = []; state.slashMenuOpen = false;
      }
      state.sendingByBot[bot.id] = true;
      renderComposerBanners(); renderRightLauncher();
      if (typeof syncKickFn === "function") syncKickFn(240);

      state.syncGen = (state.syncGen || 0) + 1;
      if (!editId) {
        var optimisticId = "pc-" + Date.now();
        var roomNow = getRoomMessages(bot.id);
        var maxRoomSeq = roomNow.reduce(function (mx, item) { return Math.max(mx, Number((item && (item.order_seq || item.seq)) || 0)); }, 0);
        var nowMsOpt = Date.now();
        var stickerVal = (opts && (opts.sticker || opts.stickerUrl)) ? String(opts.sticker || opts.stickerUrl) : "";
        state.messages.push({
          id: optimisticId, client_msg_id: optimisticId, role: "user", kind: "chat", text: finalText,
          sticker: (opts && opts.stickerUrl) ? String(opts.stickerUrl) : stickerVal, bot_id: bot.id,
          source: "pc", order_seq: maxRoomSeq > 0 ? (maxRoomSeq + 0.01) : 0, created: nowMsOpt,
          time: new Date(nowMsOpt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })
        });
        renderChatStream(true);
        apiCall("/local/infinite-chat/send", "POST", { bot_id: bot.id, text: finalText, ...(stickerVal ? { sticker: stickerVal } : {}), client_msg_id: optimisticId }).then(function (res) {
          delete state.sendingByBot[bot.id];
          if (!res || res.ok === false) { showToast("发送失败: " + ((res && res.error) || "连接异常")); updateComposerButtons(); }
          state.syncGen = (state.syncGen || 0) + 1;
          applySyncPayload(res); renderChatStream(true);
        });
      } else {
        apiCall("/local/infinite-chat/edit", "POST", { bot_id: bot.id, message_id: editId, text: finalText }).then(function (res) {
          delete state.sendingByBot[bot.id];
          if (!res || res.ok === false) { showToast("编辑重发失败: " + ((res && res.error) || "连接异常")); updateComposerButtons(); }
          state.syncGen = (state.syncGen || 0) + 1;
          applySyncPayload(res); renderChatStream(true);
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
      renderRightLauncher(); renderHeaderAndPins(); updateComposerButtons();
    }

    function syncNow(force) {
      if (state.syncInFlight) return;
      if (!state.viewActive && !force && !Object.keys(state.sendingByBot).length && !Object.keys(state.streamsByBot).length) return;
      state.syncInFlight = true;
      var reqGen = (state.syncGen = (state.syncGen || 0) + 1);
      apiCall("/local/infinite-chat/sync", "POST", { force: Boolean(force) }).then(function (res) {
        if (reqGen !== state.syncGen) return;
        applySyncPayload(res);
        renderChatStream(false);
      }).finally(function () {
        state.syncInFlight = false;
        if (typeof syncKickFn === "function") syncKickFn();
      });
    }

    function selectBot(botId) {
      if (!botId) return;
      state.syncGen = (state.syncGen || 0) + 1;
      state.activeBotId = botId; state.botSwitcherOpen = false; state.editingMsgId = null; state.replyingToMsg = null;
      state.unreadWhileScrolledUp = false; state.lastStreamSig = ""; state.lastLiveSig = "";
      apiCall("/local/bots/select", "POST", { bot_id: botId }).then(function (r) {
        if (r && r.ok === false && r.error) showToast("切换助理同步失败: " + r.error);
      });
      renderRightLauncher(); renderHeaderAndPins(); renderComposerBanners(); renderChatStream(true);
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
              '</div>' +
              '<div class="dsb-head-right">' +
                '<button type="button" class="dsb-head-status" id="dsb-head-status"></button>' +
                '<div class="dsb-head-actions">' +
                  '<button type="button" class="dsb-icon-btn" id="dsb-btn-vault" title="记忆金库与跨端文件" aria-label="记忆金库与跨端文件">' + iconSvg("vault", 15) + '</button>' +
                  '<button type="button" class="dsb-icon-btn" id="dsb-btn-studio" title="助理设置" aria-label="助理设置">' + iconSvg("sliders", 15) + '</button>' +
                  '<button type="button" class="dsb-icon-btn" id="dsb-btn-more" popovertarget="dsb-more-pop" aria-haspopup="true" aria-controls="dsb-more-pop" title="更多操作与主题 (Ctrl+K)" aria-label="更多操作">' + iconSvg("more", 15) + '</button>' +
                  '<span class="dsb-act-sep" aria-hidden="true"></span>' +
                  '<button type="button" class="dsb-icon-btn" id="dsb-btn-close" title="隐藏工作台" aria-label="隐藏工作台">' + iconSvg("close", 15) + '</button>' +
                '</div>' +
              '</div>' +
              '<div id="dsb-more-pop" class="dsb-more-pop" popover="auto" aria-label="更多操作"></div>' +
              '<div class="dsb-ctx-hairline"><div class="dsb-ctx-hairline-fill" id="dsb-ctx-fill" style="width:0%"></div></div>' +
            '</header>' +
            '<div class="dsb-pin-bar" id="dsb-pin-bar" style="display:none;"></div>' +
            '<div class="dsb-chat-scroll" id="dsb-chat-scroll"><div class="dsb-chat-inner"><div id="dsb-history-box" style="display:flex;flex-direction:column;gap:20px;"></div><div id="dsb-live-box"></div></div></div>' +
            '<button type="button" class="dsb-scroll-bottom" id="dsb-scroll-bottom-btn" aria-label="回到底部">' + iconSvg("arrowDown", 13) + '<span data-sb-label="1">回到底部</span></button>' +
            '<div class="dsb-composer-wrap"><div class="dsb-slash-pop" id="dsb-slash-pop" role="listbox" style="display:none;"></div><div id="dsb-sticker-pop" class="dsb-sticker-pop" popover="auto" aria-label="表情贴纸选择"></div>' +
              '<form id="dsb-composer-form" class="dsb-composer-inner"><div id="dsb-composer-banners" style="display:flex;flex-direction:column;gap:5px;"></div>' +
              '<div class="dsb-composer-row"><input id="dsb-file-picker" type="file" multiple style="display:none;"/><button type="button" class="dsb-tool-circle" id="dsb-attach-btn" title="上传图片或文件附件" aria-label="上传附件">' + iconSvg("paperclip", 16) + '</button>' +
              '<button type="button" class="dsb-tool-circle" id="dsb-sticker-btn" popovertarget="dsb-sticker-pop" aria-haspopup="true" aria-expanded="false" aria-controls="dsb-sticker-pop" title="发送表情贴纸" aria-label="发送表情贴纸">' + iconSvg("smile", 16) + '</button>' +
              '<div class="dsb-input-capsule"><label for="dsb-chat-input" class="dsb-sr-only">输入消息</label><textarea id="dsb-chat-input" rows="1" placeholder="发消息、粘贴截图，/ 指令，Ctrl+K 快捷面板…"></textarea></div>' +
              '<button type="button" class="dsb-tool-circle" id="dsb-mic-btn" title="语音听写" aria-label="语音听写">' + iconSvg("mic", 16) + '</button><button type="submit" class="dsb-send-circle" id="dsb-send-btn" title="发送消息" aria-label="发送消息">' + iconSvg("send", 16) + '</button></div></form></div>' +
            '<span id="dsb-sr-status" class="dsb-sr-only" role="status" aria-live="polite"></span>' +
            '<div id="dsb-toast-el" class="dsb-toast"></div>' +
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
      if (U() && U().ensureStickerToggleBound) U().ensureStickerToggleBound(uiCtx());

      pane.addEventListener("toggle", function (ev) {
        var dt = ev.target;
        if (!dt || !dt.classList) return;
        var pKey = dt.getAttribute("data-proc-key");
        if (pKey) { if (dt.open) state.expandedProcessIds[pKey] = true; else delete state.expandedProcessIds[pKey]; }
        if (dt.id === "dsb-more-pop" && ev.newState === "open") {
          var mb = document.getElementById("dsb-btn-more");
          if (R() && R().buildHeaderMoreMenuHtml) dt.innerHTML = R().buildHeaderMoreMenuHtml(state);
          if (mb && mb.getBoundingClientRect) {
            var r = mb.getBoundingClientRect();
            dt.style.top = Math.round(r.bottom + 6) + "px";
            dt.style.right = Math.max(10, Math.round(window.innerWidth - r.right)) + "px";
            dt.style.left = "auto";
          }
        }
      }, true);
      pane.addEventListener("contentvisibilityautostatechange", function (ev) {
        state.paneSkipped = Boolean(ev && ev.skipped);
        if (!state.paneSkipped && typeof syncKickFn === "function") { syncNow(false); syncKickFn(); }
      });

      var formEl = document.getElementById("dsb-composer-form"), taEl = document.getElementById("dsb-chat-input"), scrollEl = document.getElementById("dsb-chat-scroll");
      if (scrollEl) {
        scrollEl.addEventListener("scroll", function () { if (U() && U().updateScrollBottomPill) U().updateScrollBottomPill(uiCtx(), false); }, { passive: true });
      }
      taEl.addEventListener("compositionstart", function () { state.isComposing = true; });
      taEl.addEventListener("compositionend", function (ev) { state.isComposing = false; state.lastCompositionEndAt = ev.timeStamp || performance.now(); });
      taEl.addEventListener("keydown", function (ev) {
        var justEndedIme = ((ev.timeStamp || performance.now()) - state.lastCompositionEndAt) < 50;
        var inIme = ev.isComposing || state.isComposing || ev.keyCode === 229 || justEndedIme;
        if (ev.key === "Escape") {
          if (state.stickerOpen && U() && U().closeStickerPicker) { U().closeStickerPicker(uiCtx()); return; }
          if (state.slashMenuOpen) { state.slashMenuOpen = false; renderComposerBanners(); return; }
          if (state.editingMsgId || state.replyingToMsg) { state.editingMsgId = null; state.editingText = ""; state.replyingToMsg = null; renderComposerBanners(); return; }
          if (state.studioOpen || state.vaultOpen) { state.studioOpen = false; state.vaultOpen = false; renderSheets(); return; }
          return;
        }
        if (state.slashMenuOpen && !inIme) {
          var filteredCmds = (U() && U().getFilteredSlashCommands) ? U().getFilteredSlashCommands(taEl.value, SLASH_COMMANDS) : SLASH_COMMANDS;
          if (filteredCmds.length && (ev.key === "ArrowDown" || ev.key === "ArrowUp")) {
            ev.preventDefault();
            var delta = ev.key === "ArrowDown" ? 1 : -1;
            state.slashActiveIdx = (state.slashActiveIdx + delta + filteredCmds.length) % filteredCmds.length;
            if (U() && U().renderSlashMenu) U().renderSlashMenu(uiCtx(), taEl.value, SLASH_COMMANDS);
            return;
          }
          if (filteredCmds.length && (ev.key === "Tab" || (ev.key === "Enter" && !ev.shiftKey))) {
            ev.preventDefault();
            var picked = filteredCmds[Math.max(0, Math.min(filteredCmds.length - 1, state.slashActiveIdx))];
            if (picked) {
              state.slashMenuOpen = false;
              taEl.value = picked.cmd; autoFitTextarea(taEl); renderComposerBanners();
              if (picked.cmd.slice(-1) !== " ") submitTurn(); else taEl.focus();
            }
            return;
          }
        }
        if (ev.key === "ArrowUp" && !ev.shiftKey && !ev.ctrlKey && !ev.altKey && !ev.metaKey && !inIme && !taEl.value.trim()) {
          var rm = getRoomMessages(state.activeBotId), lastUser = null;
          for (var ui = rm.length - 1; ui >= 0; ui--) {
            if (rm[ui] && rm[ui].role === "user" && rm[ui].kind !== "bot_message") { lastUser = rm[ui]; break; }
          }
          if (lastUser) {
            ev.preventDefault();
            var ce = parseUserQuoteAndAttachments(lastUser.text).body || lastUser.text;
            state.editingMsgId = lastUser.id; state.editingText = ce; state.replyingToMsg = null;
            taEl.value = ce; autoFitTextarea(taEl); renderComposerBanners();
          }
          return;
        }
        if (ev.key !== "Enter" || ev.shiftKey || inIme) return;
        ev.preventDefault();
        var curBot = getActiveBot();
        if (state.streamsByBot[curBot.id] || state.sendingByBot[curBot.id]) return;
        formEl.requestSubmit();
      });
      taEl.addEventListener("input", function () {
        autoFitTextarea(taEl);
        var v = taEl.value, wantSlash = v.indexOf("/") === 0 && v.indexOf(" ") === -1 && v.length <= 14;
        if (wantSlash) state.slashActiveIdx = 0;
        if (wantSlash || wantSlash !== state.slashMenuOpen) { state.slashMenuOpen = wantSlash; renderComposerBanners(); }
        else updateComposerButtons();
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
        var mPopOpen = document.getElementById("dsb-more-pop");
        if (mPopOpen && mPopOpen.classList.contains("is-open") && !t.closest("#dsb-more-pop") && !t.closest("#dsb-btn-more")) mPopOpen.classList.remove("is-open");
        if (state.stickerOpen && !t.closest("#dsb-sticker-pop") && !t.closest("#dsb-sticker-btn") && U() && U().closeStickerPicker) U().closeStickerPicker(uiCtx());
        if (t.closest("#dsb-sticker-btn")) {
          if (U() && U().ensureStickerToggleBound) U().ensureStickerToggleBound(uiCtx());
          if (!Object.prototype.hasOwnProperty.call(HTMLElement.prototype, "popover")) {
            if (U() && U().toggleStickerPicker) U().toggleStickerPicker(uiCtx()); else showToast("表情模块加载中");
          } else if (!U()) showToast("表情模块加载中");
          return;
        }
        var stkPackBtn = t.closest("[data-sticker-pack]");
        if (stkPackBtn) { state.activeStickerPackId = stkPackBtn.getAttribute("data-sticker-pack") || ""; if (U() && U().renderStickerPopover) U().renderStickerPopover(uiCtx()); return; }
        var sendStkBtn = t.closest("[data-send-sticker]");
        if (sendStkBtn) { if (U() && U().sendStickerCode) U().sendStickerCode(uiCtx(), sendStkBtn.getAttribute("data-send-sticker"), sendStkBtn.getAttribute("data-sticker-url")); return; }
        if (t.closest("#dsb-scroll-bottom-btn")) {
          var scEl = document.getElementById("dsb-chat-scroll");
          if (scEl) {
            var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
            try { scEl.scrollTo({ top: scEl.scrollHeight, behavior: reduce ? "auto" : "smooth" }); } catch (e) { scEl.scrollTop = scEl.scrollHeight; }
            state.unreadWhileScrolledUp = false; if (U() && U().updateScrollBottomPill) U().updateScrollBottomPill(uiCtx(), false);
          }
          return;
        }
        if (t.closest("#dsb-head-status")) {
          if (!state.vpsOnline) apiCall("/local/reconnect", "POST", {}).then(function () { showToast("正在重连云端中枢…"); syncNow(true); });
          else openBotStudio("edit");
          return;
        }
        var starterBtn = t.closest("[data-send-starter]");
        if (starterBtn) {
          var sp = starterBtn.getAttribute("data-send-starter") || "";
          if (sp) { if (/[：:]$/.test(sp.trim())) { taEl.value = sp; autoFitTextarea(taEl); taEl.focus(); renderComposerBanners(); } else { taEl.value = sp; submitTurn(); } }
          return;
        }
        if (t.closest("#dsb-btn-more") && !Object.prototype.hasOwnProperty.call(HTMLElement.prototype, "popover")) {
          var mp = document.getElementById("dsb-more-pop"), mb2 = document.getElementById("dsb-btn-more");
          if (mp && mb2) {
            if (R() && R().buildHeaderMoreMenuHtml) mp.innerHTML = R().buildHeaderMoreMenuHtml(state);
            var r2 = mb2.getBoundingClientRect();
            mp.style.top = Math.round(r2.bottom + 6) + "px"; mp.style.right = Math.max(10, Math.round(window.innerWidth - r2.right)) + "px"; mp.style.left = "auto";
            mp.classList.toggle("is-open");
          }
          return;
        }
        var moreActBtn = t.closest("[data-more-act]");
        if (moreActBtn) {
          var mAct = moreActBtn.getAttribute("data-more-act"), mPop = document.getElementById("dsb-more-pop");
          if (mPop) { if (typeof mPop.hidePopover === "function") try { mPop.hidePopover(); } catch (e) {} mPop.classList.remove("is-open"); }
          if (mAct === "theme") toggleTheme();
          else if (mAct === "files") openVaultSheet("", "files");
          else if (mAct === "compact") runCompact();
          else if (mAct === "create") openBotStudio("create");
          else if (mAct === "cmdk" && U() && U().openCommandPalette) U().openCommandPalette(uiCtx());
          return;
        }
        var imgEl = t.closest("[data-lightbox-src]"), copyCodeBtn = t.closest("[data-copy-code]");
        if (imgEl) { openLightbox(imgEl.getAttribute("data-lightbox-src")); return; }
        var dlFileBtn = t.closest("[data-dl-file]");
        if (dlFileBtn) { ev.stopPropagation(); var card = dlFileBtn.closest("[data-open-file]"); if (U() && U().triggerFileDownload) U().triggerFileDownload(dlFileBtn.getAttribute("data-dl-file"), card ? card.getAttribute("data-file-name") : ""); return; }
        var openFileCard = t.closest("[data-open-file]");
        if (openFileCard) { if (U() && U().openFilePreview) U().openFilePreview(escapeHtml, openFileCard.getAttribute("data-open-file"), openFileCard.getAttribute("data-file-name"), openFileCard.getAttribute("data-file-ext"), openFileCard.getAttribute("data-download-url")); return; }
        if (copyCodeBtn) {
          var codeTxt = decodeURIComponent(copyCodeBtn.getAttribute("data-copy-code") || "");
          if (U() && U().copyTextWithFeedback) U().copyTextWithFeedback(uiCtx(), copyCodeBtn, codeTxt, "代码已复制");
          else { navigator.clipboard.writeText(codeTxt); showToast("代码已复制"); }
          return;
        }
        if (t.closest("#dsb-btn-vault")) { openVaultSheet("", "notes"); return; }
        if (t.closest("#dsb-btn-studio")) { openBotStudio("edit"); return; }
        if (t.closest("#dsb-btn-close")) { closeWorkspacePanel(); return; }
        if (t.closest("#dsb-attach-btn")) { filePicker.click(); return; }
        if (t.closest("#dsb-mic-btn")) { toggleVoiceDictation(); return; }
        if (t.closest("#dsb-send-btn") && t.closest("#dsb-send-btn").classList.contains("is-stop")) {
          ev.preventDefault(); var bId = state.activeBotId; delete state.sendingByBot[bId]; delete state.streamsByBot[bId];
          apiCall("/local/infinite-chat/stop", "POST", { bot_id: bId }).then(function (r) { applySyncPayload(r); renderChatStream(false); });
          updateComposerButtons(); renderChatStream(false); return;
        }
        var selBot = t.closest("[data-select-bot]"); if (selBot) { selectBot(selBot.getAttribute("data-select-bot")); return; }
        if (t.closest("[data-bot-filter]")) return;
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
        if (rmAtt) { var rmIdx = Number(rmAtt.getAttribute("data-rm-att")); if (U() && U().removePendingAttachment) U().removePendingAttachment(uiCtx(), rmIdx); else { state.pendingAttachments.splice(rmIdx, 1); renderComposerBanners(); } return; }
        var retryAtt = t.closest("[data-retry-att]"); if (retryAtt) { if (U() && U().retryAttachment) U().retryAttachment(uiCtx(), Number(retryAtt.getAttribute("data-retry-att"))); return; }
        var prevAtt = t.closest("[data-preview-att]"); if (prevAtt) { var pItem = state.pendingAttachments[Number(prevAtt.getAttribute("data-preview-att"))]; if (pItem && pItem.thumbUrl) openLightbox(pItem.thumbUrl); return; }
        var choiceBtn = t.closest("[data-send-choice]"); if (choiceBtn) { taEl.value = choiceBtn.getAttribute("data-send-choice") || ""; submitTurn(); return; }
        var swVarBtn = t.closest("[data-switch-variant]"); if (swVarBtn && U() && U().switchMessageVariantLocal) { U().switchMessageVariantLocal(uiCtx(), swVarBtn); return; }
        var actBtn = t.closest("[data-act]");
        if (actBtn) {
          var act = actBtn.getAttribute("data-act"), mid = actBtn.getAttribute("data-mid"), msgObj = state.messages.find(function (x) { return x.id === mid; });
          if (!msgObj) return;
          if (act === "copy") { if (U() && U().copyTextWithFeedback) U().copyTextWithFeedback(uiCtx(), actBtn, msgObj.text || "", "已复制到剪贴板"); else { navigator.clipboard.writeText(msgObj.text || ""); showToast("已复制到剪贴板"); } }
          else if (act === "reply") { state.replyingToMsg = msgObj; state.editingMsgId = null; renderComposerBanners(); taEl.focus(); }
          else if (act === "edit") { var cleanEdit = parseUserQuoteAndAttachments(msgObj.text).body || msgObj.text; state.editingMsgId = msgObj.id; state.editingText = cleanEdit; state.replyingToMsg = null; taEl.value = cleanEdit; autoFitTextarea(taEl); renderComposerBanners(); taEl.focus(); }
          else if (act === "reroll") { state.sendingByBot[state.activeBotId] = true; updateComposerButtons(); apiCall("/local/infinite-chat/reroll", "POST", { bot_id: state.activeBotId, message_id: msgObj.id }).then(function (r) { delete state.sendingByBot[state.activeBotId]; if (!r || r.ok === false) showToast("重发失败: " + ((r && r.error) || "网络异常")); applySyncPayload(r); renderChatStream(true); }); }
          else if (act === "pin") { apiCall("/local/vault", "POST", { op: "pin", text: String(msgObj.text || "").slice(0, 360) }).then(function (r) { if (!r || r.ok === false) { showToast("置顶失败: " + ((r && r.error) || "网络异常")); return; } if (Array.isArray(r.pins)) state.vaultPinned = r.pins; renderHeaderAndPins(); showToast("已钉入跨端置顶记忆"); }); }
          return;
        }
        if (U() && U().handleSheetClickAction && U().handleSheetClickAction(uiCtx(), t)) return;
      });
      applyLayoutClasses(); renderHeaderAndPins(); renderComposerBanners();
    }
    function syncStudioDraftFromInputs() { if (U() && U().syncStudioDraftFromInputs) U().syncStudioDraftFromInputs(state); }
    function openLightbox(src) { if (U() && U().openLightbox) U().openLightbox(escapeHtml, src); }
    var assetsLoading = false, assetsRetryTimer = null;
    function isStylesLoaded() {
      var el = document.getElementById("dsb-companion-styles");
      return Boolean(el && el.getAttribute("data-loaded") === "1");
    }
    function loadUiAssets() {
      if (assetsLoading) return; assetsLoading = true;
      apiCall("/local/ui-assets", "GET", null).then(function (r) {
        assetsLoading = false;
        if (r && r.ok !== false && r.css) {
          if (assetsRetryTimer) { clearTimeout(assetsRetryTimer); assetsRetryTimer = null; }
          injectStyles(r.css);
        } else if (state.viewActive && !assetsRetryTimer) {
          assetsRetryTimer = setTimeout(function () {
            assetsRetryTimer = null;
            if (state.viewActive && (!isStylesLoaded() || !window.__DSB_RENDER__ || !window.__DSB_CLIENT_UI__)) loadUiAssets();
          }, 1800);
        }
        if (r && r.render_js && !window.__DSB_RENDER__) { var s = document.createElement("script"); s.textContent = r.render_js; document.head.appendChild(s); }
        if (r && r.client_ui_js && !window.__DSB_CLIENT_UI__) { var s2 = document.createElement("script"); s2.textContent = r.client_ui_js; document.head.appendChild(s2); }
        if (U() && U().ensureStickerToggleBound) U().ensureStickerToggleBound(uiCtx());
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
              if (typeof syncKickFn === "function") syncKickFn(50);
              return function () {
                if (activeSlotHost === hostEl) activeSlotHost = null;
                state.viewActive = false;
                var pane = document.getElementById("dsb-split-pane");
                if (pane) pane.classList.remove("is-open");
                if (typeof syncKickFn === "function") syncKickFn();
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
        var themeObs = null, themeMql = null;
        if (typeof MutationObserver !== "undefined" && document.body) {
          themeObs = new MutationObserver(function () {
            if (!state.themeManual) {
              var nextT = resolveHostTheme();
              if (nextT !== state.theme) { applyLayoutClasses(); renderHeaderAndPins(); }
            }
          });
          themeObs.observe(document.body, { attributes: true, attributeFilter: ["data-ds-dark-theme"] });
        }
        var onMqlChange = function () {
          if (!state.themeManual) { applyLayoutClasses(); renderHeaderAndPins(); }
        };
        if (typeof window !== "undefined" && window.matchMedia) {
          themeMql = window.matchMedia("(prefers-color-scheme: dark)");
          if (themeMql && typeof themeMql.addEventListener === "function") themeMql.addEventListener("change", onMqlChange);
        }
        var boot = function () { renderNativeSidebarMount(); loadUiAssets(); };
        if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else setTimeout(boot, 30);
        var syncTimer = null;
        var scheduleNextSync = function (overrideMs) {
          if (syncTimer) { clearTimeout(syncTimer); syncTimer = null; }
          var hidden = (typeof document !== "undefined" && document.hidden) || state.paneSkipped;
          var hasActive = Object.keys(state.sendingByBot).length > 0 || Object.keys(state.streamsByBot).length > 0;
          if (hidden || (!state.viewActive && !hasActive)) return;
          var delay = typeof overrideMs === "number" ? overrideMs : (hasActive ? 240 : 2400);
          syncTimer = setTimeout(function () {
            syncTimer = null;
            renderNativeSidebarMount();
            if ((!isStylesLoaded() || !window.__DSB_RENDER__ || !window.__DSB_CLIENT_UI__) && !assetsLoading) loadUiAssets();
            syncNow(false);
          }, delay);
        };
        syncKickFn = scheduleNextSync;
        var onVisChange = function () {
          if (typeof document !== "undefined" && !document.hidden) { syncNow(false); scheduleNextSync(60); }
          else if (syncTimer) { clearTimeout(syncTimer); syncTimer = null; }
        };
        var onDocKeyDown = function (ev) {
          if ((ev.ctrlKey || ev.metaKey) && String(ev.key || "").toLowerCase() === "k" && state.viewActive) {
            ev.preventDefault();
            if (U() && U().openCommandPalette) U().openCommandPalette(uiCtx());
            return;
          }
          if (ev.key === "Escape" && state.viewActive && (state.studioOpen || state.vaultOpen)) {
            state.studioOpen = false; state.vaultOpen = false; renderSheets(); renderHeaderAndPins();
          }
        };
        if (typeof document !== "undefined") {
          document.addEventListener("visibilitychange", onVisChange);
          document.addEventListener("keydown", onDocKeyDown);
        }
        window.__dsbTimer = syncTimer;
        var dispose = function () {
          if (syncTimer) { clearTimeout(syncTimer); syncTimer = null; }
          if (assetsRetryTimer) { clearTimeout(assetsRetryTimer); assetsRetryTimer = null; }
          syncKickFn = null;
          voiceRecorder.cancelled = true;
          if (U() && U().stopVoiceRecorder) U().stopVoiceRecorder(uiCtx(), voiceRecorder);
          if (typeof document !== "undefined") {
            document.removeEventListener("visibilitychange", onVisChange);
            document.removeEventListener("keydown", onDocKeyDown);
          }
          if (themeObs) { try { themeObs.disconnect(); } catch (e) {} themeObs = null; }
          if (themeMql && typeof themeMql.removeEventListener === "function") {
            try { themeMql.removeEventListener("change", onMqlChange); } catch (e) {}
          }
        };
        if (ctx && typeof ctx.effect === "function") ctx.effect(function () { return dispose; });
        else if (ctx && typeof ctx.on === "function") ctx.on("dispose", dispose);
      }
    };
    return { default: pluginExport, name: "dsh-deepseek-claw", inject: pluginExport.inject, apply: pluginExport.apply };
  }
});
