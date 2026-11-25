(function () {
  var MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;
  function resolveEscFn(fn) {
    if (typeof fn === "function") return fn;
    return function (s) {
      return String(s === null || s === undefined ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    };
  }
  function formatFileSize(bytes) {
    if (!bytes || bytes <= 0) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }
  function extInfo(name) {
    var m = String(name || "").match(/\.([a-z0-9]{1,5})$/i);
    var ext = m ? m[1].toUpperCase() : "FILE";
    var cls = ext === "PDF" ? "pdf" : (/^(ZIP|RAR|7Z|GZ|TAR)$/.test(ext) ? "zip" : (/^(PY|KT|JS|TS|MJS|JAVA|JSON|YML|YAML|MD|SH|CSS|HTML)$/.test(ext) ? "code" : (/^(XLS|XLSX|CSV)$/.test(ext) ? "sheet" : (/^(DOC|DOCX|TXT|RTF)$/.test(ext) ? "doc" : "file"))));
    return { ext: ext.slice(0, 4), cls: cls };
  }
  function revokeItemThumb(item) {
    if (item && item.thumbUrl) { try { URL.revokeObjectURL(item.thumbUrl); } catch (e) {} item.thumbUrl = ""; }
  }

  function runUploadItem(ctx, item) {
    var state = ctx.state;
    var file = item._file;
    if (!file) return;
    item.error = "";
    item.uploading = true;
    item.aborted = false;
    item.progress = 8;
    state.uploadingCount++;
    ctx.renderComposerBanners();

    var reader = new FileReader();
    var tick = null;
    item._abort = function () {
      item.aborted = true;
      if (tick) { clearInterval(tick); tick = null; }
      try { if (reader.readyState === 1) reader.abort(); } catch (e) {}
    };
    reader.onprogress = function (ev) {
      if (item.aborted) return;
      if (ev && ev.lengthComputable && ev.total > 0) {
        item.progress = Math.max(10, Math.min(45, Math.round((ev.loaded / ev.total) * 38) + 8));
        ctx.renderComposerBanners();
      }
    };
    reader.onload = function () {
      if (item.aborted) return;
      item.progress = 55;
      ctx.renderComposerBanners();
      var rs = String(reader.result || "");
      var b64 = rs.indexOf(",") !== -1 ? rs.split(",")[1] : rs;
      tick = setInterval(function () {
        if (item.aborted || !item.uploading || item.progress >= 92) { clearInterval(tick); tick = null; return; }
        item.progress = Math.min(92, item.progress + 7);
        ctx.renderComposerBanners();
      }, 140);
      ctx.apiCall("/local/upload", "POST", { name: item.name, data_b64: b64 }).then(function (res) {
        if (tick) { clearInterval(tick); tick = null; }
        if (item.aborted) return;
        state.uploadingCount = Math.max(0, state.uploadingCount - 1);
        if (res && res.ok && res.path) {
          item.path = res.path;
          item.uploading = false;
          item.progress = 100;
          item.error = "";
          ctx.showToast("已附加 " + item.name);
        } else {
          item.uploading = false;
          item.progress = 0;
          item.error = (res && res.error) || "上传失败";
          ctx.showToast("上传失败: " + item.error);
        }
        ctx.renderComposerBanners();
      });
    };
    reader.onerror = function () {
      if (tick) { clearInterval(tick); tick = null; }
      if (item.aborted) return;
      state.uploadingCount = Math.max(0, state.uploadingCount - 1);
      item.uploading = false;
      item.progress = 0;
      item.error = "读取失败";
      ctx.showToast("读取本地文件失败");
      ctx.renderComposerBanners();
    };
    reader.readAsDataURL(file);
  }

  function uploadFileObject(ctx, file) {
    if (!file) return;
    if (file.size > MAX_ATTACHMENT_BYTES) {
      ctx.showToast("单个附件不能超过 50MB");
      return;
    }
    var state = ctx.state;
    var fname = file.name || ("paste-" + Date.now() + ".png");
    var isImg = /^image\//i.test(file.type || "") || /\.(png|jpe?g|webp|gif|bmp)$/i.test(fname);
    var thumbUrl = "";
    if (isImg && typeof URL !== "undefined" && URL.createObjectURL) {
      try { thumbUrl = URL.createObjectURL(file); } catch (e) {}
    }
    var ei = extInfo(fname);
    var item = {
      name: fname,
      path: "",
      isImage: isImg,
      thumbUrl: thumbUrl,
      sizeBytes: file.size || 0,
      sizeLabel: formatFileSize(file.size || 0),
      ext: ei.ext,
      extCls: ei.cls,
      progress: 0,
      error: "",
      uploading: true,
      _file: file,
    };
    state.pendingAttachments.push(item);
    runUploadItem(ctx, item);
  }

  function retryAttachment(ctx, idx) {
    var item = ctx.state.pendingAttachments[idx];
    if (!item || item.uploading || !item._file) return;
    runUploadItem(ctx, item);
  }

  function removePendingAttachment(ctx, idx) {
    var list = ctx.state.pendingAttachments;
    if (idx < 0 || idx >= list.length) return;
    var item = list[idx];
    if (typeof item._abort === "function") item._abort();
    if (item.uploading) ctx.state.uploadingCount = Math.max(0, ctx.state.uploadingCount - 1);
    revokeItemThumb(item);
    list.splice(idx, 1);
    ctx.renderComposerBanners();
  }

  function encodeMonoWav(samples, sr) {
    var buf = new ArrayBuffer(44 + samples.length * 2), view = new DataView(buf);
    var wStr = function (o, s) { for (var i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
    wStr(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); wStr(8, "WAVE"); wStr(12, "fmt ");
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, sr, true); view.setUint32(28, sr * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    wStr(36, "data"); view.setUint32(40, samples.length * 2, true);
    for (var i = 0; i < samples.length; i++) { var s = Math.max(-1, Math.min(1, samples[i])); view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true); }
    var bytes = new Uint8Array(buf), parts = [], CHUNK = 8192;
    for (var j = 0; j < bytes.length; j += CHUNK) parts.push(String.fromCharCode.apply(null, bytes.subarray(j, Math.min(bytes.length, j + CHUNK))));
    return window.btoa(parts.join(""));
  }

  function stopVoiceRecorder(ctx, voiceRecorder) {
    if (!voiceRecorder) return;
    try {
      if (voiceRecorder.processor) {
        voiceRecorder.processor.onaudioprocess = null;
        voiceRecorder.processor.disconnect();
      }
      if (voiceRecorder.source) voiceRecorder.source.disconnect();
    } catch (e) {}
    try {
      if (voiceRecorder.stream) voiceRecorder.stream.getTracks().forEach(function (t) { t.stop(); });
      if (voiceRecorder.audioCtx && typeof voiceRecorder.audioCtx.close === "function") voiceRecorder.audioCtx.close();
    } catch (e) {}
    voiceRecorder.stream = null;
    voiceRecorder.audioCtx = null;
    voiceRecorder.source = null;
    voiceRecorder.processor = null;
    voiceRecorder.chunks = [];
    if (ctx && ctx.state) ctx.state.recordingVoice = false;
  }

  function toggleVoiceDictation(ctx, voiceRecorder) {
    var state = ctx.state;
    if (state.transcribingVoice) return;
    if (state.recordingVoice) {
      var chunks = (voiceRecorder.chunks || []).slice();
      var sr = (voiceRecorder.audioCtx && voiceRecorder.audioCtx.sampleRate) || 16000;
      stopVoiceRecorder(ctx, voiceRecorder);
      state.transcribingVoice = true;
      ctx.updateComposerButtons();
      var totalLen = chunks.reduce(function (a, c) { return a + c.length; }, 0);
      if (totalLen < 800) {
        state.transcribingVoice = false;
        ctx.updateComposerButtons();
        return;
      }
      var merged = new Float32Array(totalLen);
      var off = 0;
      for (var i = 0; i < chunks.length; i++) {
        merged.set(chunks[i], off);
        off += chunks[i].length;
      }
      ctx.apiCall("/local/speech-to-text", "POST", { audio_b64: encodeMonoWav(merged, sr), language: "auto" }).then(function (res) {
        state.transcribingVoice = false;
        if (res && res.ok && res.text) {
          var ta = document.getElementById("dsb-chat-input");
          if (ta) {
            ta.value = (ta.value ? ta.value + " " : "") + res.text;
            ctx.autoFitTextarea(ta);
            ta.focus();
          }
        } else {
          ctx.showToast((res && res.error) || "未识别到语音");
        }
        ctx.updateComposerButtons();
      });
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      ctx.showToast("当前环境不支持麦克风录音");
      return;
    }
    voiceRecorder.cancelled = false;
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      if (voiceRecorder.cancelled) {
        try { stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
        return;
      }
      var AC = window.AudioContext || window.webkitAudioContext;
      var audioCtx = new AC({ sampleRate: 16000 });
      var source = audioCtx.createMediaStreamSource(stream);
      var proc = audioCtx.createScriptProcessor(4096, 1, 1);
      voiceRecorder.stream = stream;
      voiceRecorder.audioCtx = audioCtx;
      voiceRecorder.source = source;
      voiceRecorder.processor = proc;
      voiceRecorder.chunks = [];
      proc.onaudioprocess = function (ev) {
        if (state.recordingVoice) voiceRecorder.chunks.push(new Float32Array(ev.inputBuffer.getChannelData(0)));
      };
      source.connect(proc);
      proc.connect(audioCtx.destination);
      state.recordingVoice = true;
      ctx.showToast("正在聆听，再次点击麦克风完成转写");
      ctx.updateComposerButtons();
    }).catch(function () {
      stopVoiceRecorder(ctx, voiceRecorder);
      ctx.updateComposerButtons();
      ctx.showToast("无法访问麦克风权限");
    });
  }

  function openBotStudio(ctx, mode) {
    var state = ctx.state;
    var bot = ctx.getActiveBot();
    state.studioMode = mode === "create" ? "create" : "edit";
    state.studioOpen = true;
    state.vaultOpen = false;
    state.botSwitcherOpen = false;
    if (state.studioMode === "create") {
      state.editBotName = "";
      state.editBotDesc = "";
      state.editBotColor = ctx.PRESET_COLORS[state.bots.length % ctx.PRESET_COLORS.length];
      state.editBotShape = "circle";
      state.editBotPersona = "";
      state.editBotModel = "";
      state.editBotCuriosity = false;
    } else {
      state.editBotName = bot.name || "";
      state.editBotDesc = bot.description || "";
      state.editBotColor = bot.color || "#B568F7";
      state.editBotShape = bot.shape || "flower";
      state.editBotPersona = bot.instructions || bot.persona || "";
      state.editBotModel = bot.model || "";
      state.editBotCuriosity = Boolean(bot.curiosity_enabled);
    }
    state.promptPreviewData = null;
    state.promptPreviewLoading = false;
    var studioReqTok = (state._studioReqTok = (state._studioReqTok || 0) + 1);
    ctx.renderSheets();
    if (state.studioMode !== "create") {
      ctx.apiCall("/local/prompt-preview", "POST", { bot_id: bot.id }).then(function (pv) {
        if (studioReqTok !== state._studioReqTok || !state.studioOpen) return;
        if (pv && pv.ok) {
          state.promptPreviewData = pv;
          var slot = document.getElementById("dsb-st-prompt-slot");
          var R = typeof window !== "undefined" ? window.__DSB_RENDER__ : null;
          if (slot && R && R.buildPromptPreviewSectionHtml) {
            slot.innerHTML = R.buildPromptPreviewSectionHtml(pv);
          } else {
            syncStudioDraftFromInputs(state);
            ctx.renderSheets();
          }
        }
      });
    }
    ctx.apiCall("/local/models", "POST", {}).then(function (r) {
      if (studioReqTok !== state._studioReqTok || !state.studioOpen) return;
      if (r && r.ok === false && r.error) ctx.showToast("获取模型列表失败: " + r.error);
      if (r && Array.isArray(r.models)) {
        state.availableModels = r.models;
        var sel = document.getElementById("dsb-st-model");
        if (sel) {
          var curVal = sel.value || state.editBotModel || "";
          var esc = ctx.escapeHtml;
          sel.innerHTML = ['<option value="">跟随系统默认模型</option>'].concat(state.availableModels.map(function (m) {
            var mid = typeof m === "string" ? m : (m.id || "");
            return '<option value="' + esc(mid) + '"' + (mid === curVal ? " selected" : "") + ">" + esc(mid) + "</option>";
          })).join("");
          if (curVal) sel.value = curVal;
        }
      }
    });
  }

  function loadSharedFiles(ctx) {
    var state = ctx.state;
    state.vaultStatusMsg = "正在同步三端文件列表…";
    ctx.apiCall("/local/files", "GET").then(function (r) {
      if (r && r.ok) {
        state.sharedFiles = { local: r.local || [], cloud: r.cloud || [], desktop: r.desktop || [] };
        state.vaultStatusMsg = "";
      } else {
        state.vaultStatusMsg = "文件同步失败: " + ((r && r.error) || "网络异常");
        ctx.showToast(state.vaultStatusMsg);
      }
      ctx.renderSheets();
    });
  }

  function loadVaultFile(ctx, relPath) {
    var state = ctx.state;
    var clean = String(relPath || "").replace(/^memory_vault\//, "");
    state.vaultSelectedPath = clean;
    var reqTok = (state._vaultFileReqTok = (state._vaultFileReqTok || 0) + 1);
    ctx.apiCall("/local/vault", "POST", { op: "read", path: clean }).then(function (r) {
      if (reqTok !== state._vaultFileReqTok || state.vaultSelectedPath !== clean) return;
      if (r && r.ok === false && r.error) ctx.showToast("读取文档失败: " + r.error);
      state.vaultSelectedContent = (r && typeof r.content === "string") ? r.content : "";
      ctx.renderSheets();
    });
  }

  function openVaultSheet(ctx, initialPath, initialTab) {
    var state = ctx.state;
    state.vaultOpen = true;
    state.studioOpen = false;
    state.botSwitcherOpen = false;
    state.vaultLoading = true;
    if (initialTab) state.vaultTab = initialTab;
    ctx.renderSheets();
    if (state.vaultTab === "files") {
      state.vaultLoading = false;
      loadSharedFiles(ctx);
      return;
    }
    ctx.apiCall("/local/vault", "POST", { op: "list" }).then(function (res) {
      state.vaultLoading = false;
      if (res && res.ok === false && res.error) ctx.showToast("加载记忆金库失败: " + res.error);
      state.vaultFiles = Array.isArray(res && res.files) ? res.files : [];
      state.vaultPinned = Array.isArray(res && (res.pins || res.pinned_memories)) ? (res.pins || res.pinned_memories) : state.vaultPinned;
      var pick = initialPath || state.vaultSelectedPath || (state.vaultFiles[0] && (state.vaultFiles[0].path || state.vaultFiles[0]));
      if (pick) loadVaultFile(ctx, String(pick));
      else ctx.renderSheets();
      ctx.renderHeaderAndPins();
    });
  }

  function supportsTopLayerExit() {
    if (typeof window === "undefined" || !window.CSS || typeof CSS.supports !== "function") return false;
    return CSS.supports("transition-behavior", "allow-discrete") && CSS.supports("transition-property", "overlay");
  }

  function showTopLayerModalDialog(dlg, onClosed) {
    if (!dlg) return;
    var pointerDownOnBackdrop = false, removed = false;
    var cleanup = function () {
      if (removed) return;
      removed = true;
      if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
      if (typeof onClosed === "function") onClosed();
    };
    var startAnimatedClose = function () {
      if (removed || dlg.hasAttribute("data-closing")) return;
      if (!dlg.open) { cleanup(); return; }
      if (supportsTopLayerExit()) {
        dlg.close();
        var timer = setTimeout(cleanup, 260);
        dlg.addEventListener("transitionend", function onEnd(ev) {
          if (ev.target === dlg) { clearTimeout(timer); dlg.removeEventListener("transitionend", onEnd); cleanup(); }
        });
        return;
      }
      dlg.setAttribute("data-closing", "");
      var fallbackDone = false;
      var finishClose = function () {
        if (fallbackDone) return;
        fallbackDone = true; dlg.removeAttribute("data-closing");
        if (dlg.open) dlg.close();
        cleanup();
      };
      dlg.addEventListener("transitionend", function onEndFallback(ev) {
        if (ev.target === dlg) { dlg.removeEventListener("transitionend", onEndFallback); finishClose(); }
      });
      setTimeout(finishClose, 260);
    };
    dlg.closeAnimated = startAnimatedClose;

    if (typeof HTMLDialogElement === "undefined" || !("closedBy" in HTMLDialogElement.prototype)) {
      var isClickOutsideDialogBox = function (ev) {
        var box = (dlg.classList && dlg.classList.contains("dsb-sheet-dialog")) ? dlg : dlg.firstElementChild;
        if (!box || typeof box.getBoundingClientRect !== "function") return ev.target === dlg;
        var rect = box.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) return ev.target === dlg;
        return ev.clientX < rect.left || ev.clientX > rect.right || ev.clientY < rect.top || ev.clientY > rect.bottom;
      };
      dlg.addEventListener("pointerdown", function (ev) { pointerDownOnBackdrop = ev.target === dlg && isClickOutsideDialogBox(ev); });
      dlg.addEventListener("pointerup", function (ev) { if (pointerDownOnBackdrop && ev.target === dlg && isClickOutsideDialogBox(ev)) startAnimatedClose(); pointerDownOnBackdrop = false; });
    }
    dlg.addEventListener("cancel", function (ev) { if (!supportsTopLayerExit()) { ev.preventDefault(); startAnimatedClose(); } });
    dlg.addEventListener("close", function () { if (!dlg.hasAttribute("data-closing")) setTimeout(cleanup, supportsTopLayerExit() ? 220 : 0); });
    dlg.addEventListener("click", function (ev) { if (ev.target && ev.target.closest && ev.target.closest("[data-dlg-close]")) startAnimatedClose(); });
    (document.getElementById("dsb-split-pane") || document.body).appendChild(dlg);
    dlg.showModal();
  }

  function sanitizeDialogMediaHref(rawUrl) {
    var R = typeof window !== "undefined" ? window.__DSB_RENDER__ : null;
    if (R && typeof R.sanitizeHref === "function") return R.sanitizeHref(rawUrl);
    var u = String(rawUrl || "").trim();
    if (/^(?:javascript|vbscript|file):/i.test(u)) return "";
    if (/^data:(?!image\/(?:png|jpeg|jpg|webp|gif);base64,)/i.test(u)) return "";
    return u;
  }

  function openLightbox(escapeHtmlOrCtx, src) {
    var safeSrc = sanitizeDialogMediaHref(src);
    if (!safeSrc) return;
    var esc = resolveEscFn(escapeHtmlOrCtx);
    var dlg = document.createElement("dialog");
    dlg.className = "dsb-modal-dialog";
    dlg.setAttribute("closedby", "any");
    dlg.setAttribute("aria-label", "图片大图预览");
    dlg.innerHTML = '<div class="dsb-lightbox-stage"><img src="' + esc(safeSrc) + '" alt="预览大图"/><div class="dsb-lightbox-bar"><a class="dsb-btn-sec" href="' + esc(safeSrc) + '" target="_blank" rel="noopener noreferrer" style="text-decoration:none;display:inline-flex;align-items:center;">原图打开</a><button type="button" class="dsb-btn-pri" data-dlg-close="1">关闭</button></div></div>';
    showTopLayerModalDialog(dlg);
  }
  function triggerFileDownload(downloadUrl, fileName) {
    var safeUrl = sanitizeDialogMediaHref(downloadUrl);
    if (!safeUrl) return;
    var a = document.createElement("a");
    a.href = safeUrl;
    if (fileName) a.download = fileName;
    a.target = "_blank"; a.rel = "noopener noreferrer";
    document.body.appendChild(a); a.click();
    setTimeout(function () { if (a.parentNode) a.parentNode.removeChild(a); }, 400);
  }
  function openFilePreview(escapeHtml, previewUrl, fileName, ext, downloadUrl) {
    var safePreview = sanitizeDialogMediaHref(previewUrl), safeDownload = sanitizeDialogMediaHref(downloadUrl) || safePreview;
    if (!safePreview) return;
    if (!/^(PDF|TXT|MD|CSV|JSON|HTML)$/i.test(String(ext || ""))) { triggerFileDownload(safeDownload, fileName); return; }
    var esc = resolveEscFn(escapeHtml), dlg = document.createElement("dialog");
    dlg.className = "dsb-modal-dialog";
    dlg.setAttribute("closedby", "any");
    dlg.setAttribute("aria-label", fileName || "文档预览");
    dlg.innerHTML = '<div class="dsb-file-preview-box"><div class="dsb-file-preview-head"><span class="dsb-file-preview-title">' + esc(fileName || "文档预览") + '</span><div class="dsb-file-preview-actions"><a class="dsb-btn-sec" href="' + esc(safePreview) + '" target="_blank" rel="noopener noreferrer" style="text-decoration:none;display:inline-flex;align-items:center;">新窗口打开</a><button type="button" class="dsb-btn-pri" data-fp-dl="1">下载</button><button type="button" class="dsb-btn-sec" data-dlg-close="1">关闭</button></div></div><iframe class="dsb-file-preview-frame" sandbox="" src="' + esc(safePreview) + '" title="' + esc(fileName || "PDF 预览") + '"></iframe></div>';
    dlg.addEventListener("click", function (ev) {
      if (ev.target && ev.target.closest && ev.target.closest("[data-fp-dl]")) triggerFileDownload(safeDownload, fileName);
    });
    showTopLayerModalDialog(dlg);
  }

  function applySyncPayload(ctx, res) {
    if (!res || !res.ok) return;
    var s = ctx.state;
    if (Array.isArray(res.bots) && res.bots.length) s.bots = res.bots;
    if (Array.isArray(res.messages)) s.messages = res.messages;
    if (res.messages_epoch) s.messagesEpoch = Number(res.messages_epoch) || 1;
    if (res.streams_by_bot && typeof res.streams_by_bot === "object") s.streamsByBot = res.streams_by_bot;
    if (res.bot_context_by_id && typeof res.bot_context_by_id === "object") s.botContextById = res.bot_context_by_id;
    if (typeof res.vps_online === "boolean") s.vpsOnline = res.vps_online;
    if (typeof res.rtt_ms === "number") s.rttMs = res.rtt_ms;
    if (typeof res.phone_online === "boolean") s.phoneOnline = res.phone_online;
    var pins = res.pins || res.pinned_memories;
    if (Array.isArray(pins)) s.vaultPinned = pins;
    ctx.renderRightLauncher(); ctx.renderHeaderAndPins(); ctx.updateComposerButtons();
  }
  function syncStudioDraftFromInputs(state) {
    var g = function (id) { return document.getElementById(id); };
    if (g("dsb-st-name")) state.editBotName = g("dsb-st-name").value;
    if (g("dsb-st-desc")) state.editBotDesc = g("dsb-st-desc").value;
    if (g("dsb-st-model")) state.editBotModel = g("dsb-st-model").value;
    if (g("dsb-st-persona")) state.editBotPersona = g("dsb-st-persona").value;
    if (g("dsb-st-curiosity")) state.editBotCuriosity = Boolean(g("dsb-st-curiosity").checked);
  }
  var copyTimers = typeof WeakMap !== "undefined" ? new WeakMap() : new Map(), copyOrigHtml = typeof WeakMap !== "undefined" ? new WeakMap() : new Map(), lastSrCopyMsg = "";
  function announceSrStatus(msg) {
    var sr = typeof document !== "undefined" ? document.getElementById("dsb-sr-status") : null;
    if (!sr) return;
    var out = String(msg || "");
    if (out && out === lastSrCopyMsg) out += "\u00A0";
    if (out) lastSrCopyMsg = String(msg || "");
    sr.textContent = "";
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(function () { sr.textContent = out; });
    else sr.textContent = out;
  }
  function legacyCopyText(text) {
    try {
      var f = document.createElement("textarea"); f.value = text; f.setAttribute("readonly", "");
      f.style.cssText = "position:fixed;inset-block-start:0;opacity:0;pointer-events:none;";
      document.body.appendChild(f); f.select();
      try { return document.execCommand("copy"); } finally { f.remove(); }
    } catch (e) { return false; }
  }
  function copyTextWithFeedback(ctx, buttonEl, text, okToast) {
    var val = String(text || ""); if (!val) return Promise.resolve(false);
    var report = function (ok) {
      if (ctx && ctx.showToast) ctx.showToast(ok ? (okToast || "已复制") : "复制失败");
      announceSrStatus(ok ? "Copied" : "Copy failed");
      if (!buttonEl || typeof buttonEl.setAttribute !== "function") return;
      if (!copyOrigHtml.has(buttonEl)) copyOrigHtml.set(buttonEl, buttonEl.innerHTML);
      var prevT = copyTimers.get(buttonEl); if (prevT) clearTimeout(prevT);
      buttonEl.removeAttribute("data-copied"); buttonEl.removeAttribute("data-failed");
      var R = typeof window !== "undefined" ? window.__DSB_RENDER__ : null;
      var ico = R && R.iconSvg ? R.iconSvg(ok ? "check" : "alert", 12) : "";
      buttonEl.setAttribute(ok ? "data-copied" : "data-failed", "1");
      buttonEl.innerHTML = ico + "<span>" + (ok ? "已复制" : "复制失败") + "</span>";
      copyTimers.set(buttonEl, setTimeout(function () {
        buttonEl.removeAttribute("data-copied"); buttonEl.removeAttribute("data-failed");
        if (copyOrigHtml.has(buttonEl)) buttonEl.innerHTML = copyOrigHtml.get(buttonEl);
        announceSrStatus("");
      }, 1800));
    };
    if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(val).then(function () { report(true); return true; }).catch(function () { var fb = legacyCopyText(val); report(fb); return fb; });
    }
    var okSync = legacyCopyText(val); report(okSync); return Promise.resolve(okSync);
  }
  function buildMessageSignature(m) {
    if (!m) return "";
    var vLen = Array.isArray(m.variants) ? m.variants.length : 0, vIdx = m.active_variant_idx !== undefined ? m.active_variant_idx : -1, cLen = Array.isArray(m.choices) ? m.choices.length : 0, t = String(m.text || "");
    return [m.id || "", m.kind || "", t.length, t.slice(0, 16), t.slice(-16), (m.thought || "").length, m.sticker || "", (m.attachments || []).length, (m.tool_trace || []).length, (m.turn_steps || []).length, (m.timeline || []).length, m.tps || 0, vLen, vIdx, cLen].join(":");
  }
  function reconcileChatHistory(ctx, historyEl, roomMsgs, bot) {
    if (!historyEl) return { changed: false, addedNew: false };
    var list = Array.isArray(roomMsgs) ? roomMsgs : [];
    if (!list.length) {
      var curEmpty = historyEl.querySelector(".dsb-empty-state");
      if (curEmpty && historyEl.children.length === 1 && curEmpty.getAttribute("data-empty-bot") === String(bot && bot.id || "")) return { changed: false, addedNew: false };
      var R = typeof window !== "undefined" ? window.__DSB_RENDER__ : null;
      historyEl.innerHTML = (R && R.buildEmptyStateHtml)
        ? R.buildEmptyStateHtml(bot, ctx.state.whaleAvatarUrl)
        : ('<div class="dsb-empty-state" data-empty-bot="' + ctx.escapeHtml((bot && bot.id) || "") + '">' + ctx.renderBotAvatarSvg(bot, 48) + '<div class="dsb-empty-title">' + ctx.escapeHtml((bot && bot.name) || "") + '</div><div class="dsb-empty-sub">' + ctx.escapeHtml((bot && bot.description) || "独立会话与思维链空间") + "</div></div>");
      return { changed: true, addedNew: false };
    }
    var emptyOld = historyEl.querySelector(".dsb-empty-state");
    if (emptyOld && emptyOld.parentNode === historyEl) emptyOld.parentNode.removeChild(emptyOld);
    var existingById = {}, prevLastId = historyEl.lastElementChild ? historyEl.lastElementChild.getAttribute("data-msg-id") : "";
    for (var k = 0; k < historyEl.children.length; k++) { var eid = historyEl.children[k].getAttribute("data-msg-id"); if (eid) existingById[eid] = historyEl.children[k]; }
    var changed = false, addedNew = false, seenIds = {}, scratch = document.createElement("div");
    for (var i = 0; i < list.length; i++) {
      var m = list[i], mid = String((m && m.id) || ("row-" + i)), sig = buildMessageSignature(m), isDef = i < Math.max(0, list.length - 12), node = existingById[mid] || null;
      seenIds[mid] = true;
      if (!node || node.getAttribute("data-msg-sig") !== sig) {
        scratch.innerHTML = ctx.renderSingleMessageHtml(m);
        var fresh = scratch.firstElementChild; if (!fresh) continue;
        fresh.setAttribute("data-msg-id", mid); fresh.setAttribute("data-msg-sig", sig); fresh.classList.toggle("is-deferred", isDef);
        if (node && node.parentNode === historyEl) historyEl.replaceChild(fresh, node); else addedNew = true;
        node = fresh; changed = true;
      } else node.classList.toggle("is-deferred", isDef);
      var curAt = historyEl.children[i] || null;
      if (node !== curAt) { historyEl.insertBefore(node, curAt); changed = true; }
    }
    for (var j = historyEl.children.length - 1; j >= 0; j--) {
      var tailEl = historyEl.children[j], tMid = tailEl.getAttribute("data-msg-id");
      if (!tMid || !seenIds[tMid] || j >= list.length) { historyEl.removeChild(tailEl); changed = true; }
    }
    var newLastId = list[list.length - 1] ? String(list[list.length - 1].id || "") : "";
    if (newLastId && newLastId !== prevLastId) addedNew = true;
    return { changed: changed, addedNew: addedNew };
  }

  function getFilteredSlashCommands(rawInput, allCommands) {
    var cmds = Array.isArray(allCommands) ? allCommands : [];
    var s = String(rawInput || "").trim().toLowerCase();
    if (!s || s.indexOf("/") !== 0) return [];
    if (s === "/") return cmds.slice();
    return cmds.filter(function (sc) {
      var c = String((sc && sc.cmd) || "").toLowerCase();
      var d = String((sc && sc.desc) || "").toLowerCase();
      return c.indexOf(s) === 0 || (s.length > 1 && d.indexOf(s.slice(1)) !== -1);
    });
  }

  function renderSlashMenu(ctx, rawInput, allCommands) {
    var slashEl = document.getElementById("dsb-slash-pop");
    if (!slashEl) return;
    var state = ctx.state;
    var items = state.slashMenuOpen ? getFilteredSlashCommands(rawInput, allCommands) : [];
    if (!state.slashMenuOpen || !items.length) {
      slashEl.style.display = "none";
      slashEl.innerHTML = "";
      return;
    }
    if (state.slashActiveIdx < 0 || state.slashActiveIdx >= items.length) state.slashActiveIdx = 0;
    slashEl.style.display = "block";
    slashEl.innerHTML = items.map(function (sc, idx) {
      var active = idx === state.slashActiveIdx;
      return '<button type="button" role="option" aria-selected="' + (active ? "true" : "false") + '" class="dsb-slash-item' + (active ? " is-selected" : "") + '" data-pick-slash="' + ctx.escapeHtml(sc.cmd) + '"><span class="dsb-slash-cmd">' + ctx.escapeHtml(sc.cmd.trim()) + '</span><span style="color:var(--dsb-muted);font-size:11.5px;">' + ctx.escapeHtml(sc.desc) + "</span></button>";
    }).join("");
    var selEl = slashEl.querySelector(".dsb-slash-item.is-selected");
    if (selEl && typeof selEl.scrollIntoView === "function") {
      try { selEl.scrollIntoView({ block: "nearest" }); } catch (e) {}
    }
  }

  function supportsNativePopover() {
    return typeof HTMLElement !== "undefined" && Object.prototype.hasOwnProperty.call(HTMLElement.prototype, "popover");
  }

  function positionStickerPopover(pop, triggerBtn) {
    if (!pop || !triggerBtn || typeof triggerBtn.getBoundingClientRect !== "function") return;
    var rect = triggerBtn.getBoundingClientRect();
    var popW = Math.min(312, Math.max(240, window.innerWidth - 24));
    var left = Math.max(12, Math.min(window.innerWidth - popW - 12, rect.left));
    var bottom = Math.max(12, window.innerHeight - rect.top + 8);
    pop.style.width = popW + "px";
    pop.style.left = Math.round(left) + "px";
    pop.style.bottom = Math.round(bottom) + "px";
    pop.style.top = "auto";
    pop.style.right = "auto";
  }

  function renderStickerPopover(ctx) {
    var pop = document.getElementById("dsb-sticker-pop");
    var btn = document.getElementById("dsb-sticker-btn");
    if (!pop) return;
    var state = ctx.state;
    var R = typeof window !== "undefined" ? window.__DSB_RENDER__ : null;
    if (R && R.buildStickerPopoverHtml) {
      pop.innerHTML = R.buildStickerPopoverHtml(state.stickerPacks, state.activeStickerPackId, state.stickerLoading);
    }
    if (btn) btn.setAttribute("aria-expanded", state.stickerOpen ? "true" : "false");
    if (btn) btn.classList.toggle("is-active", Boolean(state.stickerOpen));
    if (!state.stickerOpen) {
      if (supportsNativePopover() && pop.matches && pop.matches(":popover-open")) {
        try { pop.hidePopover(); } catch (e) {}
      }
      pop.classList.remove("is-open");
      return;
    }
    positionStickerPopover(pop, btn);
    if (supportsNativePopover() && typeof pop.showPopover === "function") {
      if (!pop.matches(":popover-open")) {
        try { pop.showPopover(); } catch (e) { pop.classList.add("is-open"); }
      }
    } else {
      pop.classList.add("is-open");
    }
  }

  function closeStickerPicker(ctx) {
    if (!ctx || !ctx.state) return;
    if (!ctx.state.stickerOpen) return;
    ctx.state.stickerOpen = false;
    renderStickerPopover(ctx);
  }

  function loadStickersIfNeeded(ctx) {
    var state = ctx.state;
    if ((!state.stickerPacks || !state.stickerPacks.length) && !state.stickerLoading) {
      state.stickerLoading = true;
      renderStickerPopover(ctx);
      ctx.apiCall("/local/stickers", "POST", {}).then(function (res) {
        state.stickerLoading = false;
        if (res && Array.isArray(res.sticker_packs) && res.sticker_packs.length) {
          state.stickerPacks = res.sticker_packs;
          if (!state.activeStickerPackId) state.activeStickerPackId = res.sticker_packs[0].id || "";
        } else if (res && res.ok === false && res.error && ctx.showToast) {
          ctx.showToast("加载表情包失败: " + res.error);
        }
        renderStickerPopover(ctx);
      });
      return true;
    }
    return false;
  }

  function ensureStickerToggleBound(ctx) {
    var pop = document.getElementById("dsb-sticker-pop"), state = ctx.state;
    if (!pop || pop.getAttribute("data-stk-bound")) return;
    pop.setAttribute("data-stk-bound", "1");
    pop.addEventListener("toggle", function (ev) {
      state.stickerOpen = ev.newState === "open";
      var btn = document.getElementById("dsb-sticker-btn");
      if (btn) {
        btn.setAttribute("aria-expanded", state.stickerOpen ? "true" : "false");
        btn.classList.toggle("is-active", state.stickerOpen);
      }
      if (state.stickerOpen) {
        positionStickerPopover(pop, btn);
        if (!loadStickersIfNeeded(ctx)) renderStickerPopover(ctx);
      }
    });
  }

  function toggleStickerPicker(ctx) {
    ensureStickerToggleBound(ctx);
    if (supportsNativePopover()) return;
    ctx.state.stickerOpen = !ctx.state.stickerOpen;
    if (ctx.state.stickerOpen && loadStickersIfNeeded(ctx)) return;
    renderStickerPopover(ctx);
  }

  function sendStickerCode(ctx, code, url) {
    var c = String(code || "").trim();
    if (!c) return;
    closeStickerPicker(ctx);
    if (typeof ctx.submitTurn === "function") {
      var stUrl = String(url || "");
      ctx.submitTurn({ overrideText: "[sticker:" + c + "]", stickerUrl: stUrl, sticker: stUrl || c });
    }
    var ta = document.getElementById("dsb-chat-input");
    if (ta && typeof ta.focus === "function") ta.focus();
  }

  function updateScrollBottomPill(ctx, hasNewActivity) {
    var scrollEl = document.getElementById("dsb-chat-scroll");
    var pill = document.getElementById("dsb-scroll-bottom-btn");
    var hdr = document.querySelector("#dsb-split-pane .dsb-header");
    if (hdr && scrollEl) hdr.classList.toggle("is-scrolled", scrollEl.scrollTop > 4);
    if (!scrollEl || !pill) return;
    var dist = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
    var far = dist > 160;
    if (!far) {
      ctx.state.unreadWhileScrolledUp = false;
      pill.classList.remove("is-visible", "has-unread");
      return;
    }
    if (hasNewActivity) ctx.state.unreadWhileScrolledUp = true;
    pill.classList.add("is-visible");
    pill.classList.toggle("has-unread", Boolean(ctx.state.unreadWhileScrolledUp));
    var lbl = pill.querySelector("[data-sb-label]");
    if (lbl) lbl.textContent = ctx.state.unreadWhileScrolledUp ? "有新回复" : "回到底部";
  }

  function renderSheets(ctx) {
    var state = ctx.state, R = ctx.R ? ctx.R() : (typeof window !== "undefined" ? window.__DSB_RENDER__ : null);
    var dlg = document.getElementById("dsb-sheet-dialog");
    if (!state.studioOpen && !state.vaultOpen) {
      if (dlg) {
        if (typeof dlg.closeAnimated === "function") dlg.closeAnimated();
        else if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
      }
      return;
    }
    if (!R) return;
    var html = state.studioOpen
      ? R.buildStudioSheetHtml(state, ctx.getActiveBot(), ctx.PRESET_COLORS || [], ctx.SHAPE_OPTIONS || [])
      : R.buildVaultSheetHtml(state);
    if (dlg && dlg.open && !dlg.hasAttribute("data-closing")) {
      dlg.setAttribute("aria-label", state.studioOpen ? "助理设置" : "记忆金库与跨端文件");
      dlg.innerHTML = html;
      return;
    }
    if (dlg) { dlg.removeAttribute("id"); if (typeof dlg.closeAnimated === "function") dlg.closeAnimated(); }
    var nd = document.createElement("dialog");
    nd.id = "dsb-sheet-dialog"; nd.className = "dsb-sheet-dialog";
    nd.setAttribute("closedby", "any");
    nd.setAttribute("aria-label", state.studioOpen ? "助理设置" : "记忆金库与跨端文件");
    nd.innerHTML = html;
    showTopLayerModalDialog(nd, function () {
      if (state.studioOpen || state.vaultOpen) {
        state.studioOpen = false; state.vaultOpen = false; ctx.renderHeaderAndPins();
      }
    });
  }

  function openCommandPalette(ctx) {
    var existing = document.getElementById("dsb-cmdk-dialog");
    if (existing && existing.open && !existing.hasAttribute("data-closing")) {
      if (typeof existing.closeAnimated === "function") existing.closeAnimated();
      return;
    }
    if (existing) { existing.removeAttribute("id"); if (typeof existing.closeAnimated === "function") existing.closeAnimated(); }
    var R = ctx.R ? ctx.R() : (typeof window !== "undefined" ? window.__DSB_RENDER__ : null);
    if (!R || !R.buildCommandPaletteHtml || !R.getCommandPaletteItems) return;
    var dlg = document.createElement("dialog");
    dlg.id = "dsb-cmdk-dialog"; dlg.className = "dsb-cmdk-dialog";
    dlg.setAttribute("closedby", "any"); dlg.setAttribute("aria-label", "快捷命令面板");
    var q = "", selIdx = 0, slashCmds = ctx.SLASH_COMMANDS || [];
    var ico = R.iconSvg ? R.iconSvg("search", 15) : "";
    dlg.innerHTML = '<div class="dsb-cmdk-card"><div class="dsb-cmdk-head">' + ico +
      '<input id="dsb-cmdk-inp" class="dsb-cmdk-inp" type="text" placeholder="搜索助理、记忆金库、设置或斜杠指令…" autocomplete="off"/></div>' +
      '<div id="dsb-cmdk-list" class="dsb-cmdk-list" role="listbox"></div>' +
      '<div class="dsb-cmdk-foot"><span>↑↓ 选择 · Enter 执行 · Esc 关闭</span><span>Ctrl+K</span></div></div>';
    var renderList = function () {
      var listEl = dlg.querySelector("#dsb-cmdk-list");
      var items = R.getCommandPaletteItems(q, ctx.state, slashCmds);
      selIdx = items.length ? ((selIdx % items.length) + items.length) % items.length : 0;
      if (listEl) listEl.innerHTML = R.buildCommandPaletteHtml(q, ctx.state, slashCmds, selIdx);
      return items;
    };
    var execItem = function (it) {
      if (!it) return;
      if (typeof dlg.closeAnimated === "function") dlg.closeAnimated();
      if (it.kind === "bot" && ctx.selectBot) ctx.selectBot(it.id);
      else if (it.kind === "act") {
        if (it.id === "vault" && ctx.openVaultSheet) ctx.openVaultSheet("", "notes");
        else if (it.id === "files" && ctx.openVaultSheet) ctx.openVaultSheet("", "files");
        else if (it.id === "studio" && ctx.openBotStudio) ctx.openBotStudio("edit");
        else if (it.id === "create" && ctx.openBotStudio) ctx.openBotStudio("create");
        else if (it.id === "theme" && ctx.toggleTheme) ctx.toggleTheme();
        else if (it.id === "compact" && ctx.runCompact) ctx.runCompact();
      } else if (it.kind === "slash") {
        var ta = document.getElementById("dsb-chat-input");
        if (ta) {
          ta.value = it.id; ctx.autoFitTextarea(ta);
          if (String(it.id).slice(-1) !== " ") ctx.submitTurn();
          else ta.focus();
        }
      }
    };
    dlg.addEventListener("input", function (ev) {
      if (ev.target && ev.target.id === "dsb-cmdk-inp") { q = ev.target.value || ""; selIdx = 0; renderList(); }
    });
    dlg.addEventListener("keydown", function (ev) {
      var items = R.getCommandPaletteItems(q, ctx.state, slashCmds);
      if (!items.length) return;
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault(); selIdx = (selIdx + (ev.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        var listItems = renderList(), activeEl = dlg.querySelector(".dsb-cmdk-item.is-selected");
        if (listItems.length && activeEl && activeEl.scrollIntoView) activeEl.scrollIntoView({ block: "nearest" });
      } else if (ev.key === "Enter") { ev.preventDefault(); execItem(items[selIdx]); }
    });
    dlg.addEventListener("click", function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest("[data-cmdk-idx]") : null;
      if (btn) execItem(R.getCommandPaletteItems(q, ctx.state, slashCmds)[Number(btn.getAttribute("data-cmdk-idx"))]);
    });
    showTopLayerModalDialog(dlg);
    renderList();
    setTimeout(function () { var inp = dlg.querySelector("#dsb-cmdk-inp"); if (inp) inp.focus(); }, 20);
  }

  function switchMessageVariantLocal(ctx, swVarBtn) {
    var state = ctx.state, vMid = swVarBtn.getAttribute("data-mid"), vIdx = Number(swVarBtn.getAttribute("data-vidx"));
    var targetM = state.messages.find(function (x) { return x && x.id === vMid; });
    if (targetM && Array.isArray(targetM.variants) && targetM.variants[vIdx]) {
      var selV = targetM.variants[vIdx];
      targetM.active_variant_idx = vIdx; targetM.text = String(selV.text || ""); targetM.thought = String(selV.thought || "");
      targetM.tool_trace = Array.isArray(selV.trace) ? selV.trace : []; targetM.turn_steps = Array.isArray(selV.turn_steps) ? selV.turn_steps : [];
      targetM.timeline = Array.isArray(selV.timeline) ? selV.timeline : []; targetM.sticker = String(selV.sticker || "");
      targetM.attachments = Array.isArray(selV.attachments) ? selV.attachments : []; targetM.choices = Array.isArray(selV.choices) ? selV.choices : [];
      targetM.tps = Number(selV.tps || 0); targetM.ttft_ms = Number(selV.ttft_ms || 0); targetM.cache_hit_pct = Number(selV.cache_hit_pct || 0);
      state.lastStreamSig = ""; ctx.renderChatStream(false);
    }
    ctx.apiCall("/local/infinite-chat/variant", "POST", { bot_id: state.activeBotId, message_id: vMid, variant_idx: vIdx }).then(function (r) {
      if (!r || r.ok === false) { ctx.showToast("切换版本失败: " + ((r && r.error) || "网络异常")); return; }
      applySyncPayload(ctx, r); ctx.renderChatStream(false);
    });
  }

  function handleSheetClickAction(ctx, t) {
    var state = ctx.state, R = ctx.R ? ctx.R() : (window.__DSB_RENDER__ || null);
    var closeS = t.closest("[data-close-sheet]");
    if (closeS && (closeS.getAttribute("data-close-sheet") !== "mask" || t === closeS)) { state.studioOpen = false; state.vaultOpen = false; ctx.renderSheets(); ctx.renderHeaderAndPins(); return true; }
    var pickCol = t.closest("[data-pick-color]"); if (pickCol) { syncStudioDraftFromInputs(state); state.editBotColor = pickCol.getAttribute("data-pick-color"); ctx.renderSheets(); return true; }
    var pickSh = t.closest("[data-pick-shape]"); if (pickSh) { syncStudioDraftFromInputs(state); state.editBotShape = pickSh.getAttribute("data-pick-shape"); ctx.renderSheets(); return true; }
    if (t.closest("[data-export-bot]")) { if (R && R.exportBotArchive) R.exportBotArchive(ctx.apiCall, state.activeBotId, ctx.showToast); return true; }
    if (t.closest("[data-import-bot]")) {
      if (R && R.pickAndImportBotArchive) R.pickAndImportBotArchive(ctx.apiCall, function (r) {
        if (r && Array.isArray(r.bots)) state.bots = r.bots; if (r && r.active_bot_id) state.activeBotId = r.active_bot_id; if (r && Array.isArray(r.messages)) state.messages = r.messages;
        state.studioOpen = false; ctx.renderSheets(); ctx.renderRightLauncher(); ctx.renderHeaderAndPins(); ctx.renderChatStream(true);
      }, ctx.showToast);
      return true;
    }
    if (t.closest("[data-save-bot]")) {
      syncStudioDraftFromInputs(state); if (!state.editBotName.trim()) { ctx.showToast("请输入助理名称"); return true; }
      var isCr = state.studioMode === "create";
      ctx.apiCall(isCr ? "/local/bots/create" : "/local/bots/update", "POST", {
        bot_id: isCr ? "" : state.activeBotId, name: state.editBotName.trim(), description: state.editBotDesc.trim(),
        color: state.editBotColor, shape: state.editBotShape, instructions: state.editBotPersona, persona: state.editBotPersona, model: state.editBotModel, curiosity_enabled: state.editBotCuriosity
      }).then(function (r) {
        if (!r || r.ok === false) { ctx.showToast("保存失败: " + ((r && r.error) || "网络异常")); return; }
        if (Array.isArray(r.bots)) state.bots = r.bots; if (isCr && r.bot && r.bot.id) state.activeBotId = r.bot.id;
        state.studioOpen = false; ctx.renderSheets(); ctx.renderRightLauncher(); ctx.renderHeaderAndPins(); ctx.renderChatStream(true);
        ctx.showToast(r.cloud_warning ? "本地已保存（云端同步异常: " + r.cloud_warning + "）" : (isCr ? "已创建新助理" : "助理设定已保存"));
      });
      return true;
    }
    if (t.closest("[data-delete-bot]")) {
      ctx.apiCall("/local/bots/delete", "POST", { bot_id: state.activeBotId }).then(function (r) {
        if (!r || r.ok === false) { ctx.showToast("删除失败: " + ((r && r.error) || "操作失败")); return; }
        if (Array.isArray(r.bots)) state.bots = r.bots; state.activeBotId = r.active_bot_id || "pc_engineer";
        state.studioOpen = false; ctx.renderSheets(); ctx.renderRightLauncher(); ctx.renderHeaderAndPins(); ctx.renderChatStream(true); ctx.showToast("助理已删除");
      });
      return true;
    }
    var vTab = t.closest("[data-vault-tab]"); if (vTab) { state.vaultTab = vTab.getAttribute("data-vault-tab"); if (state.vaultTab === "files") loadSharedFiles(ctx); else ctx.renderSheets(); return true; }
    if (t.closest("[data-refresh-files]")) { loadSharedFiles(ctx); return true; }
    var syncBtn = t.closest("[data-sync-file]");
    if (syncBtn) {
      var dir = syncBtn.getAttribute("data-sync-file"), sp = syncBtn.getAttribute("data-sync-path"); state.vaultStatusMsg = "正在同步 " + sp + "…"; ctx.renderSheets();
      ctx.apiCall("/local/files/sync", "POST", { direction: dir, path: sp }).then(function (r) {
        state.vaultStatusMsg = r && r.ok ? "同步完成: " + (r.workspace_path || r.name || sp) : "同步失败: " + ((r && r.error) || "unknown");
        ctx.showToast(r && r.ok ? "跨端文件同步成功" : state.vaultStatusMsg); loadSharedFiles(ctx);
      });
      return true;
    }
    var vPath = t.closest("[data-vault-path]"); if (vPath) { loadVaultFile(ctx, vPath.getAttribute("data-vault-path")); return true; }
    if (t.closest("[data-save-vault]")) {
      var ed = document.getElementById("dsb-vault-editor"); if (!ed || !state.vaultSelectedPath) return true; state.vaultSelectedContent = ed.value;
      ctx.apiCall("/local/vault", "POST", { op: "write", path: state.vaultSelectedPath, content: ed.value }).then(function (r) {
        state.vaultStatusMsg = r && r.ok ? "已保存 " + new Date().toLocaleTimeString("zh-CN", { hour12: false }) : "保存失败: " + ((r && r.error) || "网络异常");
        ctx.renderSheets(); ctx.showToast(r && r.ok ? "记忆文档已保存" : state.vaultStatusMsg);
      });
      return true;
    }
    if (t.closest("[data-add-pin]")) {
      var pinInp = document.getElementById("dsb-new-pin-inp"), pTxt = pinInp ? pinInp.value.trim() : ""; if (!pTxt) return true;
      ctx.apiCall("/local/vault", "POST", { op: "pin", text: pTxt }).then(function (r) {
        if (!r || r.ok === false) { ctx.showToast("添加置顶失败: " + ((r && r.error) || "网络异常")); return; }
        if (Array.isArray(r.pins)) state.vaultPinned = r.pins; ctx.renderHeaderAndPins(); ctx.renderSheets(); ctx.showToast("已添加置顶记忆");
      });
      return true;
    }
    return false;
  }

  window.__DSB_CLIENT_UI__ = {
    uploadFileObject: uploadFileObject,
    retryAttachment: retryAttachment,
    removePendingAttachment: removePendingAttachment,
    revokeItemThumb: revokeItemThumb,
    encodeMonoWav: encodeMonoWav,
    stopVoiceRecorder: stopVoiceRecorder,
    toggleVoiceDictation: toggleVoiceDictation,
    openBotStudio: openBotStudio,
    loadSharedFiles: loadSharedFiles,
    openVaultSheet: openVaultSheet,
    loadVaultFile: loadVaultFile,
    openLightbox: openLightbox,
    openFilePreview: openFilePreview,
    triggerFileDownload: triggerFileDownload,
    applySyncPayload: applySyncPayload,
    syncStudioDraftFromInputs: syncStudioDraftFromInputs,
    copyTextWithFeedback: copyTextWithFeedback,
    buildMessageSignature: buildMessageSignature,
    reconcileChatHistory: reconcileChatHistory,
    getFilteredSlashCommands: getFilteredSlashCommands,
    renderSlashMenu: renderSlashMenu,
    ensureStickerToggleBound: ensureStickerToggleBound,
    toggleStickerPicker: toggleStickerPicker,
    renderStickerPopover: renderStickerPopover,
    closeStickerPicker: closeStickerPicker,
    sendStickerCode: sendStickerCode,
    updateScrollBottomPill: updateScrollBottomPill,
    renderSheets: renderSheets,
    openCommandPalette: openCommandPalette,
    switchMessageVariantLocal: switchMessageVariantLocal,
    handleSheetClickAction: handleSheetClickAction,
  };
})();
