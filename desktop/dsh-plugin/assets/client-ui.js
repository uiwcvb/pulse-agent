(function () {
  var MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024;

  function formatFileSize(bytes) {
    if (!bytes || bytes <= 0) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function extInfo(name) {
    var m = String(name || "").match(/\.([a-z0-9]{1,5})$/i);
    var ext = m ? m[1].toUpperCase() : "FILE";
    var cls = "file";
    if (ext === "PDF") cls = "pdf";
    else if (/^(ZIP|RAR|7Z|GZ|TAR)$/.test(ext)) cls = "zip";
    else if (/^(PY|KT|JS|TS|MJS|JAVA|JSON|YML|YAML|MD|SH|CSS|HTML)$/.test(ext)) cls = "code";
    else if (/^(XLS|XLSX|CSV)$/.test(ext)) cls = "sheet";
    else if (/^(DOC|DOCX|TXT|RTF)$/.test(ext)) cls = "doc";
    return { ext: ext.slice(0, 4), cls: cls };
  }

  function revokeItemThumb(item) {
    if (item && item.thumbUrl) {
      try { URL.revokeObjectURL(item.thumbUrl); } catch (e) {}
      item.thumbUrl = "";
    }
  }

  function runUploadItem(ctx, item) {
    var state = ctx.state;
    var file = item._file;
    if (!file) return;
    item.error = "";
    item.uploading = true;
    item.progress = 8;
    state.uploadingCount++;
    ctx.renderComposerBanners();

    var reader = new FileReader();
    reader.onprogress = function (ev) {
      if (ev && ev.lengthComputable && ev.total > 0) {
        item.progress = Math.max(10, Math.min(45, Math.round((ev.loaded / ev.total) * 38) + 8));
        ctx.renderComposerBanners();
      }
    };
    reader.onload = function () {
      item.progress = 55;
      ctx.renderComposerBanners();
      var rs = String(reader.result || "");
      var b64 = rs.indexOf(",") !== -1 ? rs.split(",")[1] : rs;
      var tick = setInterval(function () {
        if (!item.uploading || item.progress >= 92) { clearInterval(tick); return; }
        item.progress = Math.min(92, item.progress + 7);
        ctx.renderComposerBanners();
      }, 140);
      ctx.apiCall("/local/upload", "POST", { name: item.name, data_b64: b64 }).then(function (res) {
        clearInterval(tick);
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
    if (item.uploading) ctx.state.uploadingCount = Math.max(0, ctx.state.uploadingCount - 1);
    revokeItemThumb(item);
    list.splice(idx, 1);
    ctx.renderComposerBanners();
  }

  function encodeMonoWav(samples, sr) {
    var buf = new ArrayBuffer(44 + samples.length * 2);
    var view = new DataView(buf);
    function wStr(o, s) {
      for (var i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i));
    }
    wStr(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    wStr(8, "WAVE");
    wStr(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sr, true);
    view.setUint32(28, sr * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    wStr(36, "data");
    view.setUint32(40, samples.length * 2, true);
    for (var i = 0; i < samples.length; i++) {
      var s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    var bytes = new Uint8Array(buf);
    var bin = "";
    for (var j = 0; j < bytes.length; j++) bin += String.fromCharCode(bytes[j]);
    return window.btoa(bin);
  }

  function toggleVoiceDictation(ctx, voiceRecorder) {
    var state = ctx.state;
    if (state.transcribingVoice) return;
    if (state.recordingVoice) {
      state.recordingVoice = false;
      state.transcribingVoice = true;
      ctx.updateComposerButtons();
      var chunks = voiceRecorder.chunks.slice();
      var sr = (voiceRecorder.audioCtx && voiceRecorder.audioCtx.sampleRate) || 16000;
      try {
        if (voiceRecorder.processor) voiceRecorder.processor.disconnect();
        if (voiceRecorder.source) voiceRecorder.source.disconnect();
      } catch (e) {}
      try {
        if (voiceRecorder.stream) voiceRecorder.stream.getTracks().forEach(function (t) { t.stop(); });
        if (voiceRecorder.audioCtx) voiceRecorder.audioCtx.close();
      } catch (e) {}
      voiceRecorder.stream = null;
      voiceRecorder.audioCtx = null;
      voiceRecorder.source = null;
      voiceRecorder.processor = null;
      voiceRecorder.chunks = [];
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
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
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
    ctx.renderSheets();
    if (state.studioMode !== "create") {
      ctx.apiCall("/local/prompt-preview", "POST", { bot_id: bot.id }).then(function (pv) {
        if (pv && pv.ok) {
          state.promptPreviewData = pv;
          if (state.studioOpen) ctx.renderSheets();
        }
      });
    }
    ctx.apiCall("/local/models", "POST", {}).then(function (r) {
      if (r && r.ok === false && r.error) ctx.showToast("获取模型列表失败: " + r.error);
      if (r && Array.isArray(r.models)) {
        state.availableModels = r.models;
        if (state.studioOpen) ctx.renderSheets();
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
    ctx.apiCall("/local/vault", "POST", { op: "read", path: clean }).then(function (r) {
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

  function showTopLayerModalDialog(dlg) {
    if (!dlg) return;
    var pointerDownOnBackdrop = false;
    var removed = false;
    var cleanup = function () {
      if (removed) return;
      removed = true;
      if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
    };
    var startAnimatedClose = function () {
      if (removed || dlg.hasAttribute("data-closing")) return;
      if (!dlg.open) { cleanup(); return; }
      if (supportsTopLayerExit()) {
        dlg.close();
        var timer = setTimeout(cleanup, 260);
        dlg.addEventListener("transitionend", function onEnd(ev) {
          if (ev.target === dlg) {
            clearTimeout(timer);
            dlg.removeEventListener("transitionend", onEnd);
            cleanup();
          }
        });
        return;
      }
      dlg.setAttribute("data-closing", "");
      var fallbackDone = false;
      var finishClose = function () {
        if (fallbackDone) return;
        fallbackDone = true;
        dlg.removeAttribute("data-closing");
        if (dlg.open) dlg.close();
        cleanup();
      };
      dlg.addEventListener("transitionend", function onEndFallback(ev) {
        if (ev.target === dlg) {
          dlg.removeEventListener("transitionend", onEndFallback);
          finishClose();
        }
      });
      setTimeout(finishClose, 260);
    };
    dlg.closeAnimated = startAnimatedClose;

    if (!("closedBy" in HTMLDialogElement.prototype)) {
      dlg.addEventListener("pointerdown", function (ev) {
        pointerDownOnBackdrop = ev.target === dlg;
      });
      dlg.addEventListener("pointerup", function (ev) {
        if (pointerDownOnBackdrop && ev.target === dlg) startAnimatedClose();
        pointerDownOnBackdrop = false;
      });
    }
    dlg.addEventListener("cancel", function (ev) {
      if (!supportsTopLayerExit()) {
        ev.preventDefault();
        startAnimatedClose();
      }
    });
    dlg.addEventListener("close", function () {
      if (dlg.hasAttribute("data-closing")) return;
      setTimeout(cleanup, supportsTopLayerExit() ? 220 : 0);
    });
    dlg.addEventListener("click", function (ev) {
      if (ev.target && ev.target.closest && ev.target.closest("[data-dlg-close]")) {
        startAnimatedClose();
      }
    });
    document.body.appendChild(dlg);
    dlg.showModal();
  }

  function openLightbox(escapeHtmlOrCtx, src) {
    if (!src) return;
    var esc = typeof escapeHtmlOrCtx === "function"
      ? escapeHtmlOrCtx
      : function (s) { return String(s === null || s === undefined ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); };
    var dlg = document.createElement("dialog");
    dlg.className = "dsb-modal-dialog";
    dlg.setAttribute("closedby", "any");
    dlg.setAttribute("aria-label", "图片大图预览");
    dlg.innerHTML =
      '<div class="dsb-lightbox-stage">' +
      '<img src="' + esc(src) + '" alt="预览大图"/>' +
      '<div class="dsb-lightbox-bar">' +
      '<a class="dsb-btn-sec" href="' + esc(src) + '" target="_blank" rel="noopener noreferrer" style="text-decoration:none;display:inline-flex;align-items:center;">原图打开</a>' +
      '<button type="button" class="dsb-btn-pri" data-dlg-close="1">关闭</button>' +
      '</div></div>';
    showTopLayerModalDialog(dlg);
  }

  function triggerFileDownload(downloadUrl, fileName) {
    if (!downloadUrl) return;
    var a = document.createElement("a");
    a.href = downloadUrl;
    if (fileName) a.download = fileName;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { if (a.parentNode) a.parentNode.removeChild(a); }, 400);
  }

  function openFilePreview(escapeHtml, previewUrl, fileName, ext, downloadUrl) {
    if (!previewUrl) return;
    var isPdfOrText = /^(PDF|TXT|MD|CSV|JSON|HTML)$/i.test(String(ext || ""));
    if (!isPdfOrText) {
      triggerFileDownload(downloadUrl || previewUrl, fileName);
      return;
    }
    var esc = typeof escapeHtml === "function"
      ? escapeHtml
      : function (s) { return String(s === null || s === undefined ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); };
    var dlg = document.createElement("dialog");
    dlg.className = "dsb-modal-dialog";
    dlg.setAttribute("closedby", "any");
    dlg.setAttribute("aria-label", fileName || "文档预览");
    dlg.innerHTML =
      '<div class="dsb-file-preview-box">' +
      '<div class="dsb-file-preview-head">' +
      '<span class="dsb-file-preview-title">' + esc(fileName || "文档预览") + '</span>' +
      '<div class="dsb-file-preview-actions">' +
      '<a class="dsb-btn-sec" href="' + esc(previewUrl) + '" target="_blank" rel="noopener noreferrer" style="text-decoration:none;display:inline-flex;align-items:center;">新窗口打开</a>' +
      '<button type="button" class="dsb-btn-pri" data-fp-dl="1">下载</button>' +
      '<button type="button" class="dsb-btn-sec" data-dlg-close="1">关闭</button>' +
      '</div></div>' +
      '<iframe class="dsb-file-preview-frame" src="' + esc(previewUrl) + '" title="' + esc(fileName || "PDF 预览") + '"></iframe>' +
      '</div>';
    dlg.addEventListener("click", function (ev) {
      if (ev.target && ev.target.closest && ev.target.closest("[data-fp-dl]")) {
        triggerFileDownload(downloadUrl || previewUrl, fileName);
      }
    });
    showTopLayerModalDialog(dlg);
  }

  function applySyncPayload(ctx, res) {
    if (!res || !res.ok) return;
    var state = ctx.state;
    if (Array.isArray(res.bots) && res.bots.length) state.bots = res.bots;
    if (Array.isArray(res.messages)) state.messages = res.messages;
    if (res.messages_epoch) state.messagesEpoch = Number(res.messages_epoch) || 1;
    if (res.streams_by_bot && typeof res.streams_by_bot === "object") state.streamsByBot = res.streams_by_bot;
    if (res.bot_context_by_id && typeof res.bot_context_by_id === "object") state.botContextById = res.bot_context_by_id;
    if (typeof res.vps_online === "boolean") state.vpsOnline = res.vps_online;
    if (typeof res.rtt_ms === "number") state.rttMs = res.rtt_ms;
    if (typeof res.phone_online === "boolean") state.phoneOnline = res.phone_online;
    var pins = res.pins || res.pinned_memories;
    if (Array.isArray(pins)) state.vaultPinned = pins;
    ctx.renderRightLauncher();
    ctx.renderHomeRail();
    ctx.renderHeaderAndPins();
    ctx.updateComposerButtons();
  }

  function syncStudioDraftFromInputs(state) {
    var n = document.getElementById("dsb-st-name");
    var d = document.getElementById("dsb-st-desc");
    var m = document.getElementById("dsb-st-model");
    var p = document.getElementById("dsb-st-persona");
    var c = document.getElementById("dsb-st-curiosity");
    if (n) state.editBotName = n.value;
    if (d) state.editBotDesc = d.value;
    if (m) state.editBotModel = m.value;
    if (p) state.editBotPersona = p.value;
    if (c) state.editBotCuriosity = Boolean(c.checked);
  }

  function renderHomeRail(ctx) {
    var state = ctx.state;
    var listEl = document.getElementById("dsb-contact-list");
    var connEl = document.getElementById("dsb-conn-pill");
    if (connEl) {
      var rttTxt = state.vpsOnline ? (state.rttMs > 0 ? state.rttMs + "ms" : "已连接") : "重连中";
      connEl.innerHTML = '<span class="dsb-conn-dot' + (state.vpsOnline ? "" : " is-off") + '"></span><span>' + ctx.escapeHtml(rttTxt + (state.phoneOnline ? " · 手机在线" : "")) + "</span>";
    }
    var fBtns = document.querySelectorAll("[data-bot-filter]");
    for (var f = 0; f < fBtns.length; f++) {
      fBtns[f].classList.toggle("is-active", fBtns[f].getAttribute("data-bot-filter") === state.botFilter);
    }
    if (!listEl) return;
    var q = state.botSearch.trim().toLowerCase();
    var html = state.bots.filter(function (b) {
      var isRun = Boolean(state.streamsByBot[b.id] || state.sendingByBot[b.id]);
      if (state.botFilter === "running" && !isRun) return false;
      if (state.botFilter === "pinned" && !(b.pinned || b.primary || b.id === "pc_engineer")) return false;
      if (q && String(b.name || "").toLowerCase().indexOf(q) === -1 && String(b.description || "").toLowerCase().indexOf(q) === -1) return false;
      return true;
    }).map(function (b) {
      var roomMsgs = ctx.getRoomMessages(b.id);
      var lastM = roomMsgs.length ? roomMsgs[roomMsgs.length - 1] : null;
      var liveSt = state.streamsByBot[b.id];
      var isRun = Boolean(liveSt || state.sendingByBot[b.id]);
      var sub = isRun
        ? (liveSt && liveSt.tool ? "正在执行 · " + ctx.friendlyToolLabel(liveSt.tool, {}) : "正在思考…")
        : (lastM ? String(lastM.text || "").replace(/\s+/g, " ").slice(0, 42) : (b.description || "点击开始独立对话"));
      var timeStr = lastM && lastM.time ? String(lastM.time).slice(0, 5) : "";
      return '<button type="button" class="dsb-contact-item' + (b.id === state.activeBotId ? " is-active" : "") + '" data-select-bot="' + ctx.escapeHtml(b.id) + '">' +
        ctx.renderBotAvatarSvg(b, 38) +
        '<div class="dsb-contact-body"><div class="dsb-contact-r1"><span class="dsb-contact-name">' + ctx.escapeHtml(b.name) +
        ((b.primary || b.id === "pc_engineer") ? '<span class="dsb-role-badge">主控</span>' : "") +
        '</span><span class="dsb-contact-time">' + ctx.escapeHtml(timeStr) + '</span></div>' +
        '<div class="dsb-contact-sub' + (isRun ? " is-live" : "") + '">' + ctx.escapeHtml(sub) + "</div></div></button>";
    }).join("");
    listEl.innerHTML = html || '<div style="padding:24px 12px;text-align:center;color:var(--dsb-faint);font-size:12px;">暂无匹配的助理</div>';
  }

  window.__DSB_CLIENT_UI__ = {
    uploadFileObject: uploadFileObject,
    retryAttachment: retryAttachment,
    removePendingAttachment: removePendingAttachment,
    revokeItemThumb: revokeItemThumb,
    encodeMonoWav: encodeMonoWav,
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
    renderHomeRail: renderHomeRail,
  };
})();
