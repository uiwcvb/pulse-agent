/**
 * [Module Responsibility]
 * DSH 会话全景扫描、跨会话调度与宿主机控制模块 (`lib-dsh-sessions.mjs`)。
 * 负责：
 * 1. DSH 本地 `.proj_cache` 会话历史解析、全景摘要 (`getDshPanorama`)、全文检索 (`searchDshSessions`) 与详情查看 (`inspectDshSessionDetail`)。
 * 2. 跨进程 Host IPC 代理 (`proxyToDshHostIpc`)、DSH 桌面端唤醒 (`launchDshDesktopApp`) 与跨会话指令派发 (`dispatchToDshSession`)。
 * 3. 受控的本机 PowerShell 命令执行 (`runLocalPcShellCommand`) 与本地文件系统结构化操作 (`runLocalPcFsOperation`)。
 * 4. 向 DSH ToolRegistry 注册全套跨端协同工具 (`registerAllDshTools`)。
 *
 * [Public Interface]
 * - `getDshPanorama(context, allowIpcProxy)`, `searchDshSessions(context, query, limit)`, `inspectDshSessionDetail(context, sessionId, maxTurns)`
 * - `dispatchToDshSession(context, opts)`, `proxyToDshHostIpc(action, payload, timeoutMs)`, `isDshHostIpcAlive()`, `launchDshDesktopApp()`
 * - `runLocalPcShellCommand(command, cwd, timeoutSec)`, `runLocalPcFsOperation(payload)`
 * - `registerAllDshTools(toolRegistry, context)`
 *
 * [Dependencies]
 * - Node.js 标准库: `node:fs`, `node:path`, `node:os`, `node:http`, `node:child_process`
 * - `./lib-chat-cache.mjs`: `HOSTNAME`, `PC_DEVICE_ID`, `PC_DEVICE_NAME`, `DSH_HOST_IPC_PORT`, `runtimeState`, `getService`, `findBotByIdOrName`, `executeCloudChatTurn`
 */
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import http from "node:http";
import crypto from "node:crypto";
import { spawn, execFile } from "node:child_process";
import {
  HOSTNAME,
  PC_DEVICE_ID,
  PC_DEVICE_NAME,
  DSH_HOST_IPC_PORT,
  runtimeState,
  loadLocalConfig,
  getService,
  findBotByIdOrName,
  cloudFetch,
  executeCloudChatTurn,
} from "./lib-chat-cache.mjs";

export function formatAgo(tsMs) {
  if (!tsMs) return "未知";
  const diffSec = Math.max(0, Math.floor((Date.now() - Number(tsMs)) / 1000));
  if (diffSec < 60) return diffSec < 15 ? "刚刚" : `${diffSec}秒前`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}分钟前`;
  const diffHr = Math.floor(diffMin / 60);
  return diffHr < 24 ? `${diffHr}小时前` : `${Math.floor(diffHr / 24)}天前`;
}
export function parseProjCacheSession(sid, fullPath, liveAgent, now) {
  const stat = fs.statSync(fullPath); const raw = JSON.parse(fs.readFileSync(fullPath, "utf8"));
  const rec = raw?.record || {}; const rows = rec?.rows || {};
  const rawTurns = Array.isArray(rows?.turnOutline?.val?.turns) ? rows.turnOutline.val.turns : [];
  const turns = rawTurns.map((t, idx) => ({
    turn: Number(t?.turn ?? idx + 1), seq: Number(t?.seq ?? 0), prompt: String(t?.prompt || "").trim(),
    response: String(t?.response || "").trim(),
  }));
  const isBlank = Boolean(rows?.sessionListMetadata?.val?.blank);
  const rawTitle = String(rows?.title?.val || rows?.titleInput?.val?.first?.text || turns[0]?.prompt?.slice(0, 48) || "").trim();
  if ((isBlank && !turns.length) || (!rawTitle && !turns.length) || (!turns.length && !liveAgent)) return null;
  if (["默认助手", "主助理", "DSB 主助理", "研究助手", "杠精", "司工", "探微", "审校", "云汐"].includes(rawTitle) && (!turns.length || !turns[0]?.prompt)) return null;
  const rawStats = rows?.sessionStats?.val || {}; const totals = rows?.tokenUsage?.val?.totals || {};
  const tokenUsage = {
    uncachedInputTokens: Number(totals.uncachedInputTokens || 0), outputTokens: Number(totals.outputTokens || 0),
    cacheReadTokens: Number(totals.cacheReadTokens || 0), cacheWriteTokens: Number(totals.cacheWriteTokens || 0),
  };
  tokenUsage.totalTokens = tokenUsage.uncachedInputTokens + tokenUsage.outputTokens + tokenUsage.cacheReadTokens + tokenUsage.cacheWriteTokens;
  const sessionStats = {
    turns: Number(rawStats.turns || turns.length || 0), steps: Number(rawStats.steps || 0),
    pendingCalls: rawStats.pendingCalls || {},
  };
  const pendingTools = Object.values(sessionStats.pendingCalls).map((c) => c?.name || c?.tool || "tool").filter(Boolean);
  const boundary = rows?.turnBoundary?.val || {}; let status = "idle";
  if (liveAgent?.status && liveAgent.status !== "idle") status = "running";
  else if (boundary.openTurnStartSeq != null && now - stat.mtimeMs < 600000) status = "running";
  const subCatalog = rows?.subagentCatalog?.val?.head?.values || [];
  const teamMembers = rows?.agentTeam?.val?.members || []; const subById = new Map();
  for (const c of [...subCatalog, ...teamMembers]) {
    const cid = String(c?.childId || c?.id || "").trim();
    if (!cid) continue;
    subById.set(cid, {
      id: cid.slice(0, 8), full_id: cid, name: String(c?.name || c?.label || "子代理").slice(0, 48),
      task: String(c?.description || c?.label || "").slice(0, 180), phase: String(c?.phase || "active"),
    });
  }
  const subagents = Array.from(subById.values()); const goalObj = rows?.goal?.val?.current?.goal || null;
  const goal = goalObj ? String(goalObj.objective || "").trim() : "";
  const cwd = rec?.identity?.cwd || liveAgent?.session?.meta?.cwd || "";
  const lastTurn = turns[turns.length - 1] || {};
  const lastPrompt = String(lastTurn.prompt || rows?.titleInput?.val?.first?.text || "").trim();
  const lastResponse = String(lastTurn.response || "").trim();
  const updatedAt = Number(rows?.sessionListMetadata?.val?.lastPromptAt || stat.mtimeMs || rec?.identity?.createdAt || 0);
  const summary = {
    id: sid, short_id: sid.replace(/^session-/, "").slice(0, 8), device_id: runtimeState.deviceId,
    device_name: runtimeState.deviceName, device_type: "pc", title: String(rawTitle || "未命名会话").slice(0, 90),
    workspace: cwd, workspace_name: cwd ? path.basename(cwd) : "default-workspace",
    status,
    live: Boolean(liveAgent), turns_count: sessionStats.turns, steps_count: sessionStats.steps,
    subagents_count: subagents.length,
    subagents,
    pending_tools: pendingTools,
    goal,
    tokenUsage,
    sessionStats,
    activity_summary: status === "running" ? `正在运行 (${sessionStats.steps} 步)` : `已完成 ${sessionStats.turns} 轮`,
    last_prompt: lastPrompt.slice(0, 240), last_response: lastResponse.slice(0, 320), updated_at: updatedAt,
    updated_ago: formatAgo(updatedAt),
  };
  runtimeState.sessionDetailById.set(sid, { session: summary, turns, goal: goalObj, subagents, tokenUsage, sessionStats });
  return summary;
}
export function getDshPanorama(ctx = null, force = false) {
  const now = Date.now();
  if (!force && runtimeState.cachedPanorama && now - runtimeState.cachedPanoramaAt < 1200) {
    return runtimeState.cachedPanorama;
  }
  const cfg = loadLocalConfig();
  const projDir = path.join(cfg.dsh_home || path.join(os.homedir(), ".dsh"), "storages", "session_projcache", "sessions");
  const liveAgentsBySid = new Map();
  try {
    for (const agent of getService(ctx || runtimeState.activeCtx, "agents")?.roots?.() || []) {
      if (agent?.session?.id) liveAgentsBySid.set(agent.session.id, agent);
    }
  } catch {}
  const sessions = []; let subagentsTotalCount = 0;
  let runningCount = 0;
  try {
    if (fs.existsSync(projDir)) {
      for (const f of fs.readdirSync(projDir)) {
        if (!f.endsWith(".json")) continue;
        const sid = f.replace(/\.json$/, "");
        if (!sid.startsWith("session-")) { subagentsTotalCount++; continue; }
        if (sid.startsWith("session-bot-root-") || sid.startsWith("session-dsb-permanent-")) continue;
        try {
          const item = parseProjCacheSession(sid, path.join(projDir, f), liveAgentsBySid.get(sid), now);
          if (item) {
            if (item.status === "running") runningCount++;
            sessions.push(item);
          }
        } catch {}
      }
    }
  } catch {}
  sessions.sort((a, b) => {
    if (a.status === "running" && b.status !== "running") return -1;
    if (a.status !== "running" && b.status === "running") return 1;
    return Number(b.updated_at || 0) - Number(a.updated_at || 0);
  });
  const panorama = {
    ok: true, pc_online: true, device_id: runtimeState.deviceId,
    device_name: runtimeState.deviceName, device_type: "pc", sentinel: runtimeState.bridgeId,
    sessions_count: sessions.length, running_count: runningCount, live_in_memory_count: liveAgentsBySid.size,
    subagents_total_count: subagentsTotalCount, sessions: sessions.slice(0, 100),
    last_3_dialogs: sessions.filter((s) => s.last_prompt || s.last_response).slice(0, 3).map((s) => ({
      session_id: s.short_id, device_name: runtimeState.deviceName, time: s.updated_ago,
      user: `[${s.title}] ${s.last_prompt}`,
      assistant: s.last_response || s.activity_summary,
    })),
    updated_at: now,
  };
  runtimeState.cachedPanorama = panorama;
  runtimeState.cachedPanoramaAt = now;
  return panorama;
}
export function searchDshSessions(ctx, query = "", statusFilter = "all") {
  const panorama = getDshPanorama(ctx, false); const q = String(query || "").trim().toLowerCase();
  const terms = q.split(/\s+/).filter(Boolean); const sf = String(statusFilter || "all").toLowerCase();
  const matched = (panorama.sessions || []).filter((s) => {
    if (sf === "running" && s.status !== "running") return false;
    if (sf === "subagents" && !(s.subagents_count > 0)) return false;
    if (!terms.length) return true;
    const turns = runtimeState.sessionDetailById.get(s.id)?.turns || [];
    const hay = [
      s.title, s.id, s.short_id, s.workspace_name, s.goal,
      ...turns.map((t) => `${t.prompt} ${t.response}`),
    ].join(" ").toLowerCase();
    return terms.every((term) => hay.includes(term));
  });
  return { ok: true, query: q, total_matched: matched.length, sessions: matched };
}
function resolveDshSessionFromPanorama(panorama, rawQuery) {
  const sessions = Array.isArray(panorama?.sessions) ? panorama.sessions : [];
  const qRaw = String(rawQuery || "").trim(); const q = qRaw.toLowerCase();
  const qClean = q.replace(/^session-/, "");
  if (!q || q === "latest" || q === "new" || q === "create") {
    const first = sessions[0] || null;
    return first
      ? { ok: true, session: first }
      : { ok: false, error: "未找到任何本机 DeepSeek Harness 会话" };
  }
  const exact = sessions.find((s) => {
    const sid = String(s.id || "").toLowerCase(); const sidClean = sid.replace(/^session-/, "");
    const shortId = String(s.short_id || "").toLowerCase(); const title = String(s.title || "").toLowerCase();
    return sid === q || sidClean === qClean || shortId === qClean || title === q;
  });
  if (exact) return { ok: true, session: exact };
  const prefixOrTitle = sessions.find((s) => {
    const sid = String(s.id || "").toLowerCase(); const sidClean = sid.replace(/^session-/, "");
    const shortId = String(s.short_id || "").toLowerCase(); const title = String(s.title || "").toLowerCase();
    return (
      sid.startsWith(q) ||
      (qClean.length >= 4 && sidClean.startsWith(qClean)) ||
      (qClean.length >= 4 && shortId.startsWith(qClean)) ||
      title.includes(q)
    );
  });
  if (prefixOrTitle) return { ok: true, session: prefixOrTitle };
  return {
    ok: false,
    error: `未找到匹配的本机 DSH 会话: "${qRaw}"（请使用 list_sessions 中的 short_id、完整 session_id 或标题，或传 "latest"）`,
    available_sessions: sessions.slice(0, 12).map((s) => ({
      id: s.id, short_id: s.short_id, title: s.title,
    })),
  };
}
export function extractTurnsFromLiveEvents(events = []) {
  const turns = []; let curTurn = null;
  for (const ev of events) {
    if (!ev || typeof ev !== "object") continue;
    if (ev.type === "user/message") {
      const txt = (ev.data?.message?.content || [])
        .filter((b) => b?.type === "text")
        .map((b) => b.text)
        .join("")
        .trim();
      if (txt) {
        if (curTurn) turns.push(curTurn);
        curTurn = {
          turn: turns.length + 1, seq: Number(ev.seq ?? 0), prompt: txt,
          response: "",
        };
      }
    } else if (ev.type === "assistant/message") {
      const txt = (ev.data?.message?.content || [])
        .filter((b) => b?.type === "text")
        .map((b) => b.text)
        .join("")
        .trim();
      if (txt) {
        if (!curTurn) {
          curTurn = {
            turn: turns.length + 1, seq: Number(ev.seq ?? 0), prompt: "",
            response: txt,
          };
        } else { curTurn.response = txt; }
      }
    }
  }
  if (curTurn) turns.push(curTurn);
  return turns;
}
export async function inspectDshSessionDetail(ctx, sessionIdOrQuery) {
  const targetCtx = ctx || runtimeState.activeCtx;
  if (!targetCtx && (await isDshHostIpcAlive())) {
    const proxied = await proxyToDshHostIpc("/local/infinite-chat/inspect_session", { session_id: sessionIdOrQuery }, 15000);
    if (proxied && typeof proxied === "object") return proxied;
  }
  const panorama = getDshPanorama(targetCtx, true);
  const resolved = resolveDshSessionFromPanorama(panorama, sessionIdOrQuery);
  if (!resolved.ok) return resolved;
  const matched = resolved.session;
  const detail = runtimeState.sessionDetailById.get(matched.id) || { session: { ...matched }, turns: [] };
  let turns = Array.isArray(detail.turns) ? [...detail.turns] : []; let liveStatus = matched.status;
  let recentEvents = [];
  if (targetCtx) {
    try {
      const agentsSvc = getService(targetCtx, "agents"); const sessionsSvc = getService(targetCtx, "sessions");
      const sessionController = getService(targetCtx, "sessionController");
      const liveAgent = agentsSvc?.get?.(matched.id);
      if (liveAgent?.status) { liveStatus = liveAgent.status === "idle" ? "idle" : "running"; }
      let liveEvents = null; const memSession = liveAgent?.session || sessionsSvc?.get?.(matched.id);
      if (memSession && typeof memSession.snapshotEvents === "function") {
        liveEvents = memSession.snapshotEvents();
      } else if (sessionController && typeof sessionController.inspect === "function") {
        const inspected = await sessionController.inspect(matched.id, AbortSignal.timeout(8000));
        if (Array.isArray(inspected?.events)) liveEvents = inspected.events;
      } else if (sessionController?.commands && typeof sessionController.commands.readSessionState === "function") {
        const state = await sessionController.commands.readSessionState(matched.id);
        if (Array.isArray(state?.events)) liveEvents = state.events;
      }
      if (Array.isArray(liveEvents) && liveEvents.length > 0) {
        recentEvents = liveEvents.slice(-12).map((ev) => ({
          seq: ev?.seq, type: ev?.type, data: ev?.data,
        }));
        const liveTurns = extractTurnsFromLiveEvents(liveEvents);
        if (liveTurns.length >= turns.length) { turns = liveTurns; }
      }
    } catch {}
  }
  const lastTurn = turns[turns.length - 1] || {};
  const enrichedSession = {
    ...detail.session,
    status: liveStatus, turns_count: Math.max(Number(detail.session?.turns_count || 0), turns.length),
    last_prompt: String(lastTurn.prompt || detail.session?.last_prompt || "").slice(0, 240),
    last_response: String(lastTurn.response || detail.session?.last_response || "").slice(0, 320),
  };
  return {
    ok: true,
    ...detail,
    session: enrichedSession,
    turns,
    turns_count: turns.length, last_error: runtimeState.lastAgentErrorBySid.get(matched.id) || null,
    recent_events: recentEvents,
  };
}
export function deepFreeze(obj) {
  if (obj && typeof obj === "object" && !Object.isFrozen(obj)) {
    Object.freeze(obj);
    for (const k of Reflect.ownKeys(obj)) deepFreeze(obj[k]);
  }
  return obj;
}
export async function proxyToDshHostIpc(localPath, body = {}, timeoutMs = 90000) {
  try {
    const res = await fetch(`http://127.0.0.1:${DSH_HOST_IPC_PORT}/api/dsh-deepseek-claw`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ localPath, method: "POST", body }),
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!res.ok) return null;
    return await res.json();
  } catch { return null; }
}
export async function isDshHostIpcAlive() {
  try {
    const res = await fetch(`http://127.0.0.1:${DSH_HOST_IPC_PORT}/health`, {
      method: "GET", signal: AbortSignal.timeout(1200),
    });
    return res.ok;
  } catch { return false; }
}
export async function launchDshDesktopApp(waitForHostIpc = false, maxWaitMs = 25000) {
  const dshHome = process.env.DSH_HOME || process.env.DEEPSEEK_HARNESS_HOME || "";
  const exeCandidates = [
    process.env.DSH_EXE_PATH || "",
    dshHome ? path.join(dshHome, "DeepSeek Harness.exe") : "",
    path.join(process.env.LOCALAPPDATA || "", "Programs", "DeepSeek Harness", "DeepSeek Harness.exe"),
    path.join(process.env.PROGRAMFILES || "", "DeepSeek Harness", "DeepSeek Harness.exe"),
    path.join(process.env["PROGRAMFILES(X86)"] || "", "DeepSeek Harness", "DeepSeek Harness.exe"),
  ].filter(Boolean);
  const batCandidates = [
    process.env.DSH_BAT_PATH || "",
    dshHome ? path.join(dshHome, "Start-DeepSeek-Harness.bat") : "",
  ].filter(Boolean);
  let launchedVia = "";
  if (await isDshHostIpcAlive()) {
    execFile(
      "powershell.exe",
      [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "$p = Get-Process 'DeepSeek Harness' -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1; if ($p) { (New-Object -ComObject WScript.Shell).AppActivate($p.Id) | Out-Null }",
      ],
      { windowsHide: true },
      () => {}
    );
    return { ok: true, already_running: true, host_ipc_ready: true, message: "DeepSeek Harness 已在运行并已唤醒窗口" };
  }
  for (const exePath of exeCandidates) {
    if (exePath && fs.existsSync(exePath)) {
      try {
        const child = spawn(exePath, [], {
          detached: true, stdio: "ignore", cwd: path.dirname(exePath),
          windowsHide: false,
        });
        child.unref();
        launchedVia = exePath;
        break;
      } catch {}
    }
  }
  if (!launchedVia) {
    for (const batCandidate of batCandidates) {
      if (fs.existsSync(batCandidate)) {
        try {
          const child = spawn("cmd.exe", ["/c", batCandidate], {
            detached: true, stdio: "ignore", cwd: path.dirname(batCandidate),
            windowsHide: true,
          });
          child.unref();
          launchedVia = batCandidate;
          break;
        } catch {}
      }
    }
  }
  if (!launchedVia) {
    return { ok: false, error: "未找到 DeepSeek Harness 启动路径，请设置 DSH_EXE_PATH / DSH_HOME 或将其安装到默认目录" };
  }
  if (!waitForHostIpc) {
    return { ok: true, launched: true, path: launchedVia, message: `已自动拉起 DeepSeek Harness (${launchedVia})` };
  }
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 700));
    if (await isDshHostIpcAlive()) {
      return { ok: true, launched: true, host_ipc_ready: true, path: launchedVia };
    }
  }
  return { ok: true, launched: true, host_ipc_ready: false, path: launchedVia, message: "已启动 DeepSeek Harness，正在初始化内部会话服务…" };
}
export async function dispatchToDshSession(ctx, args = {}) {
  const targetCtx = ctx || runtimeState.activeCtx;
  const prompt = String(args?.message || args?.prompt || args?.text || "").trim();
  if (!prompt) return { ok: false, error: "指令不能为空" };
  if (!targetCtx) {
    let ipcRes = await proxyToDshHostIpc("/local/infinite-chat/dispatch_session", args, 24000);
    if (ipcRes && typeof ipcRes === "object") return ipcRes;
    const launchRes = await launchDshDesktopApp(true, 18000);
    if (!launchRes.ok) return launchRes;
    await new Promise((r) => setTimeout(r, 1200));
    ipcRes = await proxyToDshHostIpc("/local/infinite-chat/dispatch_session", args, 24000);
    if (ipcRes && typeof ipcRes === "object") return ipcRes;
    return {
      ok: false, launched_dsh: true, error: "小启动器已自动拉起 DeepSeek Harness 客户端，但会话引擎仍在加载中，请稍候几秒再试。",
    };
  }
  const sessionController = getService(targetCtx, "sessionController");
  const agentsSvc = getService(targetCtx, "agents"); const sessionsSvc = getService(targetCtx, "sessions");
  const rawTarget = String(args?.session_id || args?.sessionId || args?.query || "").trim();
  let targetSession = null; let targetSid = "";
  if ((rawTarget === "new" || rawTarget === "create") && sessionController && typeof sessionController.createSession === "function") {
    try {
      const created = await sessionController.createSession({ cwd: args?.cwd || getDefaultPcWorkspaceDir() });
      targetSid = String(created?.sessionId || created?.id || "");
      if (targetSid) targetSession = { id: targetSid, title: "新建会话", status: "idle" };
    } catch {}
  }
  if (!targetSid) {
    const panorama = getDshPanorama(targetCtx, true);
    const resolved = resolveDshSessionFromPanorama(panorama, rawTarget);
    if (!resolved.ok) return resolved;
    targetSession = resolved.session; targetSid = targetSession?.id || "";
  }
  if (!targetSid) return { ok: false, error: "未找到匹配的本机 DSH 会话" };
  let agent = agentsSvc?.get?.(targetSid) || null;
  if (!agent && sessionController) {
    try {
      if (typeof sessionController.resolveAgent === "function") {
        const r = await sessionController.resolveAgent(targetSid);
        if (r?.error) return { ok: false, error: `唤醒会话 ${targetSid} 失败: ${r.error.message || JSON.stringify(r.error)}` };
        agent = r?.agent || (typeof r?.followup === "function" ? r : null);
      } else if (typeof sessionController.agents?.resolveAgent === "function") {
        const r = await sessionController.agents.resolveAgent(targetSid);
        agent = r?.agent || (typeof r?.followup === "function" ? r : null);
      }
    } catch (err) { return { ok: false, error: `解析会话 ${targetSid} 异常: ${String(err?.message || err)}` }; }
  }
  if (!agent && (!sessionController || typeof sessionController.prompt !== "function")) {
    return { ok: false, error: `会话 ${targetSid} 尚未就绪` };
  }
  let session = agent?.session || sessionsSvc?.get?.(targetSid) || null;
  const firstSeq = Number(session?.seq || 0);
  const isAgentBusy = Boolean(agent?.status && agent.status !== "idle") || targetSession?.status === "running";
  const mode = args?.mode ? (args.mode === "steer" ? "steer" : "followup") : (isAgentBusy ? "steer" : "followup");
  const shouldWait = args?.wait === true;
  try {
    if (sessionController && typeof sessionController.prompt === "function") {
      const pTask = sessionController.prompt({
        sessionId: targetSid, requestId: crypto.randomUUID(),
        content: [{ type: "text", text: prompt }], mode,
      });
      if (!shouldWait) {
        pTask.catch(() => {});
        await Promise.race([pTask.then(() => true).catch(() => false), new Promise((r) => setTimeout(r, 90))]);
      } else {
        await Promise.race([pTask, new Promise((_, rej) => setTimeout(() => rej(new Error("prompt ack timeout")), 15000))]);
      }
      if (!agent) agent = agentsSvc?.get?.(targetSid) || null;
      if (!session) session = agent?.session || sessionsSvc?.get?.(targetSid) || null;
    } else if (agent && typeof agent.followup === "function") {
      const msg = deepFreeze({
        id: crypto.randomUUID(), role: "user",
        content: [{ type: "text", text: prompt }], source: { kind: "user" },
      });
      if (mode === "steer" && typeof agent.steer === "function") agent.steer(msg);
      else agent.followup(msg);
    } else { return { ok: false, error: `会话 ${targetSid} 尚未就绪` }; }
  } catch (err) { return { ok: false, error: `向会话 ${targetSid} 投递指令失败: ${String(err?.message || err)}` }; }
  if (!shouldWait) {
    return {
      ok: true, pc_online: true, dispatched: true, mode_used: mode, preempted: Boolean(isAgentBusy && mode === "steer"),
      status: "running", session_id: targetSid, short_id: targetSid.replace(/^session-/, "").slice(0, 8),
      title: targetSession?.title || targetSid.slice(0, 12),
      assistant_reply: `✅ 已${isAgentBusy && mode === "steer" ? "即时打断插话 (steer)" : "异步派发"}至会话「${targetSession?.title || targetSid.slice(0, 12)}」：「${prompt.slice(0, 60)}」`,
    };
  }
  const reqTimeoutMs = Number(args?.timeout_ms) || (Number(args?.timeout_sec) ? Number(args.timeout_sec) * 1000 : 15000);
  const waitMs = Math.min(60000, Math.max(2000, reqTimeoutMs));
  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 350));
    session = agent?.session || sessionsSvc?.get?.(targetSid) || session;
    const curSeqNow = Number(session?.seq || 0);
    if (agent && agent.status === "idle" && curSeqNow > firstSeq + 1) { break; }
  }
  session = agent?.session || sessionsSvc?.get?.(targetSid) || session;
  let replyText = ""; let userEchoConfirmed = false;
  const curSeq = Number(session?.seq || 0);
  for (let sq = firstSeq; sq < curSeq; sq++) {
    try {
      const ev = session.eventAt(sq);
      if (ev?.type === "user/message") {
        userEchoConfirmed = true;
      } else if (ev?.type === "assistant/message") {
        const txt = (ev.data?.message?.content || []).filter((b) => b?.type === "text").map((b) => b.text).join("").trim();
        if (txt) replyText = txt;
      }
    } catch {}
  }
  const stillRunning = !agent || agent.status !== "idle" || (!replyText && curSeq <= firstSeq + 1);
  return {
    ok: true, pc_online: true, dispatched: true, mode_used: mode, preempted: Boolean(isAgentBusy && mode === "steer"),
    user_message_recorded: userEchoConfirmed || curSeq > firstSeq, seq_before: firstSeq, seq_after: curSeq,
    status: stillRunning ? "running" : "completed", session_id: targetSid,
    short_id: targetSid.replace(/^session-/, "").slice(0, 8),
    title: targetSession?.title || targetSid.slice(0, 12),
    assistant_reply: replyText || (stillRunning
      ? `✅ 指令已投递至会话「${targetSession?.title || targetSid.slice(0, 12)}」(seq: ${firstSeq} -> ${curSeq}) 并正在后台执行中，可稍后用 inspect_session 查看回复。`
      : "已在会话中执行完毕。"),
  };
}
runtimeState.getDshPanorama = getDshPanorama;

export {
  detectSystemGitBash,
  getDefaultPcWorkspaceDir,
  walkPcDir,
  globToRegExp,
  resolvePcPath,
  executeInteractiveCuaTool,
  manageLocalPcBackgroundTask,
  runLocalPcShellCommand,
  runLocalPcFsOperation,
  registerAllDshTools,
} from "./lib-dsh-pc-tools.mjs";
