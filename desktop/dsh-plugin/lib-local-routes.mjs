/**
 * [Module Responsibility]
 * DSH DSB 插件本地 HTTP 路由、Host IPC 服务与 18195 网关管理 (`lib-local-routes.mjs`)。
 * 从 `index.js` 拆出，负责 `/local/*` 本地接口处理、Host-Sentinel 请求转发、
 * `/proxy-media` 媒体代理、IPC/Loopback 服务器启停与自愈重绑。
 */
import fs from "node:fs";
import path from "node:path";
import http from "node:http";
import { fileURLToPath } from "node:url";
import {
  PC_DEVICE_ID,
  DSH_HOST_IPC_PORT,
  runtimeState,
  loadLocalConfig,
  getService,
  dedupeAndNormalizeBots,
  loadBotsRosterFromDisk,
  saveBotsRosterToDisk,
  findBotByIdOrName,
  resolveCloudBotId,
  mergeCloudPcMessagesIntoCache,
  loadLocalChatCache,
  saveLocalChatCache,
  cloudFetch,
  executeCloudChatTurn,
  switchCloudMessageVariant,
  handleBotArchiveLocalRoute,
  applyCloudSyncPayload,
} from "./lib-chat-cache.mjs";
import { callCuaTool } from "./cua-bridge.mjs";
import { handleLocalUploadRpc, handleLocalFilesRpc } from "./lib-file-bridge.mjs";
import {
  netTelemetry,
  logSentinel,
  getRecentSentinelLogs,
  getSentinelLogPath,
  triggerInstantReconnect,
} from "./lib-sentinel-net.mjs";
import {
  getDshPanorama,
  searchDshSessions,
  inspectDshSessionDetail,
  dispatchToDshSession,
  proxyToDshHostIpc,
  runLocalPcShellCommand,
  runLocalPcFsOperation,
} from "./lib-dsh-sessions.mjs";
import { startNativePcBridge } from "./index.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PROCESS_STARTED_AT = Date.now();

const HOST_ONLY_LOCAL_PATHS = new Set([
  "/local/speech-to-text",
  "/local/cua",
  "/local/sessions",
  "/sessions",
  "/local/infinite-chat/inspect_session",
  "/local/infinite-chat/dispatch_session",
  "/local/infinite-chat/search_sessions",
]);

let localHttpServer = null;
let dshHostIpcServer = null;
let localLoopbackRetryTimer = null;
let localHttpServerBinding = false;

export function isPrimaryGatewayListening() {
  return Boolean(localHttpServer && localHttpServer.listening);
}

export function isLocalHttpServerBound() {
  return Boolean(localHttpServer);
}

export async function handleLocalRequest(localPath, method = "GET", body = null) {
  const clean = String(localPath || "").split("?")[0];

  if (clean === "/local/ui-assets" || clean === "/local/whalechan-avatar") {
    let css = "";
    let renderJs = "";
    let clientUiJs = "";
    let dataUrl = "";
    try { css = fs.readFileSync(path.join(__dirname, "assets", "dsb-ui.css"), "utf8"); } catch {}
    try { renderJs = fs.readFileSync(path.join(__dirname, "assets", "dsb-render.js"), "utf8"); } catch {}
    try { clientUiJs = fs.readFileSync(path.join(__dirname, "assets", "client-ui.js"), "utf8"); } catch {}
    try {
      const jpgP = path.join(__dirname, "assets", "whale_avatar.jpg");
      const pngP = path.join(__dirname, "assets", "whalechan_avatar.png");
      if (fs.existsSync(jpgP)) dataUrl = `data:image/jpeg;base64,${fs.readFileSync(jpgP).toString("base64")}`;
      else if (fs.existsSync(pngP)) dataUrl = `data:image/png;base64,${fs.readFileSync(pngP).toString("base64")}`;
    } catch {}
    return { ok: true, css, render_js: renderJs, client_ui_js: clientUiJs, data_url: dataUrl };
  }

  if (clean === "/local/panorama" || clean === "/local/bot-sessions" || clean.startsWith("/local/bots")) {
    const subOp = clean.startsWith("/local/bots/") ? clean.slice("/local/bots/".length) : "";
    const op = String(subOp || body?.op || body?.action || "list").trim().toLowerCase();
    if (op === "export" || op === "import") return await handleBotArchiveLocalRoute(op, body || {});
    let roster = loadBotsRosterFromDisk();
    let cloudWarn = "";
    if ((op === "switch" || op === "select") && body?.bot_id) {
      const target = findBotByIdOrName(body.bot_id);
      if (target) { runtimeState.activeBotId = target.id; saveBotsRosterToDisk(); }
    } else if ((op === "delete" || op === "delete_bot" || op === "remove") && body?.bot_id) {
      const target = findBotByIdOrName(body.bot_id);
      if (!target || target.primary || target.id === "pc_engineer") return { ok: false, error: "不能删除电脑端主控助理" };
      const cloudBotId = resolveCloudBotId(target.id);
      runtimeState.botsRoster = roster.filter((b) => b.id !== target.id && b.id !== String(body.bot_id).trim());
      if (runtimeState.activeBotId === target.id || !runtimeState.botsRoster.some((b) => b.id === runtimeState.activeBotId)) {
        runtimeState.activeBotId = runtimeState.botsRoster[0]?.id || "pc_engineer";
      }
      saveBotsRosterToDisk();
      const delRes = await cloudFetch(`/api/bots/${encodeURIComponent(cloudBotId)}?source=pc`, "DELETE", null, 15000).catch((e) => {
        cloudWarn = String(e?.message || e);
        return null;
      });
      if (Array.isArray(delRes?.bots) && delRes.bots.length > 0) {
        runtimeState.botsRoster = dedupeAndNormalizeBots(delRes.bots);
        saveBotsRosterToDisk();
      }
    } else if ((op === "create_bot" || op === "create") && body?.name) {
      const rawName = String(body.name).trim();
      const existing = roster.find((b) => b.name.toLowerCase() === rawName.toLowerCase());
      if (existing) {
        runtimeState.activeBotId = existing.id; saveBotsRosterToDisk();
      } else {
        const personaStr = String(body.persona || body.instructions || body.system_prompt || `你是「${rawName}」，认真协助用户完成任务。`).trim();
        const descStr = String(body.description || `${rawName} 电脑端助手`).trim();
        const colorStr = String(body.color || body.avatar_color || "#0A84FF").trim();
        const shapeStr = String(body.shape || "circle").trim();
        const modelStr = String(body.model || "").trim();
        const curiosityVal = Boolean(body.curiosity_enabled);
        const cloudCreated = await cloudFetch("/api/bots", "POST", {
          name: rawName, title: "", description: descStr, instructions: personaStr, color: colorStr,
          shape: shapeStr, model: modelStr, curiosity_enabled: curiosityVal, engine: "big", notify: true, source: "pc", home_node: "pc",
        }, 15000).catch((e) => {
          cloudWarn = String(e?.message || e);
          return null;
        });
        const createdId = String(cloudCreated?.id || body.id || "").trim().replace(/[^a-zA-Z0-9_-]/g, "") || `bot_${Date.now().toString(36)}`;
        const newBot = {
          id: createdId, name: rawName, description: descStr, color: colorStr, shape: shapeStr,
          model: modelStr, curiosity_enabled: curiosityVal, primary: false, home_node: "pc", persona: personaStr, instructions: personaStr,
        };
        roster.push(newBot);
        runtimeState.botsRoster = dedupeAndNormalizeBots(roster);
        runtimeState.activeBotId = newBot.id; saveBotsRosterToDisk();
      }
    } else if (op === "update" || op === "update_bot" || op === "edit") {
      const targetId = String(body?.bot_id || body?.id || runtimeState.activeBotId || "pc_engineer").trim();
      const target = findBotByIdOrName(targetId);
      if (target) {
        const nextName = String(body?.name || target.name || target.id).trim();
        const nextDesc = body?.description !== undefined ? String(body.description).trim() : String(target.description || `${nextName} 跨端助手`);
        const nextColor = String(body?.color || target.color || "#0A84FF").trim();
        const nextShape = String(body?.shape || target.shape || "flower").trim();
        const nextPersona = String(body?.persona ?? body?.instructions ?? target.persona ?? target.instructions ?? "").trim();
        const nextModel = body?.model !== undefined ? String(body.model).trim() : String(target.model || "");
        const nextCuriosity = body?.curiosity_enabled !== undefined ? Boolean(body.curiosity_enabled) : Boolean(target.curiosity_enabled);
        const nextNotify = body?.notify !== undefined ? Boolean(body.notify) : Boolean(target.notify ?? true);
        target.name = nextName;
        target.description = nextDesc;
        target.color = nextColor;
        target.shape = nextShape;
        target.model = nextModel;
        target.curiosity_enabled = nextCuriosity;
        target.notify = nextNotify;
        if (nextPersona) {
          target.persona = nextPersona;
          target.instructions = nextPersona;
        }
        runtimeState.botsRoster = dedupeAndNormalizeBots(roster);
        saveBotsRosterToDisk();
        const cloudBotId = resolveCloudBotId(target.id);
        const putPayload = {
          id: cloudBotId,
          name: nextName,
          description: nextDesc,
          instructions: target.instructions || nextPersona || `你是「${nextName}」`,
          color: nextColor,
          shape: nextShape,
          model: String(target.model || ""),
          curiosity_enabled: Boolean(target.curiosity_enabled),
          notify: Boolean(target.notify ?? true),
          source: "pc",
        };
        await Promise.all([
          cloudFetch(`/api/bots/${encodeURIComponent(cloudBotId)}`, "PUT", putPayload, 12000).catch((e) => { cloudWarn = String(e?.message || e); return null; }),
          cloudFetch(`/api/bots/${encodeURIComponent(cloudBotId)}/identity`, "PATCH", {
            name: nextName,
            color: nextColor,
            shape: nextShape,
            persona: target.instructions || nextPersona,
            instructions: target.instructions || nextPersona,
            source: "pc",
          }, 12000).catch((e) => { cloudWarn = cloudWarn || String(e?.message || e); return null; }),
        ]);
      }
    }
    const panorama = getDshPanorama(runtimeState.activeCtx, Boolean(body?.force));
    const currentRoster = loadBotsRosterFromDisk();
    return {
      ok: true,
      ...(cloudWarn ? { cloud_warning: cloudWarn } : {}),
      bots: currentRoster,
      bot: findBotByIdOrName(runtimeState.activeBotId),
      active_bot_id: runtimeState.activeBotId,
      messages_epoch: runtimeState.messagesEpoch,
      panorama,
      phone_online: runtimeState.phoneOnline,
      phone_last_3_dialogs: runtimeState.phoneLast3Dialogs || [],
      shared_memories: runtimeState.sharedMemories,
    };
  }

  if (clean.startsWith("/local/infinite-chat")) {
    const op = String(clean.replace(/^\/local\/infinite-chat\/?/, "") || body?.op || "sync").trim().toLowerCase();

    if (op === "sync" || op === "history") {
      const nowMs = Date.now();
      const hasInflight = runtimeState.inflightControllersByBot.size > 0;
      if (Boolean(body?.force) || !runtimeState.lastPanoramaCache || (nowMs - (runtimeState.lastPanoramaAt || 0) > 12000 && !hasInflight)) {
        runtimeState.lastPanoramaCache = getDshPanorama(runtimeState.activeCtx, Boolean(body?.force));
        runtimeState.lastPanoramaAt = nowMs;
      }
      const syncIntervalMs = hasInflight ? 800 : 2500;
      if (Boolean(body?.force) || nowMs - (runtimeState.lastBgCloudSyncAt || 0) > syncIntervalMs) {
        runtimeState.lastBgCloudSyncAt = nowMs;
        const bVer = Boolean(body?.force) ? "" : String(runtimeState.cloudBotsVer || "");
        const pVer = Boolean(body?.force) ? "" : String(runtimeState.cloudPinsVer || "");
        const cVer = Boolean(body?.force) ? "" : String(runtimeState.cloudConfigVer || "");
        const syncPath = `/api/sync?since_seq=0&source=pc&bots_ver=${encodeURIComponent(bVer)}&pins_ver=${encodeURIComponent(pVer)}&config_ver=${encodeURIComponent(cVer)}`;
        Promise.all([
          cloudFetch(syncPath, "GET", null, 8000).catch(() => null),
          cloudFetch("/api/dsh/status", "GET", null, 8000).catch(() => null),
        ]).then(([syncRes, dshSt]) => {
          if (dshSt?.ok && typeof dshSt.phone_online === "boolean") runtimeState.phoneOnline = dshSt.phone_online;
          if (syncRes) applyCloudSyncPayload(syncRes);
        }).catch(() => {});
      }
      return {
        ok: true,
        messages: loadLocalChatCache(),
        messages_epoch: runtimeState.messagesEpoch,
        streams_by_bot: runtimeState.liveStreamsByBot,
        bot_context_by_id: runtimeState.botContextById || {},
        panorama: runtimeState.lastPanoramaCache,
        bots: loadBotsRosterFromDisk(),
        active_bot_id: runtimeState.activeBotId,
        vps_online: Boolean(runtimeState.vpsOnline),
        rtt_ms: Number(netTelemetry.lastRttMs || 0),
        route: String(netTelemetry.activeRoute || "direct"),
        phone_online: runtimeState.phoneOnline,
        phone_last_3_dialogs: runtimeState.phoneLast3Dialogs || [],
        pinned_memories: Array.isArray(runtimeState.pinnedMemories) ? runtimeState.pinnedMemories : [],
        pins: Array.isArray(runtimeState.pinnedMemories) ? runtimeState.pinnedMemories : [],
        shared_memories: runtimeState.sharedMemories,
      };
    }

    if (op === "compact") {
      const bot = findBotByIdOrName(body?.bot_id || runtimeState.activeBotId || "pc_engineer");
      const cloudBotId = resolveCloudBotId(bot.id);
      const compRes = await cloudFetch("/api/context/compact", "POST", { bot_id: cloudBotId, source: "pc" }, 90000).catch((e) => ({ ok: false, error: String(e?.message || e) }));
      if (compRes?.context_by_bot && typeof compRes.context_by_bot === "object") runtimeState.botContextById = { ...runtimeState.botContextById, ...compRes.context_by_bot };
      if (compRes?.marker && typeof compRes.marker === "object") mergeCloudPcMessagesIntoCache([compRes.marker]);
      return { ok: Boolean(compRes?.ok ?? true), ...compRes, messages: loadLocalChatCache(), bot_context_by_id: runtimeState.botContextById || {} };
    }

    if (op === "send" || op === "edit" || op === "reroll") return await executeCloudChatTurn(op, body || {});
    if (op === "variant") return await switchCloudMessageVariant(body || {});

    if (op === "stop") {
      const bot = findBotByIdOrName(body?.bot_id || runtimeState.activeBotId || "pc_engineer");
      const cloudBotId = resolveCloudBotId(bot.id);
      try { runtimeState.inflightControllersByBot.get(bot.id)?.abort(); } catch {}
      runtimeState.inflightControllersByBot.delete(bot.id);
      runtimeState.abortedEpochByBot.set(bot.id, (runtimeState.abortedEpochByBot.get(bot.id) || 0) + 1);
      const stopRes = await cloudFetch("/api/stop", "POST", { bot_id: cloudBotId, source: "pc" }, 10000).catch(() => null);
      saveLocalChatCache(loadLocalChatCache(), true, true);
      return { ok: true, stopped: true, bot_id: bot.id, messages: loadLocalChatCache(), messages_epoch: runtimeState.messagesEpoch, cloud_stop: stopRes };
    }

    if (op === "pin") {
      const pinRes = await cloudFetch("/api/memories/pins", "POST", {
        action: body?.action || "add", text: body?.text || "", pin_id: body?.pin_id || null, category: body?.category || "general", source: "pc",
      }, 15000).catch((e) => ({ ok: false, error: String(e?.message || e) }));
      return { ok: Boolean(pinRes?.ok || pinRes?.pins), ...pinRes };
    }

    if (op === "confirm") {
      const confRes = await cloudFetch("/api/confirm", "POST", { id: String(body?.id || "").trim(), accepted: Boolean(body?.accepted) }, 15000).catch((e) => ({ ok: false, error: String(e?.message || e) }));
      return { ok: Boolean(confRes?.ok), ...confRes };
    }

    if (op === "search_sessions") return searchDshSessions(runtimeState.activeCtx, body?.query, body?.status_filter);
    if (op === "inspect_session") return await inspectDshSessionDetail(runtimeState.activeCtx, body?.session_id || body?.query);
    if (op === "dispatch_session") return await dispatchToDshSession(runtimeState.activeCtx, body || {});
    if (op === "pc_shell") return await runLocalPcShellCommand(body || {});
    if (op === "pc_fs") return await runLocalPcFsOperation(body || {});

    if (op === "peek_phone") {
      const limit = Math.max(1, Math.min(50, Number(body?.limit) || 5));
      const st = await cloudFetch(`/api/dsh/status?limit=${limit}&source=phone`, "GET", null, 15000).catch(() => null);
      const dialogs = st?.dialogs || st?.phone_dialogs || st?.phone_last_3_dialogs || runtimeState.phoneLast3Dialogs || [];
      if (typeof st?.phone_online === "boolean") runtimeState.phoneOnline = st.phone_online;
      return { ok: Boolean(st?.ok), phone_online: runtimeState.phoneOnline, dialogs };
    }

    if (op === "message_phone") {
      const message = String(body?.message || body?.text || "").trim();
      if (!message) return { ok: false, error: "发送给手机/平板端的消息不能为空" };
      const targetBot = String(body?.bot_id || "companion").trim();
      const res = await cloudFetch("/api/blackboard", "POST", {
        action: "post", title: message.slice(0, 48), content: `@${targetBot} ${message}`,
        mentions: [targetBot], from_bot_id: "pc_engineer", from_bot_name: "电脑端 DSH", from_source: "pc",
      }, 15000).catch((e) => ({ ok: false, error: String(e?.message || e) }));
      return { ok: Boolean(res?.ok), reply: res?.ok ? `已在黑板异步 @${targetBot}` : "", error: res?.error || null };
    }
  }

  if (clean === "/local/speech-to-text") {
    const stt = getService(runtimeState.activeCtx, "speechToText") || getService(runtimeState.rootCtx, "speechToText");
    if (!stt) {
      if (!runtimeState.activeCtx) {
        const proxied = await proxyToDshHostIpc("/local/speech-to-text", body || {}, 35000);
        if (proxied && typeof proxied === "object") return proxied;
      }
      return { ok: false, error: "官方 SenseVoice 语音识别服务未加载，请重启 DeepSeek Harness 以激活语音插件" };
    }
    const op = String(body?.op || "transcribe").trim().toLowerCase();
    const snap = typeof stt.snapshot === "function" ? stt.snapshot() : {};
    const prov = Array.isArray(snap?.providers) ? snap.providers[0] : null;
    const providerId = prov?.id || snap?.selection?.providerId || "sensevoice-local";
    if (op === "status" || op === "prepare") {
      if (op === "prepare" && prov?.preparation?.phase !== "ready" && typeof stt.prepare === "function") {
        try { stt.prepare(providerId, {}); } catch {}
      }
      return { ok: true, providerId, phase: prov?.preparation?.phase || "ready", snapshot: snap };
    }
    if (prov?.preparation?.phase && prov.preparation.phase !== "ready") {
      if (typeof stt.prepare === "function") {
        try { stt.prepare(providerId, {}); } catch {}
      }
      return { ok: false, preparing: true, phase: prov.preparation.phase, error: `官方 SenseVoice 语音模型正在准备中 (${prov.preparation.phase})，请稍候几秒再试` };
    }
    const audioBase64 = String(body?.audioBase64 || body?.audio_b64 || "").trim();
    if (!audioBase64) return { ok: false, error: "未收到录音数据" };
    try {
      const audioBuf = Buffer.from(audioBase64, "base64");
      const spec = stt.resolve({ audio: audioBuf, providerId, language: body?.language || "auto" });
      const result = await stt.transcribe(spec, AbortSignal.timeout(30000));
      return { ok: true, text: String(result?.text || result || "").trim(), result };
    } catch (err) {
      return { ok: false, error: String(err?.message || err) };
    }
  }

  if (clean === "/local/prompt-preview") {
    const botId = String(body?.bot_id || runtimeState.activeBotId || "companion").trim();
    const res = await cloudFetch(
      `/api/system/prompt_preview?bot_id=${encodeURIComponent(botId)}&source=pc`,
      "GET",
      null,
      15000
    ).catch((e) => ({ ok: false, error: String(e?.message || e) }));
    return { ok: Boolean(res?.ok), ...res };
  }

  if (clean === "/local/vault") {
    const op = String(body?.op || body?.action || (method === "PUT" ? "write" : "list")).trim().toLowerCase();
    if (op === "read" && body?.path) {
      const rel = String(body.path).replace(/^memory_vault\//, "").replace(/^\/+/, "");
      const r = await cloudFetch(`/api/vault?path=${encodeURIComponent(rel)}`, "GET", null, 15000).catch((e) => ({ ok: false, error: String(e?.message || e) }));
      return { ok: Boolean(r && typeof r.content === "string"), path: rel, content: r?.content ?? "", error: r?.error || r?.detail };
    }
    if ((op === "write" || op === "save") && body?.path) {
      const rel = String(body.path).replace(/^memory_vault\//, "").replace(/^\/+/, "");
      const r = await cloudFetch("/api/vault", "PUT", { path: rel, content: String(body.content ?? "") }, 15000).catch((e) => ({ ok: false, error: String(e?.message || e) }));
      return { ok: Boolean(r?.saved || r?.ok), saved: Boolean(r?.saved || r?.ok), path: rel, error: r?.error || r?.detail };
    }
    if (op === "pin" || op === "unpin" || op === "add_pin" || op === "remove_pin") {
      const act = (op === "unpin" || op === "remove_pin") ? "unpin" : "pin";
      const pinId = String(body?.pin_id || body?.id || "").trim();
      const r = await cloudFetch("/api/memories/pins", "POST", { action: act, text: body?.text || "", pin_id: pinId, source: "pc" }, 15000).catch((e) => ({ ok: false, error: String(e?.message || e) }));
      const nextPins = Array.isArray(r?.pins) ? r.pins : [];
      if (Array.isArray(r?.pins)) runtimeState.pinnedMemories = nextPins;
      return { ok: Boolean(r?.ok || Array.isArray(r?.pins)), pins: nextPins, pinned_memories: nextPins, error: r?.error || r?.detail };
    }
    const [vRes, pRes] = await Promise.all([
      cloudFetch("/api/vault", "GET", null, 15000).catch(() => null),
      cloudFetch("/api/memories/pins", "GET", null, 10000).catch(() => null),
    ]);
    const listPins = Array.isArray(pRes?.pins) ? pRes.pins : (Array.isArray(runtimeState.pinnedMemories) ? runtimeState.pinnedMemories : []);
    runtimeState.pinnedMemories = listPins;
    return {
      ok: true,
      files: Array.isArray(vRes?.files) ? vRes.files : [],
      pins: listPins,
      pinned_memories: listPins,
    };
  }

  if (clean === "/local/upload") {
    return await handleLocalUploadRpc(body, cloudFetch, logSentinel);
  }

  if (clean === "/local/files" || clean === "/local/files/sync") {
    return await handleLocalFilesRpc(clean, body, cloudFetch);
  }

  if (clean === "/local/models") {
    const r = await cloudFetch("/api/models", "POST", body || {}, 15000).catch((e) => ({ ok: false, error: String(e?.message || e) }));
    if (r && r.ok === false && r.error) return { ok: false, models: [], error: r.error };
    return { ok: true, models: Array.isArray(r?.models) ? r.models : [], base_url: r?.base_url || "" };
  }

  if (clean === "/local/sessions" || clean === "/sessions") {
    const p = getDshPanorama(runtimeState.activeCtx, true);
    return { ok: true, sessions: p.sessions, panorama: p };
  }

  if (clean === "/local/pc-shell") {
    return await runLocalPcShellCommand(body || {});
  }

  if (clean === "/local/pc-fs") {
    return await runLocalPcFsOperation(body || {});
  }

  if (clean === "/local/cua") {
    return await callCuaTool(body?.action || body?.tool || "list_windows", body || {}, body?.bot_name || "主助理");
  }

  if (clean === "/cloud/shared-memory") {
    const res = await cloudFetch("/api/dsh/shared-memory", method || "POST", { source: "pc", ...(body || {}) }, 25000);
    if (res?.shared_memories) runtimeState.sharedMemories = { ...runtimeState.sharedMemories, ...res.shared_memories };
    return res;
  }

  return null;
}

export async function forwardRequest(localPath, cloudPath, method = "GET", body = null) {
  const cleanLocal = String(localPath || "").split("?")[0];
  if (runtimeState.activeCtx && !localHttpServer && (!cleanLocal || !HOST_ONLY_LOCAL_PATHS.has(cleanLocal))) {
    try {
      const port = Number(loadLocalConfig().local_port || 18195);
      const gwRes = await fetch(`http://127.0.0.1:${port}/api/dsh-deepseek-claw`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Connection: "close" },
        body: JSON.stringify({ localPath, cloudPath, method, body }),
        signal: AbortSignal.timeout(175000),
      });
      if (gwRes.ok) return await gwRes.json();
    } catch {}
  }
  if (localPath) {
    const handled = await handleLocalRequest(localPath, method, body);
    if (handled !== null) return handled;
  }
  if (!cloudPath) return { ok: false, error: "未指定请求路径" };
  try { return await cloudFetch(cloudPath, method, body, 175000); } catch (err) { return { ok: false, error: String(err?.message || err) }; }
}

export function createBridgeHttpHandler() {
  return (req, res) => {
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
    res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");
    res.setHeader("Connection", "close");
    if (req.method === "OPTIONS") { res.writeHead(204); res.end(); return; }
    if (req.method === "GET" && (req.url === "/health" || req.url === "/status")) {
      const cfg = loadLocalConfig();
      const hbAgeSec = runtimeState.lastHeartbeatOkMs > 0
        ? Math.max(0, Math.round((Date.now() - runtimeState.lastHeartbeatOkMs) / 1000))
        : -1;
      const effectiveVpsOnline = Boolean(runtimeState.vpsOnline && hbAgeSec >= 0 && hbAgeSec <= 45);
      const pan = runtimeState.lastPanoramaCache || {};
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({
        ok: true,
        pid: process.pid,
        uptime_sec: Math.round((Date.now() - PROCESS_STARTED_AT) / 1000),
        device_id: PC_DEVICE_ID,
        device_name: runtimeState.deviceName || "Windows-PC",
        server_url: cfg.server_url || "",
        active_route: netTelemetry.activeRoute,
        proxy_alive: Boolean(netTelemetry.proxyAlive),
        last_rtt_ms: Number(netTelemetry.lastRttMs || 0),
        last_error: String(netTelemetry.lastError || ""),
        has_dsh_ctx: Boolean(runtimeState.activeCtx),
        dsh_host_alive: Boolean(runtimeState.activeCtx || runtimeState.dshHostAlive),
        sessions_count: Number(pan.sessions_count ?? pan.total_sessions ?? 0),
        running_sessions_count: Number(pan.running_count || 0),
        bridge_running: Boolean(runtimeState.bridgeRunning),
        vps_online: effectiveVpsOnline,
        last_heartbeat_ok_age_sec: hbAgeSec,
        phone_online: Boolean(runtimeState.phoneOnline),
        heartbeat_count: Number(runtimeState.heartbeatCount || 0),
        last_heartbeat_at: String(runtimeState.lastHeartbeatAt || "等待握手"),
        bytes_sent: Number(runtimeState.bytesSent || 0),
        bytes_received: Number(runtimeState.bytesReceived || 0),
        log_path: getSentinelLogPath(),
      }));
      return;
    }
    if (req.url === "/local/logs") {
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ ok: true, log_path: getSentinelLogPath(), logs: getRecentSentinelLogs(40) }));
      return;
    }
    if (req.url === "/local/reconnect") {
      const seq = triggerInstantReconnect("tray_manual_reconnect");
      if (!runtimeState.bridgeRunning) {
        startNativePcBridge(runtimeState.activeCtx);
      }
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(JSON.stringify({ ok: true, reconnecting: true, reconnect_seq: seq }));
      return;
    }
    if (req.method === "GET" && req.url && req.url.startsWith("/proxy-media?")) {
      try {
        const u = new URL(req.url, "http://127.0.0.1");
        const mediaPath = String(u.searchParams.get("path") || "").trim();
        const cfg = loadLocalConfig();
        if (!mediaPath || !cfg.server_url) {
          res.writeHead(404);
          res.end();
          return;
        }
        const fullUrl = mediaPath.startsWith("http")
          ? mediaPath
          : `${cfg.server_url.replace(/\/$/, "")}/${mediaPath.replace(/^\//, "")}`;
        const headers = {};
        if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
        fetch(fullUrl, { method: "GET", headers, signal: AbortSignal.timeout(20000) })
          .then(async (upRes) => {
            if (!upRes.ok) {
              res.writeHead(upRes.status || 404);
              res.end();
              return;
            }
            const ct = upRes.headers.get("content-type") || "image/png";
            const cd = upRes.headers.get("content-disposition");
            const buf = Buffer.from(await upRes.arrayBuffer());
            const respHeaders = {
              "Content-Type": ct,
              "Cache-Control": "public, max-age=3600",
            };
            if (cd) respHeaders["Content-Disposition"] = cd;
            res.writeHead(200, respHeaders);
            res.end(buf);
          })
          .catch(() => {
            res.writeHead(502);
            res.end();
          });
      } catch {
        res.writeHead(400);
        res.end();
      }
      return;
    }
    let rawBody = "";
    req.on("data", (c) => { rawBody += c.toString("utf8"); });
    req.on("end", async () => {
      try {
        const payload = rawBody ? JSON.parse(rawBody) : {};
        const data = await forwardRequest(payload.localPath || null, payload.cloudPath || null, payload.method || req.method || "GET", payload.body ?? null);
        res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
        res.end(JSON.stringify(data));
      } catch (err) {
        res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
        res.end(JSON.stringify({ ok: false, error: String(err?.message || err) }));
      }
    });
  };
}

export function ensureDshHostIpcServer() {
  if (dshHostIpcServer || !runtimeState.activeCtx) return;
  try {
    const srv = http.createServer(createBridgeHttpHandler());
    srv.on("error", () => {});
    srv.listen(DSH_HOST_IPC_PORT, "127.0.0.1", () => {
      dshHostIpcServer = srv;
    });
  } catch {}
}

export function ensureLocalLoopbackServer() {
  if ((localHttpServer && localHttpServer.listening) || localHttpServerBinding) return;
  localHttpServerBinding = true;
  try {
    const port = Number(loadLocalConfig().local_port || 18195);
    const srv = http.createServer(createBridgeHttpHandler());
    srv.keepAliveTimeout = 1000;
    srv.on("error", (err) => {
      localHttpServerBinding = false;
      logSentinel("WARN", `监听本机网关端口 ${port} 失败 (${err?.code || err?.message || err})，将在 2.5s 后重试`);
      try { srv.close(); } catch {}
      if (!localHttpServer && !localLoopbackRetryTimer) {
        localLoopbackRetryTimer = setTimeout(() => {
          localLoopbackRetryTimer = null;
          ensureLocalLoopbackServer();
        }, 2500);
      }
    });
    srv.listen(port, "127.0.0.1", () => {
      localHttpServerBinding = false;
      if (localLoopbackRetryTimer) {
        clearTimeout(localLoopbackRetryTimer);
        localLoopbackRetryTimer = null;
      }
      localHttpServer = srv;
      logSentinel("INFO", `本机常驻网关已绑定 127.0.0.1:${port} (PID ${process.pid})`);
      srv.on("close", () => {
        if (localHttpServer === srv) localHttpServer = null;
        if (!localLoopbackRetryTimer) {
          localLoopbackRetryTimer = setTimeout(() => {
            localLoopbackRetryTimer = null;
            ensureLocalLoopbackServer();
          }, 1500);
        }
      });
      startNativePcBridge(runtimeState.activeCtx);
    });
  } catch (e) {
    localHttpServerBinding = false;
    logSentinel("ERROR", `ensureLocalLoopbackServer 异常: ${e?.message || e}`);
  }
}
