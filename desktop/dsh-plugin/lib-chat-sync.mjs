/**
 * [Module Responsibility]
 * 云端 HTTP 通信、聊天轮次等待及增量同步引擎 (`lib-chat-sync.mjs`)。
 * 从 `lib-chat-cache.mjs` 拆出，负责 `cloudFetch`、单 Bot 轮次发送/编辑/重发、
 * 档案导出导入与 `/api/sync` 版本增量合并。
 */
import { smartCloudFetchJson } from "./lib-sentinel-net.mjs";
import {
  runtimeState,
  loadLocalConfig,
  loadLocalChatCache,
  saveLocalChatCache,
  sanitizeMessagesList,
  mergeCloudPcMessagesIntoCache,
  normMsgKind,
  findBotByIdOrName,
  resolveCloudBotId,
  dedupeAndNormalizeBots,
  loadBotsRosterFromDisk,
  saveBotsRosterToDisk,
} from "./lib-chat-cache.mjs";

export async function cloudFetch(cloudPath, method = "GET", body = null, timeoutMs = 45000, externalSignal = null) {
  const cfg = loadLocalConfig();
  if (!cfg.server_url) return { ok: false, error: "未配置云端地址" };
  const headers = { "Content-Type": "application/json" };
  if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
  return await smartCloudFetchJson(`${cfg.server_url}${cloudPath}`, method, headers, body, timeoutMs, externalSignal);
}

export function beginBotRequest(botId) {
  try { runtimeState.inflightControllersByBot.get(botId)?.abort(); } catch {}
  const ctrl = new AbortController();
  runtimeState.inflightControllersByBot.set(botId, ctrl);
  const epoch = (runtimeState.abortedEpochByBot.get(botId) || 0) + 1;
  runtimeState.abortedEpochByBot.set(botId, epoch);
  return { ctrl, epoch };
}

export function isBotRequestAborted(botId, epoch, ctrl) {
  return Boolean(ctrl?.signal?.aborted) || (runtimeState.abortedEpochByBot.get(botId) || 0) !== epoch;
}

export async function waitForCloudBotReply(localBot, cloudBotId, epoch, ctrl, maxWaitMs = 150000, preTurnIds = null, expectedTurnId = "") {
  const deadline = Date.now() + maxWaitMs;
  let lastAssistant = null;
  let cursorSeq = 0;
  const knownBefore = preTurnIds instanceof Set ? preTurnIds : new Set();
  while (Date.now() < deadline) {
    if (isBotRequestAborted(localBot.id, epoch, ctrl)) {
      delete runtimeState.liveStreamsByBot[localBot.id];
      return { aborted: true, assistantMsg: lastAssistant };
    }
    await new Promise((r) => setTimeout(r, 180));
    if (isBotRequestAborted(localBot.id, epoch, ctrl)) {
      delete runtimeState.liveStreamsByBot[localBot.id];
      return { aborted: true, assistantMsg: lastAssistant };
    }
    const syncRes = await cloudFetch(`/api/sync?since_seq=${cursorSeq}&source=pc`, "GET", null, 8000, ctrl.signal).catch(() => null);
    if (!syncRes?.ok) continue;
    const ctxMap = syncRes.context_by_bot || syncRes.bot_context_by_id;
    if (ctxMap && typeof ctxMap === "object") {
      runtimeState.botContextById = { ...runtimeState.botContextById, ...ctxMap };
    }
    const incomingMsgs = Array.isArray(syncRes.messages) ? syncRes.messages : [];
    const recentTail = Array.isArray(syncRes.recent_tail) ? syncRes.recent_tail : [];
    const batchMsgs = incomingMsgs.length > 0 ? incomingMsgs : recentTail;
    if (batchMsgs.length > 0) {
      mergeCloudPcMessagesIntoCache(batchMsgs);
      for (const m of batchMsgs) {
        const oseq = Number(m?.order_seq || 0);
        if (oseq > cursorSeq) cursorSeq = oseq;
      }
    }
    const liveSt = syncRes.streams_by_bot?.[cloudBotId] || syncRes.streams_by_bot?.[localBot.id] || null;
    const liveEx = syncRes.executions_by_bot?.[cloudBotId] || syncRes.executions_by_bot?.[localBot.id] || null;
    if (liveSt || liveEx) {
      runtimeState.liveStreamsByBot[localBot.id] = {
        text: String(liveSt?.text || ""), thought: String(liveSt?.thought || ""), step_text: String(liveSt?.step_text || liveEx?.last_step_text || ""), tool: String(liveSt?.tool || liveEx?.tool || ""),
        trace: Array.isArray(liveSt?.trace) && liveSt.trace.length ? liveSt.trace : (Array.isArray(liveEx?.trace) ? liveEx.trace : []),
        turn_steps: Array.isArray(liveSt?.turn_steps) && liveSt.turn_steps.length ? liveSt.turn_steps : (Array.isArray(liveEx?.turn_steps) ? liveEx.turn_steps : []),
        timeline: Array.isArray(liveSt?.timeline) && liveSt.timeline.length ? liveSt.timeline : (Array.isArray(liveEx?.timeline) ? liveEx.timeline : []),
        speaker_id: String(liveSt?.speaker_id || localBot.id), speaker_name: String(liveSt?.speaker_name || localBot.name), speaker_node: String(liveSt?.speaker_node || "pc"),
      };
    } else {
      delete runtimeState.liveStreamsByBot[localBot.id];
    }
    const botMsgs = batchMsgs.filter(
      (m) => m.source === "pc" && (m.bot_id === cloudBotId || m.bot_id === localBot.id)
    );
    const tail = botMsgs[botMsgs.length - 1];
    if (
      tail &&
      tail.role === "assistant" &&
      !tail.is_sub_turn &&
      normMsgKind(tail.kind) !== "bot_message" &&
      (String(tail.text || "").trim() || String(tail.sticker || "").trim() || (Array.isArray(tail.attachments) && tail.attachments.length > 0)) &&
      (!tail.id || (!knownBefore.has(String(tail.id)) && !knownBefore.has(`reply-${tail.id}`))) &&
      (!expectedTurnId || !tail.turn_id || tail.turn_id === expectedTurnId)
    ) {
      const tid = String(tail.id || `reply-${Date.now()}`);
      const kindNorm = normMsgKind(tail.kind);
      lastAssistant = {
        id: tid,
        role: "assistant",
        kind: kindNorm,
        text: String(tail.text).trim(),
        thought: String(tail.thought || (kindNorm === "bot_reply" ? "" : runtimeState.liveStreamsByBot[localBot.id]?.thought) || "").trim(),
        bot_id: localBot.id,
        from_bot_id: tail.from_bot_id || localBot.id,
        from_bot_name: tail.from_bot_name || localBot.name,
        from_node: tail.from_node || "",
        to_bot_id: tail.to_bot_id || "",
        to_bot_name: tail.to_bot_name || "",
        to_node: tail.to_node || "",
        direction: tail.direction || "",
        sticker: String(tail.sticker || "").trim(),
        attachments: Array.isArray(tail.attachments) ? tail.attachments : [],
        choices: Array.isArray(tail.choices) ? tail.choices : [],
        variants: Array.isArray(tail.variants) && tail.variants.length ? tail.variants : undefined,
        active_variant_idx: tail.active_variant_idx !== undefined && tail.active_variant_idx !== null ? Number(tail.active_variant_idx) : undefined,
        source: "pc",
        pc_local: true,
        tool_trace: tail.trace || tail.tool_trace || [],
        turn_steps: Array.isArray(tail.turn_steps) ? tail.turn_steps : [],
        timeline: Array.isArray(tail.timeline) ? tail.timeline : [],
        ttft_ms: Number(tail.ttft_ms || tail.metrics?.ttft_ms || 0),
        tps: Number(tail.tps || tail.metrics?.tps || 0),
        cache_hit_pct: Number(tail.cache_hit_pct || tail.metrics?.cache_hit_pct || 0),
        api_calls: Number(tail.api_calls || tail.metrics?.api_calls || 0),
        order_seq: Number(tail.order_seq || tail.seq || 0),
        created: Number(tail.created || (tail.created_at ? Math.round(Number(tail.created_at) * 1000) : Date.now())),
        time: tail.time || new Date().toLocaleTimeString("zh-CN", { hour12: false }),
      };
    }
    if (lastAssistant) {
      delete runtimeState.liveStreamsByBot[localBot.id];
      return { aborted: false, assistantMsg: lastAssistant };
    }
  }
  delete runtimeState.liveStreamsByBot[localBot.id];
  return { aborted: false, assistantMsg: lastAssistant };
}

/**
 * Unified Cloud Chat Turn Executor for `send`, `edit`, and `reroll`
 */
export async function executeCloudChatTurn(op, body = {}) {
  const bot = findBotByIdOrName(body?.bot_id || runtimeState.activeBotId || "pc_engineer");
  const cloudBotId = resolveCloudBotId(bot.id);
  const cached = [...loadLocalChatCache()];
  let kept = [];
  let removedIds = [];
  let userMsg = null;
  let reused = false;

  if (op === "send") {
    const text = String(body?.text || body?.message || "").trim();
    const sticker = String(body?.sticker || "").trim();
    if (!text && !sticker) return { ok: false, error: "消息不能为空" };
    const reqCid = String(body?.client_msg_id || "").trim();
    const dupIdx = reqCid
      ? cached.findIndex((m) => m.bot_id === bot.id && m.role === "user" && (m.id === reqCid || m.client_msg_id === reqCid))
      : -1;
    if (dupIdx >= 0) {
      userMsg = cached[dupIdx];
      reused = true;
      kept = cached;
    } else {
      const rawId = String(body?.message_id || reqCid || `pc-${Date.now()}`);
      const effId = rawId.startsWith("pc-") ? rawId : `pc-${rawId}`;
      const maxExistingSeq = cached.reduce((mx, item) => Math.max(mx, Number(item?.order_seq || item?.seq || 0)), 0);
      const nowMs = Date.now();
      userMsg = {
        id: effId,
        client_msg_id: effId,
        role: "user",
        kind: "chat",
        text,
        ...(sticker ? { sticker } : {}),
        bot_id: bot.id,
        source: "pc",
        pc_local: true,
        order_seq: maxExistingSeq > 0 ? maxExistingSeq + 0.01 : 0,
        created: nowMs,
        time: new Date(nowMs).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }),
      };
      kept = [...cached, userMsg];
      saveLocalChatCache(kept, false, true);
    }
  } else if (op === "edit") {
    const messageId = String(body?.message_id || "").trim();
    const newText = String(body?.text || "").trim();
    if (!messageId || !newText) return { ok: false, error: "message_id 和新内容不能为空" };
    let targetIdx = cached.findIndex((m) => (m.id === messageId || m.client_msg_id === messageId) && m.bot_id === bot.id);
    if (targetIdx === -1) targetIdx = cached.findIndex((m) => m.id === messageId || m.client_msg_id === messageId);
    if (targetIdx === -1) {
      return { ok: false, error: `未找到待编辑的消息 (${messageId})` };
    }
    for (let i = 0; i < cached.length; i++) {
      if (i < targetIdx || cached[i].bot_id !== bot.id) kept.push(cached[i]);
      else if (i === targetIdx) {
        userMsg = { ...cached[i], id: String(cached[i].id || messageId), role: "user", text: newText, bot_id: bot.id, source: "pc", pc_local: true };
        kept.push(userMsg);
      } else removedIds.push(cached[i].id);
    }
    saveLocalChatCache(kept, true, true);
  } else if (op === "reroll") {
    const targetId = body?.message_id ? String(body.message_id).trim() : null;
    let userIdx = -1;
    if (targetId) {
      const hitIdx = cached.findIndex((m) => m.id === targetId && m.bot_id === bot.id);
      for (let i = hitIdx; i >= 0; i--) {
        if (cached[i].bot_id === bot.id && cached[i].role === "user") { userIdx = i; break; }
      }
    }
    if (userIdx === -1) {
      for (let i = cached.length - 1; i >= 0; i--) {
        if (cached[i].bot_id === bot.id && cached[i].role === "user") { userIdx = i; break; }
      }
    }
    if (userIdx === -1) return { ok: false, error: "当前对话没有可重发的用户消息" };
    userMsg = cached[userIdx];
    for (let i = 0; i < cached.length; i++) {
      if (i <= userIdx || cached[i].bot_id !== bot.id) kept.push(cached[i]);
      else removedIds.push(cached[i].id);
    }
    saveLocalChatCache(kept, true, true);
  }

  const preTurnIds = new Set(loadLocalChatCache().map((m) => String(m.id || "")));
  const { ctrl, epoch } = beginBotRequest(bot.id);
  let primaryRes = null;
  let primaryError = "";
  let assistantMsg = null;
  try {
    if (op === "send") {
      primaryRes = await cloudFetch("/api/chat", "POST", { text: userMsg.text, ...(userMsg.sticker ? { sticker: userMsg.sticker } : {}), bot_id: cloudBotId, mode: body?.mode || "auto", client_msg_id: userMsg.id, source: "pc", async_ack: true }, 25000, ctrl.signal)
        .catch((e) => ({ ok: false, error: String(e?.message || e) }));
    } else {
      const endpoint = op === "edit" ? "/api/messages/edit" : "/api/messages/reroll";
      const reqPayload = op === "edit"
        ? { message_id: userMsg.id, text: userMsg.text, bot_id: cloudBotId, source: "pc" }
        : { bot_id: cloudBotId, message_id: userMsg.id, source: "pc" };
      primaryRes = await cloudFetch(endpoint, "POST", reqPayload, 25000, ctrl.signal)
        .catch((e) => ({ ok: false, network_error: true, error: String(e?.message || e) }));
      const stCode = Number(primaryRes?.status || 0);
      const isBusiness4xx = stCode >= 400 && stCode < 500 && stCode !== 404 && stCode !== 405;
      if (!primaryRes?.ok && !isBusiness4xx && op === "reroll") {
        primaryRes = await cloudFetch("/api/chat", "POST", { text: userMsg.text, bot_id: cloudBotId, mode: "auto", client_msg_id: userMsg.id, source: "pc", async_ack: true }, 25000, ctrl.signal)
          .catch((e) => ({ ok: false, error: String(e?.message || e) }));
      }
    }

    if (isBotRequestAborted(bot.id, epoch, ctrl)) {
      return { ok: true, aborted: true, user_message: userMsg, edited_message: userMsg, removed_message_ids: removedIds, messages: loadLocalChatCache(), messages_epoch: runtimeState.messagesEpoch };
    }

    if (!primaryRes?.ok && !primaryRes?.queued && !primaryRes?.turn_id && !primaryRes?.reply && !primaryRes?.text && !primaryRes?.message?.text) {
      primaryError = String(primaryRes?.error || primaryRes?.detail || "云端对话请求失败");
    }

    if (primaryRes?.user_message?.id) {
      preTurnIds.add(String(primaryRes.user_message.id));
    }
    const allRemoved = [
      ...removedIds,
      ...(Array.isArray(primaryRes?.removed_message_ids) ? primaryRes.removed_message_ids : []),
    ];
    if (allRemoved.length > 0) {
      for (const rid of allRemoved) {
        const s = String(rid || "").trim();
        if (!s) continue;
        runtimeState.removedMessageIds.add(s);
        runtimeState.removedMessageIds.add(s.replace(/^reply-/, ""));
        runtimeState.removedMessageIds.add(`reply-${s.replace(/^reply-/, "")}`);
      }
      const cleaned = loadLocalChatCache().filter(
        (m) => !runtimeState.removedMessageIds.has(String(m.id || ""))
      );
      saveLocalChatCache(cleaned, false, true);
    }
    const expectedTurnId = String(primaryRes?.turn_id || "");

    const directReply = primaryRes?.reply || primaryRes?.text || primaryRes?.message?.text || "";
    if (directReply) {
      const rid = String(primaryRes?.message?.id || `reply-${Date.now()}`);
      const mObj = primaryRes?.message || {};
      assistantMsg = {
        id: rid,
        role: "assistant",
        kind: normMsgKind(mObj.kind || primaryRes?.kind || "chat"),
        text: String(directReply).trim(),
        thought: String(primaryRes?.thought || mObj.thought || "").trim(),
        bot_id: bot.id,
        from_bot_id: mObj.from_bot_id || bot.id,
        from_bot_name: mObj.from_bot_name || bot.name,
        from_node: mObj.from_node || "",
        to_bot_id: mObj.to_bot_id || "",
        to_bot_name: mObj.to_bot_name || "",
        to_node: mObj.to_node || "",
        direction: mObj.direction || "",
        sticker: String(primaryRes?.sticker || mObj.sticker || "").trim(),
        attachments: Array.isArray(primaryRes?.attachments) && primaryRes.attachments.length ? primaryRes.attachments : (Array.isArray(mObj.attachments) ? mObj.attachments : []),
        source: "pc",
        pc_local: true,
        tool_trace: primaryRes?.tool_trace || primaryRes?.trace || mObj.trace || [],
        turn_steps: Array.isArray(primaryRes?.turn_steps) ? primaryRes.turn_steps : (Array.isArray(mObj.turn_steps) ? mObj.turn_steps : []),
        timeline: Array.isArray(primaryRes?.timeline) ? primaryRes.timeline : (Array.isArray(mObj.timeline) ? mObj.timeline : []),
        time: new Date().toLocaleTimeString("zh-CN", { hour12: false }),
      };
    } else if (primaryRes?.ok || primaryRes?.queued || primaryRes?.turn_id) {
      const waited = await waitForCloudBotReply(bot, cloudBotId, epoch, ctrl, 150000, preTurnIds, expectedTurnId);
      if (waited.assistantMsg) assistantMsg = waited.assistantMsg;
    }

    if (!isBotRequestAborted(bot.id, epoch, ctrl)) {
      const postSync = await cloudFetch("/api/sync?since_seq=0&source=pc", "GET", null, 8000).catch(() => null);
      if (postSync) applyCloudSyncPayload(postSync);
      if (assistantMsg) {
        const latestCache = [...loadLocalChatCache()];
        const existIdx = latestCache.findIndex(
          (x) => x.id === assistantMsg.id || (x.bot_id === assistantMsg.bot_id && x.role === "assistant" && x.text === assistantMsg.text)
        );
        if (existIdx >= 0) {
          const prev = latestCache[existIdx];
          const bestTrace = (assistantMsg.tool_trace?.length || 0) > (prev.tool_trace?.length || 0)
            ? assistantMsg.tool_trace
            : (prev.tool_trace || []);
          const bestSteps = (assistantMsg.turn_steps?.length || 0) > (prev.turn_steps?.length || 0)
            ? assistantMsg.turn_steps
            : (prev.turn_steps || []);
          const bestTimeline = (assistantMsg.timeline?.length || 0) > (prev.timeline?.length || 0)
            ? assistantMsg.timeline
            : (prev.timeline || []);
          latestCache[existIdx] = {
            ...prev,
            ...assistantMsg,
            id: prev.id,
            thought: String(assistantMsg.thought || prev.thought || "").trim(),
            choices: Array.isArray(assistantMsg.choices) ? assistantMsg.choices : (prev.choices || []),
            variants: Array.isArray(assistantMsg.variants) && assistantMsg.variants.length ? assistantMsg.variants : prev.variants,
            active_variant_idx: assistantMsg.active_variant_idx !== undefined ? assistantMsg.active_variant_idx : prev.active_variant_idx,
            tool_trace: bestTrace,
            turn_steps: bestSteps,
            timeline: bestTimeline,
          };
          saveLocalChatCache(latestCache, op !== "send", true);
        } else {
          saveLocalChatCache([...latestCache, assistantMsg], op !== "send", true);
        }
      }
    }
  } finally {
    if (runtimeState.inflightControllersByBot.get(bot.id) === ctrl) {
      runtimeState.inflightControllersByBot.delete(bot.id);
    }
    delete runtimeState.liveStreamsByBot[bot.id];
  }

  const ok = Boolean(primaryRes?.ok || primaryRes?.queued || assistantMsg);
  return {
    ok,
    ...(ok ? {} : { error: primaryError || "云端未返回回复" }),
    reused,
    user_message: userMsg,
    edited_message: userMsg,
    removed_message_ids: removedIds,
    reply: assistantMsg?.text || primaryRes?.reply || primaryRes?.text || primaryRes?.message?.text || "",
    messages: loadLocalChatCache(),
    messages_epoch: runtimeState.messagesEpoch,
    bot_context_by_id: runtimeState.botContextById || {},
    streams_by_bot: runtimeState.liveStreamsByBot,
    panorama: runtimeState.lastPanoramaCache || (runtimeState.getDshPanorama ? await runtimeState.getDshPanorama(runtimeState.activeCtx, false) : null),
  };
}

export async function switchCloudMessageVariant(body = {}) {
  const bot = findBotByIdOrName(body?.bot_id || runtimeState.activeBotId || "pc_engineer");
  const cloudBotId = resolveCloudBotId(bot.id);
  const msgId = String(body?.message_id || "").trim();
  const variantIdx = Number(body?.variant_idx ?? 0);
  if (!msgId) return { ok: false, error: "message_id 不能为空" };
  const res = await cloudFetch("/api/messages/variant", "POST", {
    message_id: msgId,
    variant_idx: variantIdx,
    bot_id: cloudBotId,
    source: "pc",
  }, 15000).catch((e) => ({ ok: false, error: String(e?.message || e) }));
  if (!res?.ok) return { ok: false, error: res?.error || res?.detail || "切换版本失败" };
  if (res.context_by_bot && typeof res.context_by_bot === "object") {
    runtimeState.botContextById = { ...runtimeState.botContextById, ...res.context_by_bot };
  }
  if (res.messages_epoch) runtimeState.cloudMessagesEpoch = Number(res.messages_epoch) || runtimeState.cloudMessagesEpoch;
  if (res.message && typeof res.message === "object") {
    mergeCloudPcMessagesIntoCache([res.message]);
  }
  return {
    ok: true,
    message: res.message,
    active_variant_idx: variantIdx,
    messages: loadLocalChatCache(),
    messages_epoch: runtimeState.messagesEpoch,
    bot_context_by_id: runtimeState.botContextById || {},
  };
}

export async function handleBotArchiveLocalRoute(op, body = {}) {
  const roster = loadBotsRosterFromDisk();
  if (op === "export") {
    const target = findBotByIdOrName(body?.bot_id || runtimeState.activeBotId || "pc_engineer");
    const cloudBotId = resolveCloudBotId(target?.id || "pc_engineer");
    const cloudRes = await cloudFetch("/api/bots/export", "POST", {
      bot_id: cloudBotId,
      include_messages: body?.include_messages !== false,
      max_messages: Number(body?.max_messages || 500),
    }, 30000).catch(() => null);
    if (cloudRes?.ok && cloudRes?.bundle) {
      return { ok: true, filename: cloudRes.filename || `${cloudBotId}.dsb-bot.json`, bundle: cloudRes.bundle };
    }
    const localMsgs = loadLocalChatCache().filter((m) => m.bot_id === target.id).slice(-500);
    const fallbackBundle = {
      format: "dsb-bot-archive",
      version: "1.0",
      exported_at: new Date().toISOString(),
      bot: { ...target },
      messages: localMsgs,
      stats: { messages_count: localMsgs.length },
    };
    return { ok: true, filename: `${target.id}.dsb-bot.json`, bundle: fallbackBundle };
  }
  const bundle = body?.bundle && typeof body.bundle === "object" ? body.bundle : body;
  const impRes = await cloudFetch("/api/bots/import", "POST", {
    bundle,
    mode: String(body?.mode || "merge"),
    target_bot_id: String(body?.target_bot_id || ""),
    home_node: "pc",
    source: "pc",
    import_messages: body?.import_messages !== false,
  }, 45000).catch((e) => ({ ok: false, error: String(e?.message || e) }));
  if (!impRes?.ok) return { ok: false, error: impRes?.error || impRes?.detail || "导入助理档案失败" };
  if (Array.isArray(impRes.bots) && impRes.bots.length > 0) {
    runtimeState.botsRoster = dedupeAndNormalizeBots(impRes.bots);
  } else if (impRes.bot && typeof impRes.bot === "object") {
    runtimeState.botsRoster = dedupeAndNormalizeBots([...roster.filter((b) => b.id !== impRes.bot.id), impRes.bot]);
  }
  if (impRes.bot_id) runtimeState.activeBotId = impRes.bot_id;
  saveBotsRosterToDisk();
  const postSync = await cloudFetch("/api/sync?since_seq=0&source=pc", "GET", null, 8000).catch(() => null);
  if (Array.isArray(postSync?.messages) && postSync.messages.length > 0) {
    mergeCloudPcMessagesIntoCache(postSync.messages);
  }
  return {
    ok: true,
    bot_id: impRes.bot_id,
    bot: findBotByIdOrName(impRes.bot_id || runtimeState.activeBotId),
    bots: loadBotsRosterFromDisk(),
    active_bot_id: runtimeState.activeBotId,
    messages: loadLocalChatCache(),
    imported_messages: Number(impRes.imported_messages || 0),
  };
}

export function applyCloudSyncPayload(syncRes) {
  if (!syncRes || typeof syncRes !== "object") return;
  const sv = syncRes.sync_versions && typeof syncRes.sync_versions === "object" ? syncRes.sync_versions : {};
  const bVer = syncRes.bots_ver || sv.bots_ver;
  const pVer = syncRes.pins_ver || sv.pins_ver;
  const cVer = syncRes.config_ver || sv.config_ver;
  const vVer = syncRes.vault_ver || sv.vault_ver;
  if (bVer) runtimeState.cloudBotsVer = String(bVer);
  if (pVer) runtimeState.cloudPinsVer = String(pVer);
  if (cVer) runtimeState.cloudConfigVer = String(cVer);
  if (vVer) runtimeState.cloudVaultVer = String(vVer);
  if (!syncRes.bots_unchanged && Array.isArray(syncRes.bots) && syncRes.bots.length > 0) {
    runtimeState.botsRoster = dedupeAndNormalizeBots(syncRes.bots);
    if (!runtimeState.botsRoster.some((b) => b.id === runtimeState.activeBotId)) {
      runtimeState.activeBotId = runtimeState.botsRoster[0]?.id || "pc_engineer";
    }
    saveBotsRosterToDisk();
  }
  if (Array.isArray(syncRes.messages)) {
    const cloudEp = Number(syncRes.messages_epoch || 0);
    if (cloudEp > 0 && runtimeState.cloudMessagesEpoch !== cloudEp && syncRes.delta === false) {
      runtimeState.cloudMessagesEpoch = cloudEp;
      saveLocalChatCache(sanitizeMessagesList(syncRes.messages), true, true);
    } else if (syncRes.messages.length > 0) {
      if (cloudEp > 0) runtimeState.cloudMessagesEpoch = cloudEp;
      mergeCloudPcMessagesIntoCache(syncRes.messages);
    }
  }
  const ctxMap = syncRes.context_by_bot || syncRes.bot_context_by_id;
  if (ctxMap && typeof ctxMap === "object") {
    runtimeState.botContextById = { ...runtimeState.botContextById, ...ctxMap };
  }
  const incomingPins = syncRes.pinned_memories || syncRes.pins;
  if (Array.isArray(incomingPins)) runtimeState.pinnedMemories = incomingPins;
  if (syncRes.streams_by_bot && typeof syncRes.streams_by_bot === "object") {
    const activeCloudBotIds = new Set();
    for (const [k, stVal] of Object.entries(syncRes.streams_by_bot)) {
      if (!stVal || typeof stVal !== "object") continue;
      const localB = findBotByIdOrName(k);
      if (!localB) continue;
      activeCloudBotIds.add(localB.id);
      const exVal = syncRes.executions_by_bot?.[k] || syncRes.executions_by_bot?.[localB.id] || null;
      runtimeState.liveStreamsByBot[localB.id] = {
        text: String(stVal.text || ""), thought: String(stVal.thought || ""), step_text: String(stVal.step_text || exVal?.last_step_text || ""), tool: String(stVal.tool || exVal?.tool || ""),
        trace: Array.isArray(stVal.trace) && stVal.trace.length ? stVal.trace : (Array.isArray(exVal?.trace) ? exVal.trace : []),
        turn_steps: Array.isArray(stVal.turn_steps) && stVal.turn_steps.length ? stVal.turn_steps : (Array.isArray(exVal?.turn_steps) ? exVal.turn_steps : []),
        timeline: Array.isArray(stVal.timeline) && stVal.timeline.length ? stVal.timeline : (Array.isArray(exVal?.timeline) ? exVal.timeline : []),
        speaker_id: String(stVal.speaker_id || localB.id), speaker_name: String(stVal.speaker_name || localB.name), speaker_node: String(stVal.speaker_node || "pc"),
      };
    }
    for (const existingBotId of Object.keys(runtimeState.liveStreamsByBot)) {
      if (!activeCloudBotIds.has(existingBotId) && !syncRes.executions_by_bot?.[existingBotId]) delete runtimeState.liveStreamsByBot[existingBotId];
    }
  }
}

export function applyInboxSyncVersions(syncVersions, fetchFn = cloudFetch) {
  if (!syncVersions || typeof syncVersions !== "object") return false;
  const ep = Number(syncVersions.messages_epoch || 0);
  const changed = (
    (ep > 0 && Number(runtimeState.cloudMessagesEpoch || 0) > 0 && ep !== Number(runtimeState.cloudMessagesEpoch || 0)) ||
    (syncVersions.pins_ver && String(syncVersions.pins_ver) !== String(runtimeState.cloudPinsVer || "")) ||
    (syncVersions.bots_ver && String(syncVersions.bots_ver) !== String(runtimeState.cloudBotsVer || "")) ||
    (syncVersions.config_ver && String(syncVersions.config_ver) !== String(runtimeState.cloudConfigVer || "")) ||
    (syncVersions.vault_ver && String(syncVersions.vault_ver) !== String(runtimeState.cloudVaultVer || ""))
  );
  if (!changed) return false;
  runtimeState.lastBgCloudSyncAt = 0;
  const now = Date.now();
  if (typeof fetchFn === "function" && now - Number(runtimeState.lastFastDeltaKickAt || 0) >= 120) {
    runtimeState.lastFastDeltaKickAt = now;
    const syncUrl = `/api/sync?since_seq=0&source=pc&bots_ver=${encodeURIComponent(runtimeState.cloudBotsVer || "")}&pins_ver=${encodeURIComponent(runtimeState.cloudPinsVer || "")}&config_ver=${encodeURIComponent(runtimeState.cloudConfigVer || "")}`;
    Promise.resolve(fetchFn(syncUrl, "GET", null, 6000)).then((res) => { if (res) applyCloudSyncPayload(res); }).catch(() => {});
  }
  return true;
}
