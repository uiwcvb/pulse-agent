/**
 * [Module Responsibility]
 * 本地聊天缓存、Bot 花名册与云端对话轮次引擎 (`lib-chat-cache.mjs`)。
 * 负责：
 * 1. 维护默认与自定义 Bot 花名册 (`DEFAULT_BOTS_ROSTER`) 及本地磁盘持久化 (`bots-roster.json`)。
 * 2. 本地消息缓存 (`chat-history-cache.json`) 的清洗、去重与云端/PC 双端消息合并。
 * 3. 共享运行时状态 (`runtimeState`) 与本地配置文件 (`config.json`) 读取。
 * 4. 云端 HTTP 通信封装 (`cloudFetch`) 与单 Bot 聊天轮次流式等待 (`executeCloudChatTurn`)。
 *
 * [Public Interface]
 * - `runtimeState`: 共享运行时状态单例
 * - `DEFAULT_BOTS_ROSTER`, `dedupeAndNormalizeBots(rawList)`, `loadBotsRosterFromDisk()`, `saveBotsRosterToDisk(list)`
 * - `findBotByIdOrName(target)`, `resolveCloudBotId(bot)`
 * - `sanitizeMessagesList(list)`, `mergeCloudPcMessagesIntoCache(cloudMsgs, pcMsgs)`
 * - `loadLocalChatCache()`, `saveLocalChatCache(immediate)`
 * - `loadLocalConfig()`, `getService(context)`, `cloudFetch(urlPath, options)`
 * - `beginBotRequest(botId)`, `isBotRequestAborted(botId, reqId)`, `waitForCloudBotReply(...)`, `executeCloudChatTurn(...)`
 *
 * [Dependencies]
 * - Node.js 标准库: `node:fs`, `node:path`, `node:os`
 */
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

const __dirname = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"));

export const HOSTNAME = os.hostname() || "Windows-PC";
export const PC_DEVICE_ID = `pc-${HOSTNAME.toLowerCase().replace(/[^a-z0-9_-]/g, "-")}`;
export const PC_DEVICE_NAME = `${HOSTNAME} (DSB 电脑端)`;
export const DSH_HOST_IPC_PORT = Number(process.env.DS_CLAW_HOST_IPC_PORT || 18196);

// Keep localhost IPC strictly direct; cloud requests use `smartCloudFetchJson` dual-route (Direct <-> Proxy)
if (!process.env.NO_PROXY && !process.env.no_proxy) {
  process.env.NO_PROXY = "localhost,127.0.0.1,::1";
} else {
  const curNoProxy = process.env.NO_PROXY || process.env.no_proxy || "";
  for (const host of ["localhost", "127.0.0.1", "::1"]) {
    if (!curNoProxy.includes(host)) {
      process.env.NO_PROXY = curNoProxy ? `${curNoProxy},${host}` : host;
    }
  }
}

let cachedWhaleDataUri = null;
export function getWhalechanAvatarUri() {
  if (cachedWhaleDataUri !== null) return cachedWhaleDataUri;
  try {
    const p = path.join(__dirname, "assets", "whalechan-eyeless.png");
    if (fs.existsSync(p)) {
      cachedWhaleDataUri = `data:image/png;base64,${fs.readFileSync(p).toString("base64")}`;
      return cachedWhaleDataUri;
    }
  } catch {}
  cachedWhaleDataUri = "";
  return "";
}

export const DEFAULT_BOTS_ROSTER = [
  { id: "pc_engineer", name: "司工", color: "#0A84FF", shape: "square", primary: true, description: "电脑端主控总工程师，负责代码架构、Git Bash/终端操控、本地会话编排与云电脑协同", persona: "你是电脑端专属主控总工程师「司工」，沉稳干练、动手极快，擅长代码工程、Git Bash 自动化、DSB 会话调度与云电脑操控。跨助理协作统一使用 blackboard，查看其他助理进度使用 peek_bots。" },
  { id: "pc_research", name: "探微", color: "#3478F6", shape: "circle", primary: false, description: "电脑端深度技术调研、文献检索与源码审计专家", persona: "你是电脑端调研专家「探微」，负责深度技术调研、文档考据、源码审计与事实核验，结论严谨有据。" },
  { id: "pc_reviewer", name: "审校", color: "#FF8A3D", shape: "diamond", primary: false, description: "电脑端独立审查员，专攻代码缺陷、边界条件与方案验收", persona: "你是电脑端独立审查员「审校」，专门对代码改动、架构方案与产出物做严格复核，挑出隐患与未测边界。" },
  { id: "pc_companion", name: "云汐", color: "#2EC4B6", shape: "flower", primary: false, description: "电脑端轻快灵感搭档，负责头脑风暴、文稿润色与桌面日常协理", persona: "你是电脑端灵感搭档「云汐」，温柔利落、富有创造力，擅长文稿打磨、灵感碰撞与日常事务协理。" },
];

const LEGACY_PHONE_ONLY_IDS = new Set(["companion", "research", "contrarian", "whalechan"]);

export const runtimeState = {
  deviceId: PC_DEVICE_ID,
  deviceName: PC_DEVICE_NAME,
  deviceType: "pc",
  vpsOnline: false,
  lastHeartbeatAt: "未连接",
  lastHeartbeatOkMs: 0,
  phoneOnline: false,
  phoneLastActiveTime: "",
  phoneLast3Dialogs: [],
  connectedDevices: [],
  dshPeers: [],
  sharedMemories: { pins: [], pinned_memories_md: "", user_traits: "", personal_impression: "" },
  bridgeId: `dsh-bridge-${PC_DEVICE_ID}`,
  bridgeGeneration: 0,
  bridgeRunning: false,
  activeCtx: null,
  rootCtx: null,
  services: {},
  lastAgentErrorBySid: new Map(),
  activeBotId: "pc_engineer",
  botsRoster: null,
  cachedPanorama: null,
  cachedPanoramaAt: 0,
  sessionDetailById: new Map(),
  localMessagesCache: null,
  messagesEpoch: 1,
  removedMessageIds: new Set(),
  inflightControllersByBot: new Map(),
  abortedEpochByBot: new Map(),
  cloudBotIdByLocalId: new Map(),
  liveStreamsByBot: {},
  botContextById: {},
  cloudVaultVer: "",
  lastFastDeltaKickAt: 0,
};

export function loadLocalConfig() {
  const cfgPath = path.join(__dirname, "bridge_config.json");
  let cfg = {
    server_url: process.env.PULSE_SERVER_URL || process.env.DS_CLAW_SERVER_URL || "http://127.0.0.1:18190",
    token: process.env.DS_CLAW_TOKEN || "",
    local_port: Number(process.env.DS_CLAW_LOCAL_PORT || 18195),
    dsh_home: path.join(os.homedir(), ".dsh"),
  };
  try {
    if (fs.existsSync(cfgPath)) cfg = { ...cfg, ...JSON.parse(fs.readFileSync(cfgPath, "utf8")) };
  } catch {}
  if (!cfg.token) {
    const repoRoot = process.env.DS_CLAW_REPO_ROOT || path.resolve(__dirname, "..", "..");
    for (const c of [
      path.join(os.homedir(), ".dsh", "ds-claw-token"),
      path.join(repoRoot, "private", "device-token"),
      path.join(repoRoot, "server", "data", "device-token"),
    ]) {
      try {
        if (fs.existsSync(c)) {
          cfg.token = fs.readFileSync(c, "utf8").trim();
          if (cfg.token) break;
        }
      } catch {}
    }
  }
  cfg.server_url = String(cfg.server_url || "http://127.0.0.1:18190").replace(/\/$/, "");
  return cfg;
}

export function getService(ctx, key) {
  try {
    if (runtimeState.services?.[key]) return runtimeState.services[key];
    for (const target of [ctx, runtimeState.rootCtx, runtimeState.activeCtx]) {
      if (!target || typeof target !== "object") continue;
      // IMPORTANT: Never access `target[key]` directly on a Cordis Context (`ctx`)!
      // Cordis registers properties via `ctx.mixin` / Context getters that throw
      // `Error: cannot get property "parent" without inject` when accessed outside an `inject` scope,
      // and in DSH Desktop Host some internal getters emit an async rejection or host error if touched blindly.
      // Only use `target.get(key)` when `get` is a function on `target`.
      try {
        const getDesc = Object.getOwnPropertyDescriptor(target, "get") || Object.getOwnPropertyDescriptor(Object.getPrototypeOf(target) || {}, "get");
        const getFn = typeof getDesc?.value === "function" ? getDesc.value : null;
        if (getFn) {
          const v = getFn.call(target, key);
          if (v) {
            runtimeState.services[key] = v;
            return v;
          }
        }
      } catch {}
    }
  } catch {}
  return null;
}

export function dedupeAndNormalizeBots(rawItems = []) {
  const byId = new Map();
  const nameToId = new Map();
  const whaleUri = getWhalechanAvatarUri();
  const defaultById = new Map();
  const defaultByName = new Map();
  for (const def of DEFAULT_BOTS_ROSTER) {
    const defWithAvatar = { ...def, avatar_url: def.id === "whalechan" ? whaleUri : "" };
    defaultById.set(def.id, defWithAvatar);
    defaultByName.set(def.name.toLowerCase(), defWithAvatar);
  }
  const hasPcPrimary = Array.isArray(rawItems) && rawItems.some((x) => String(x?.id || "").trim() === "pc_engineer");
  const isLegacyPhoneOnlyCache = Array.isArray(rawItems) && rawItems.length > 0 && !hasPcPrimary &&
    rawItems.every((x) => LEGACY_PHONE_ONLY_IDS.has(String(x?.id || "").trim()));
  const sourceItems = Array.isArray(rawItems) && rawItems.length > 0 && !isLegacyPhoneOnlyCache ? rawItems : DEFAULT_BOTS_ROSTER;
  for (const item of sourceItems) {
    if (!item) continue;
    const rawId = String(item.id || "").trim();
    const rawName = String(item.name || "").trim();
    if (!rawId && !rawName) continue;
    const nameKey = rawName.toLowerCase();
    const matchedDef = defaultById.get(rawId) || defaultByName.get(nameKey) || null;
    const canonicalId = matchedDef ? matchedDef.id : (rawId || nameToId.get(nameKey) || "");
    if (!canonicalId) continue;
    if (matchedDef && rawId && rawId !== canonicalId) {
      runtimeState.cloudBotIdByLocalId.set(canonicalId, rawId);
    }
    const prev = byId.get(canonicalId) || matchedDef || {};
    const finalName = rawName || prev.name || canonicalId;
    const pcNodeCfg = (item.node_settings && typeof item.node_settings.pc === "object") ? item.node_settings.pc : null;
    nameToId.set(finalName.toLowerCase(), canonicalId);
    byId.set(canonicalId, {
      ...prev,
      id: canonicalId,
      name: finalName,
      color: String(item.color || item.avatar_color || prev.color || "#0A84FF"),
      shape: String(item.shape || prev.shape || "square"),
      primary: canonicalId === "pc_engineer" ? true : Boolean(item.primary ?? prev.primary ?? false),
      home_node: String(item.home_node || prev.home_node || "pc"),
      description: String(item.description || prev.description || ""),
      model: String((pcNodeCfg && pcNodeCfg.model !== undefined) ? pcNodeCfg.model : (item.model || prev.model || "")),
      curiosity_enabled: Boolean((pcNodeCfg && pcNodeCfg.curiosity_enabled !== undefined) ? pcNodeCfg.curiosity_enabled : (item.curiosity_enabled ?? prev.curiosity_enabled ?? (canonicalId === "pc_engineer"))),
      notify: Boolean((pcNodeCfg && pcNodeCfg.notify !== undefined) ? pcNodeCfg.notify : (item.notify ?? prev.notify ?? true)),
      persona: String(item.instructions || item.persona || prev.persona || ""),
      instructions: String(item.instructions || item.persona || prev.persona || ""),
      avatar_url: (canonicalId === "whalechan" || item.shape === "whalechan") ? whaleUri : String(item.avatar_url || prev.avatar_url || ""),
    });
  }
  if (!byId.has("pc_engineer") && defaultById.has("pc_engineer")) {
    const comp = defaultById.get("pc_engineer");
    return [comp, ...Array.from(byId.values())];
  }
  return Array.from(byId.values());
}

export function getBotsRosterFilePath() {
  const cfg = loadLocalConfig();
  return path.join(cfg.dsh_home || path.join(os.homedir(), ".dsh"), "baike-bots-roster.json");
}

export function loadBotsRosterFromDisk() {
  if (Array.isArray(runtimeState.botsRoster) && runtimeState.botsRoster.length > 0) {
    runtimeState.botsRoster = dedupeAndNormalizeBots(runtimeState.botsRoster);
    return runtimeState.botsRoster;
  }
  let saved = [], activeId = "pc_engineer";
  try {
    const p = getBotsRosterFilePath();
    if (fs.existsSync(p)) {
      const parsed = JSON.parse(fs.readFileSync(p, "utf8"));
      if (Array.isArray(parsed?.bots)) saved = parsed.bots;
      if (parsed?.active_bot_id) activeId = String(parsed.active_bot_id);
    }
  } catch {}
  runtimeState.botsRoster = dedupeAndNormalizeBots(saved);
  runtimeState.activeBotId = runtimeState.botsRoster.some((b) => b.id === activeId) ? activeId : (runtimeState.botsRoster[0]?.id || "pc_engineer");
  return runtimeState.botsRoster;
}

export function saveBotsRosterToDisk() {
  try {
    const p = getBotsRosterFilePath();
    fs.mkdirSync(path.dirname(p), { recursive: true });
    runtimeState.botsRoster = dedupeAndNormalizeBots(runtimeState.botsRoster || DEFAULT_BOTS_ROSTER);
    fs.writeFileSync(p, JSON.stringify({ active_bot_id: runtimeState.activeBotId, bots: runtimeState.botsRoster, updated_at: Date.now() }, null, 2), "utf8");
  } catch {}
}

export function findBotByIdOrName(botIdOrName) {
  const roster = loadBotsRosterFromDisk();
  const q = String(botIdOrName || "pc_engineer").trim().toLowerCase();
  for (const [localId, cloudId] of runtimeState.cloudBotIdByLocalId.entries()) {
    if (cloudId.toLowerCase() === q) return roster.find((b) => b.id === localId) || roster[0];
  }
  return roster.find((b) => b.id.toLowerCase() === q || b.name.toLowerCase() === q) || roster[0];
}

export function resolveCloudBotId(localBotId) {
  return runtimeState.cloudBotIdByLocalId.get(localBotId) || localBotId || "pc_engineer";
}

export function getLocalChatCachePath() {
  const cfg = loadLocalConfig();
  return path.join(cfg.dsh_home || path.join(os.homedir(), ".dsh"), "baike-infinite-chat-cache.json");
}

export function normMsgKind(k) {
  const s = String(k || "").trim();
  return !s || s === "chat" ? "chat" : s;
}

function _normCreatedMs(m) {
  const raw = Number(m?.created || (m?.created_at ? Number(m.created_at) * 1000 : 0) || 0);
  if (raw > 0 && raw < 1e11) return Math.round(raw * 1000);
  return raw;
}

export function sortPcMessagesDeterministically(list) {
  if (!Array.isArray(list) || list.length <= 1) return Array.isArray(list) ? list : [];
  return [...list].sort((a, b) => {
    const ao = Number(a?.order_seq || a?.seq || 0);
    const bo = Number(b?.order_seq || b?.seq || 0);
    if (ao > 0 && bo > 0 && ao !== bo) return ao - bo;
    const ac = _normCreatedMs(a);
    const bc = _normCreatedMs(b);
    if (ac > 0 && bc > 0 && ac !== bc) return ac - bc;
    if (ao !== bo) return ao - bo;
    return Number(a?.seq || 0) - Number(b?.seq || 0);
  });
}

export function sanitizeMessagesList(rawMessages) {
  if (!Array.isArray(rawMessages)) return [];
  const out = [];
  for (const m of rawMessages) {
    if (!m || typeof m !== "object") continue;
    const text = String(m.text || "").trim();
    const sticker = String(m.sticker || "").trim();
    const attachments = Array.isArray(m.attachments) ? m.attachments.filter((a) => a && typeof a === "object" && a.path) : [];
    const rawId = String(m.id || "");
    if ((!text && !sticker && !attachments.length) || m.source !== "pc") continue;
    const nKind = normMsgKind(m.kind);
    const role = (m.role === "user" && nKind !== "bot_message") ? "user" : "assistant";
    const botId = findBotByIdOrName(m.bot_id || "pc_engineer")?.id || "pc_engineer";
    const fromResolvedId = m.from_bot_id ? (findBotByIdOrName(m.from_bot_id)?.id || m.from_bot_id) : "";
    if (nKind === "bot_message" && m.direction === "outbound") continue;
    if (role === "assistant" && fromResolvedId && fromResolvedId !== botId && nKind !== "bot_message") continue;
    const incomingTrace = nKind === "bot_message"
      ? []
      : (Array.isArray(m.tool_trace) && m.tool_trace.length
          ? m.tool_trace
          : (Array.isArray(m.trace) ? m.trace : []));
    const mMetrics = m.metrics && typeof m.metrics === "object" ? m.metrics : {};
    const createdMs = Number(m.created || (m.created_at ? Math.round(Number(m.created_at) * 1000) : 0) || 0);
    const fmtTime = m.time || (createdMs > 0 ? new Date(createdMs).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }) : new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }));
    const norm = {
      ...m,
      id: rawId || `${role === "user" ? "pc" : "reply"}-${Date.now()}-${out.length}`,
      role,
      kind: nKind,
      text,
      thought: nKind === "bot_message" ? "" : String(m.thought || "").trim(),
      bot_id: botId,
      from_bot_id: m.from_bot_id || "",
      from_bot_name: m.from_bot_name || "",
      from_node: m.from_node || "",
      to_bot_id: m.to_bot_id || "",
      to_bot_name: m.to_bot_name || "",
      to_node: m.to_node || "",
      direction: m.direction || "",
      sticker,
      attachments,
      choices: Array.isArray(m.choices) ? m.choices : [],
      variants: Array.isArray(m.variants) && m.variants.length ? m.variants : undefined,
      active_variant_idx: m.active_variant_idx !== undefined && m.active_variant_idx !== null ? Number(m.active_variant_idx) : undefined,
      decision: m.decision || "",
      ttft_ms: Number(m.ttft_ms || mMetrics.ttft_ms || 0),
      tps: Number(m.tps || mMetrics.tps || 0),
      cache_hit_pct: Number(m.cache_hit_pct || mMetrics.cache_hit_pct || 0),
      api_calls: Number(m.api_calls || mMetrics.api_calls || 0),
      order_seq: Number(m.order_seq || m.seq || 0),
      created: _normCreatedMs(m) || createdMs,
      source: "pc",
      pc_local: true,
      tool_trace: incomingTrace,
      turn_steps: nKind === "bot_message" ? [] : (Array.isArray(m.turn_steps) ? m.turn_steps : []),
      timeline: nKind === "bot_message" ? [] : (Array.isArray(m.timeline) ? m.timeline : []),
      time: fmtTime,
    };
    const prev = out[out.length - 1];
    const sameAssistantReply =
      prev &&
      !prev.is_sub_turn &&
      !norm.is_sub_turn &&
      prev.bot_id === norm.bot_id &&
      prev.role === norm.role &&
      prev.text === norm.text &&
      prev.sticker === norm.sticker &&
      (normMsgKind(prev.kind) === norm.kind ||
        (prev.role === "assistant" && normMsgKind(prev.kind) !== "bot_message" && norm.kind !== "bot_message"));
    if (prev && prev.bot_id === norm.bot_id && (prev.id === norm.id || prev.id === `reply-${norm.id}` || `reply-${prev.id}` === norm.id || sameAssistantReply)) {
      const mergedTrace = norm.tool_trace.length > (prev.tool_trace?.length || 0) ? norm.tool_trace : (prev.tool_trace || []);
      const mergedTurnSteps = (norm.turn_steps?.length || 0) > (prev.turn_steps?.length || 0) ? norm.turn_steps : (prev.turn_steps || []);
      const bestKind = norm.kind === "bot_reply" || normMsgKind(prev.kind) === "bot_reply" ? "bot_reply" : (norm.kind || prev.kind || "chat");
      out[out.length - 1] = {
        ...prev,
        ...norm,
        id: prev.id.startsWith("reply-") && !norm.id.startsWith("reply-") ? norm.id : prev.id,
        kind: bestKind,
        from_bot_id: norm.from_bot_id || prev.from_bot_id || "",
        from_bot_name: (bestKind === "bot_reply" && (norm.from_bot_name || prev.from_bot_name)) ? (norm.from_bot_name?.includes("·") ? norm.from_bot_name : (prev.from_bot_name || norm.from_bot_name)) : (norm.from_bot_name || prev.from_bot_name || ""),
        from_node: norm.from_node || prev.from_node || "",
        to_bot_id: norm.to_bot_id || prev.to_bot_id || "",
        to_bot_name: norm.to_bot_name || prev.to_bot_name || "",
        to_node: norm.to_node || prev.to_node || "",
        direction: norm.direction || prev.direction || "",
        sticker: String(norm.sticker || prev.sticker || "").trim(),
        attachments: norm.attachments?.length ? norm.attachments : (prev.attachments || []),
        thought: String(norm.thought || prev.thought || "").trim(),
        tool_trace: mergedTrace,
        turn_steps: mergedTurnSteps,
        timeline: (norm.timeline?.length || 0) >= (prev.timeline?.length || 0) ? (norm.timeline || []) : (prev.timeline || []),
      };
      continue;
    }
    out.push(norm);
  }
  return sortPcMessagesDeterministically(out).slice(-160);
}

export function mergeCloudPcMessagesIntoCache(cloudMessages) {
  if (!Array.isArray(cloudMessages) || cloudMessages.length === 0) return;
  const cached = [...loadLocalChatCache()];
  let changed = false;
  for (const cm of cloudMessages) {
    if (!cm || cm.source !== "pc") continue;
    const cid = String(cm.id || "");
    const replyCid = `reply-${cid}`;
    const pcCid = `pc-${cid}`;
    if (
      cid &&
      (runtimeState.removedMessageIds.has(cid) ||
        runtimeState.removedMessageIds.has(replyCid) ||
        runtimeState.removedMessageIds.has(pcCid) ||
        (cm.client_msg_id && runtimeState.removedMessageIds.has(String(cm.client_msg_id))))
    ) {
      continue;
    }
    const text = String(cm.text || "").trim();
    const sticker = String(cm.sticker || "").trim();
    const cmAtts = Array.isArray(cm.attachments) ? cm.attachments.filter((a) => a && typeof a === "object" && a.path) : [];
    if (!text && !sticker && !cmAtts.length) continue;
    const cmKind = normMsgKind(cm.kind);
    const role = (cm.role === "user" && cmKind !== "bot_message") ? "user" : "assistant";
    const fallbackBotId = runtimeState.activeBotId || "pc_engineer";
    const botId = findBotByIdOrName(cm.bot_id || fallbackBotId)?.id || fallbackBotId;
    const fromResolvedId = cm.from_bot_id ? (findBotByIdOrName(cm.from_bot_id)?.id || cm.from_bot_id) : "";
    if (cmKind === "bot_message" && cm.direction === "outbound") continue;
    if (role === "assistant" && fromResolvedId && fromResolvedId !== botId && cmKind !== "bot_message") continue;
    const cmMetrics = cm.metrics && typeof cm.metrics === "object" ? cm.metrics : {};
    const cmCreatedMs = _normCreatedMs(cm) || Number(cm.created || 0);
    let existingIdx = -1;
    for (let i = cached.length - 1; i >= 0; i--) {
      const x = cached[i];
      if (!x) continue;
      if (
        (cid && (x.id === cid || x.id === replyCid || x.id === pcCid || x.client_msg_id === cid || x.id === cm.client_msg_id || (cm.client_msg_id && x.client_msg_id === cm.client_msg_id))) ||
        (!cm.is_sub_turn && !x.is_sub_turn && x.bot_id === botId && x.role === role && x.text === text && (x.sticker || "") === sticker && (normMsgKind(x.kind) === cmKind || (role === "assistant" && normMsgKind(x.kind) !== "bot_message" && cmKind !== "bot_message")))
      ) {
        existingIdx = i;
        break;
      }
    }
    if (existingIdx >= 0) {
      const prev = cached[existingIdx];
      const cmTrace = cmKind === "bot_message" ? [] : (Array.isArray(cm.trace) && cm.trace.length ? cm.trace : (Array.isArray(cm.tool_trace) ? cm.tool_trace : []));
      const traceArr = cmTrace.length > (prev.tool_trace?.length || 0) ? cmTrace : (prev.tool_trace || []);
      const cmTurnSteps = cmKind === "bot_message" ? [] : (Array.isArray(cm.turn_steps) ? cm.turn_steps : []);
      const turnStepsArr = cmTurnSteps.length > (prev.turn_steps?.length || 0) ? cmTurnSteps : (prev.turn_steps || []);
      const cmTimeline = cmKind === "bot_message" ? [] : (Array.isArray(cm.timeline) ? cm.timeline : []);
      const timelineArr = cmTimeline.length >= (prev.timeline?.length || 0) ? (cmTimeline.length ? cmTimeline : (prev.timeline || [])) : (prev.timeline || []);
      const nextTtft = Number(cm.ttft_ms || cmMetrics.ttft_ms || prev.ttft_ms || 0);
      const nextTps = Number(cm.tps || cmMetrics.tps || prev.tps || 0);
      const nextCachePct = Number(cm.cache_hit_pct || cmMetrics.cache_hit_pct || prev.cache_hit_pct || 0);
      const nextApiCalls = Number(cm.api_calls || cmMetrics.api_calls || prev.api_calls || 0);
      const cmOrderSeq = Number(cm.order_seq || cm.seq || 0);
      const prevOrderSeq = Number(prev.order_seq || prev.seq || 0);
      const cmVarLen = Array.isArray(cm.variants) ? cm.variants.length : 0;
      const prevVarLen = Array.isArray(prev.variants) ? prev.variants.length : 0;
      const cmActiveVar = cm.active_variant_idx !== undefined && cm.active_variant_idx !== null ? Number(cm.active_variant_idx) : undefined;
      const prevActiveVar = prev.active_variant_idx !== undefined && prev.active_variant_idx !== null ? Number(prev.active_variant_idx) : undefined;
      const needsAuthoritativeSync = (
        (cid && String(prev.id || "").startsWith("pc-") && !cid.startsWith("pc-")) ||
        (cmOrderSeq > 0 && cmOrderSeq !== prevOrderSeq) ||
        (cmCreatedMs > 0 && (!prev.created || prev.created < 1e11)) ||
        (cmVarLen !== prevVarLen) ||
        (cmActiveVar !== undefined && cmActiveVar !== prevActiveVar) ||
        (cm.text !== undefined && String(cm.text) !== String(prev.text || ""))
      );
      if (
        needsAuthoritativeSync ||
        (cm.thought && !prev.thought && cmKind !== "bot_message") ||
        (cm.sticker && !prev.sticker) ||
        (cmAtts.length > (prev.attachments?.length || 0)) ||
        (traceArr.length > (prev.tool_trace?.length || 0)) ||
        (turnStepsArr.length > (prev.turn_steps?.length || 0)) ||
        (timelineArr.length > (prev.timeline?.length || 0)) ||
        (cmKind && normMsgKind(prev.kind) !== cmKind) ||
        (nextTtft > 0 && !prev.ttft_ms) ||
        (nextTps > 0 && !prev.tps) ||
        (cm.from_bot_name && !prev.from_bot_name) ||
        (cm.to_bot_name && !prev.to_bot_name) ||
        (cm.from_node && !prev.from_node) ||
        (cm.direction && !prev.direction)
      ) {
        const nextId = (cid && (String(prev.id || "").startsWith("pc-") || !prev.id)) ? cid : (prev.id.startsWith("reply-") && !cid.startsWith("reply-") ? cid : prev.id);
        cached[existingIdx] = {
          ...prev,
          id: nextId || prev.id,
          client_msg_id: cm.client_msg_id || prev.client_msg_id || (String(prev.id || "").startsWith("pc-") ? prev.id : undefined),
          text: String(cm.text !== undefined ? cm.text : (prev.text || "")).trim(),
          kind: cmKind,
          from_bot_id: cm.from_bot_id || prev.from_bot_id || "",
          from_bot_name: cm.from_bot_name || prev.from_bot_name || "",
          from_node: cm.from_node || prev.from_node || "",
          to_bot_id: cm.to_bot_id || prev.to_bot_id || "",
          to_bot_name: cm.to_bot_name || prev.to_bot_name || "",
          to_node: cm.to_node || prev.to_node || "",
          direction: cm.direction || prev.direction || "",
          sticker: String(cm.sticker !== undefined ? cm.sticker : (prev.sticker || "")).trim(),
          attachments: cmAtts.length ? cmAtts : (Array.isArray(cm.variants) ? cmAtts : (prev.attachments || [])),
          thought: cmKind === "bot_message" ? "" : String(cm.thought !== undefined ? cm.thought : (prev.thought || "")).trim(),
          choices: Array.isArray(cm.choices) ? cm.choices : (prev.choices || []),
          variants: cmVarLen > 0 ? cm.variants : prev.variants,
          active_variant_idx: cmActiveVar !== undefined ? cmActiveVar : prev.active_variant_idx,
          decision: cm.decision || prev.decision || "",
          ttft_ms: nextTtft,
          tps: nextTps,
          cache_hit_pct: nextCachePct,
          api_calls: nextApiCalls,
          order_seq: cmOrderSeq || prevOrderSeq || 0,
          created: cmCreatedMs || Number(prev.created || 0),
          time: cm.time || prev.time,
          tool_trace: traceArr,
          turn_steps: turnStepsArr,
          timeline: timelineArr,
        };
        changed = true;
      }
      continue;
    }
    cached.push({
      id: cid || `${role === "user" ? "pc" : "reply"}-${Date.now()}`,
      client_msg_id: cm.client_msg_id || undefined,
      role,
      kind: cmKind,
      text,
      thought: cmKind === "bot_message" ? "" : String(cm.thought || "").trim(),
      bot_id: botId,
      from_bot_id: cm.from_bot_id || "",
      from_bot_name: cm.from_bot_name || "",
      from_node: cm.from_node || "",
      to_bot_id: cm.to_bot_id || "",
      to_bot_name: cm.to_bot_name || "",
      to_node: cm.to_node || "",
      direction: cm.direction || "",
      sticker: String(cm.sticker || "").trim(),
      attachments: cmAtts,
      choices: Array.isArray(cm.choices) ? cm.choices : [],
      variants: Array.isArray(cm.variants) && cm.variants.length ? cm.variants : undefined,
      active_variant_idx: cm.active_variant_idx !== undefined && cm.active_variant_idx !== null ? Number(cm.active_variant_idx) : undefined,
      decision: cm.decision || "",
      ttft_ms: Number(cm.ttft_ms || cmMetrics.ttft_ms || 0),
      tps: Number(cm.tps || cmMetrics.tps || 0),
      cache_hit_pct: Number(cm.cache_hit_pct || cmMetrics.cache_hit_pct || 0),
      api_calls: Number(cm.api_calls || cmMetrics.api_calls || 0),
      order_seq: Number(cm.order_seq || cm.seq || 0),
      created: cmCreatedMs,
      source: "pc",
      pc_local: true,
      tool_trace: cmKind === "bot_message" ? [] : (Array.isArray(cm.trace) && cm.trace.length ? cm.trace : (Array.isArray(cm.tool_trace) ? cm.tool_trace : [])),
      turn_steps: cmKind === "bot_message" ? [] : (Array.isArray(cm.turn_steps) ? cm.turn_steps : []),
      timeline: cmKind === "bot_message" ? [] : (Array.isArray(cm.timeline) ? cm.timeline : []),
      time: cm.time || (cmCreatedMs > 0 ? new Date(cmCreatedMs).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }) : new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })),
    });
    changed = true;
  }
  if (changed) {
    saveLocalChatCache(sortPcMessagesDeterministically(cached), true);
  }
}

export function loadLocalChatCache() {
  if (Array.isArray(runtimeState.localMessagesCache)) return runtimeState.localMessagesCache;
  try {
    const p = getLocalChatCachePath();
    if (fs.existsSync(p)) {
      const parsed = JSON.parse(fs.readFileSync(p, "utf8"));
      if (Array.isArray(parsed?.messages)) {
        runtimeState.localMessagesCache = sanitizeMessagesList(parsed.messages);
        if (parsed?.messages_epoch) runtimeState.messagesEpoch = Number(parsed.messages_epoch) || 1;
        return runtimeState.localMessagesCache;
      }
    }
  } catch {}
  runtimeState.localMessagesCache = [];
  return runtimeState.localMessagesCache;
}

let chatCacheWriteTimer = null;
let chatCacheDirty = false;

export function flushChatCacheSync() {
  if (!chatCacheDirty || !Array.isArray(runtimeState.localMessagesCache)) return;
  try {
    const p = getLocalChatCachePath();
    fs.mkdirSync(path.dirname(p), { recursive: true });
    const tmp = `${p}.tmp`;
    const payload = JSON.stringify({
      updated_at: Date.now(),
      messages_epoch: runtimeState.messagesEpoch,
      messages: runtimeState.localMessagesCache,
    });
    fs.writeFileSync(tmp, payload, "utf8");
    fs.renameSync(tmp, p);
    chatCacheDirty = false;
  } catch {}
}

process.once("exit", flushChatCacheSync);

export function saveLocalChatCache(messages, bumpEpoch = false, immediate = false) {
  try {
    if (bumpEpoch) runtimeState.messagesEpoch = (Number(runtimeState.messagesEpoch) || 1) + 1;
    runtimeState.localMessagesCache = sanitizeMessagesList(messages);
    chatCacheDirty = true;
    if (chatCacheWriteTimer) {
      clearTimeout(chatCacheWriteTimer);
      chatCacheWriteTimer = null;
    }
    if (immediate) {
      flushChatCacheSync();
      return;
    }
    chatCacheWriteTimer = setTimeout(() => {
      chatCacheWriteTimer = null;
      flushChatCacheSync();
    }, 350);
  } catch {}
}

export {
  cloudFetch,
  beginBotRequest,
  isBotRequestAborted,
  waitForCloudBotReply,
  executeCloudChatTurn,
  switchCloudMessageVariant,
  handleBotArchiveLocalRoute,
  applyCloudSyncPayload,
  applyInboxSyncVersions,
} from "./lib-chat-sync.mjs";

