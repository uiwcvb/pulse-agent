/**
 * [Module Responsibility]
 * DSH DSB 插件主入口与原生 PC 桥接循环 (`index.js`)。
 * 负责：
 * 1. 插件元信息导出 (`name`, `version`, `description`) 与生命周期挂载 (`apply`)。
 * 2. 原生 PC 端心跳/收件箱 FIFO 顺序执行桥接 (`startNativePcBridge`)，含失败回执重试队列与指数退避重连。
 * 3. 伴侣侧边栏前端路由注册 (`registerUiRoutes`) 与 Sentinel 守护拉起。
 */
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  runtimeState,
  loadLocalConfig,
  getService,
  cloudFetch,
  applyInboxSyncVersions,
} from "./lib-chat-cache.mjs";
import { syncWorkspaceFileToPc } from "./lib-file-bridge.mjs";
import {
  netTelemetry,
  logSentinel,
  registerInboxAbortController,
} from "./lib-sentinel-net.mjs";
import {
  getDshPanorama,
  executeInteractiveCuaTool,
  searchDshSessions,
  inspectDshSessionDetail,
  dispatchToDshSession,
  proxyToDshHostIpc,
  isDshHostIpcAlive,
  launchDshDesktopApp,
  runLocalPcShellCommand,
  runLocalPcFsOperation,
  registerAllDshTools,
} from "./lib-dsh-sessions.mjs";
import {
  forwardRequest,
  isPrimaryGatewayListening,
  ensureDshHostIpcServer,
  ensureLocalLoopbackServer,
} from "./lib-local-routes.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export const name = "dsh-deepseek-claw";
export const version = "4.0.0";
export const description = "DSB · DeepSeek Bot Multi-Room Workspace & Session Bridge";
const API_ROUTES = ["/api/baike-plugin", "/api/dsh-baike-plugin", "/api/dsh-deepseek-claw"];
let connectionRouteRegistered = false;

async function executeInboxCommand(cmd) {
  const op = String(cmd?.op || "status").toLowerCase();
  const args = cmd?.args || {};
  try {
    if (op === "status" || op === "list_sessions") {
      return (!runtimeState.activeCtx && (await isDshHostIpcAlive()))
        ? ((await proxyToDshHostIpc("/local/sessions", {}, 10000))?.panorama || getDshPanorama(runtimeState.activeCtx, true))
        : getDshPanorama(runtimeState.activeCtx, true);
    }
    if (op === "search_sessions") return searchDshSessions(runtimeState.activeCtx, args.query, args.status_filter);
    if (op === "inspect_session") return await inspectDshSessionDetail(runtimeState.activeCtx, args.session_id || args.query);
    if (op === "wake_gui" || op === "wake" || op === "open_gui" || op === "launch_gui") return await launchDshDesktopApp(Boolean(args.wait), 20000);
    if (op === "dispatch_session" || op === "send_to_session" || op === "continue_session" || op === "new_session" || op === "send_message") {
      return await dispatchToDshSession(runtimeState.activeCtx, args);
    }
    if (op === "pc_shell" || op === "shell" || op === "run_bash" || op === "exec") return await runLocalPcShellCommand(args);
    if (op === "pc_fs" || op === "read_file" || op === "write_file" || op === "edit_file" || op === "list_dir" || op === "glob_files" || op === "grep_search") {
      return await runLocalPcFsOperation({ fs_op: args.fs_op || op, ...args });
    }
    if (op === "sync_uploaded_file") return await syncWorkspaceFileToPc({ payload: args, cloudFetch });
    if (op === "cua_desktop" || op === "desktop") {
      return await executeInteractiveCuaTool(
        args.action || args.tool || "list_windows",
        { ...(typeof args.args === "object" && args.args ? args.args : {}), ...args },
        args.bot_name || "主助理"
      );
    }
    return { ok: false, error: `不支持的桌面端操作 (${op})` };
  } catch (cmdErr) {
    return { ok: false, error: String(cmdErr?.message || cmdErr || "PC bridge command failed") };
  }
}

export function startNativePcBridge(ctx) {
  try {
    if (ctx) {
      runtimeState.activeCtx = ctx;
      if (!runtimeState.rootCtx) runtimeState.rootCtx = ctx;
    }
  } catch {}
  if (runtimeState.bridgeRunning) return;
  runtimeState.bridgeRunning = true;
  const gen = ++runtimeState.bridgeGeneration;
  logSentinel("INFO", `启动 PC 云端长轮询桥接 (gen=${gen}, device=${runtimeState.deviceId})`);

  (async () => {
    let lastStatusPushAt = 0;
    let lastStatusFp = "";
    let cachedHostAlive = false;
    let lastHostCheckAt = 0;
    let reconnectFailures = 0;
    const pendingInboxResults = [];

    const flushPendingResults = async () => {
      while (pendingInboxResults.length > 0 && gen === runtimeState.bridgeGeneration) {
        const item = pendingInboxResults[0];
        try {
          const ack = await cloudFetch("/api/dsh/result", "POST", item.payload, 15000);
          if (ack && ack.ok === false && ack.error) throw new Error(ack.error);
          pendingInboxResults.shift();
        } catch (err) {
          item.attempts += 1;
          logSentinel("WARN", `补发指令回执失败 (req=${item.reqId}, attempts=${item.attempts}): ${err?.message || err}`);
          if (item.attempts >= 5) pendingInboxResults.shift();
          break;
        }
      }
    };

    while (gen === runtimeState.bridgeGeneration) {
      let hadCommands = false;
      let abortedForReconnect = false;
      const inboxCtrl = new AbortController();
      registerInboxAbortController(inboxCtrl);
      try {
        await flushPendingResults();
        const nowMs = Date.now();
        if (nowMs - lastHostCheckAt > 4000) {
          cachedHostAlive = runtimeState.activeCtx ? true : await isDshHostIpcAlive();
          runtimeState.dshHostAlive = cachedHostAlive;
          lastHostCheckAt = nowMs;
        }
        if (!runtimeState.lastPanoramaCache || nowMs - (runtimeState.lastPanoramaAt || 0) > 15000) {
          if (!runtimeState.activeCtx && cachedHostAlive) {
            const hostPan = await proxyToDshHostIpc("/local/sessions", {}, 3500);
            runtimeState.lastPanoramaCache = hostPan?.panorama || getDshPanorama(null, false);
          } else {
            runtimeState.lastPanoramaCache = getDshPanorama(runtimeState.activeCtx, false);
          }
          runtimeState.lastPanoramaAt = nowMs;
        }
        const p = runtimeState.lastPanoramaCache;
        const topSid = p?.sessions?.[0]?.id || "";
        const topTurns = p?.sessions?.[0]?.turns_count || 0;
        const curFp = `${p?.sessions_count || 0}:${p?.running_count || 0}:${topSid}:${topTurns}`;
        const needFullStatus = !runtimeState.vpsOnline || (nowMs - lastStatusPushAt > 60000) || (curFp !== lastStatusFp);
        const fastHandshake = !runtimeState.vpsOnline || lastStatusPushAt === 0;
        const reqBody = {
          peer_id: runtimeState.deviceId,
          device_id: runtimeState.deviceId,
          device_name: runtimeState.deviceName,
          device_type: "pc",
          bridge_id: runtimeState.bridgeId,
          slim: !needFullStatus,
          wait_sec: fastHandshake ? 0 : 12,
          drain: true,
        };
        if (needFullStatus) {
          reqBody.status = p;
          lastStatusPushAt = nowMs;
          lastStatusFp = curFp;
        }
        const resp = await cloudFetch("/api/dsh/inbox?drain=1", "POST", reqBody, 22000, inboxCtrl.signal);
        runtimeState.bytesSent = (runtimeState.bytesSent || 0) + JSON.stringify(reqBody).length;
        if (resp?.ok) {
          reconnectFailures = 0;
          const wasOffline = !runtimeState.vpsOnline;
          runtimeState.vpsOnline = true;
          runtimeState.heartbeatCount = (runtimeState.heartbeatCount || 0) + 1;
          runtimeState.lastHeartbeatOkMs = Date.now();
          runtimeState.lastHeartbeatAt = new Date().toLocaleTimeString("zh-CN", { hour12: false });
          runtimeState.bytesReceived = (runtimeState.bytesReceived || 0) + JSON.stringify(resp).length;
          if (wasOffline) {
            logSentinel("INFO", `已成功连接云端中枢 (route=${netTelemetry.activeRoute}, rtt=${netTelemetry.lastRttMs}ms)`);
          }
          if (typeof resp.phone_online === "boolean") runtimeState.phoneOnline = resp.phone_online;
          if (typeof resp.phone_last_active_time === "string") runtimeState.phoneLastActiveTime = resp.phone_last_active_time;
          if (Array.isArray(resp.phone_last_3_dialogs)) runtimeState.phoneLast3Dialogs = resp.phone_last_3_dialogs;
          if (Array.isArray(resp.dsh_peers)) runtimeState.dshPeers = resp.dsh_peers;
          if (Array.isArray(resp.connected_devices)) runtimeState.connectedDevices = resp.connected_devices;
          if (resp.shared_memories) runtimeState.sharedMemories = { ...runtimeState.sharedMemories, ...resp.shared_memories };
          if (resp.sync_versions && typeof resp.sync_versions === "object") {
            applyInboxSyncVersions(resp.sync_versions, cloudFetch);
          }
          const cmdList = resp.commands || resp.inbox || [];
          hadCommands = cmdList.length > 0;
          // Execute inbox commands strictly in FIFO order so dependent steps never race
          for (const cmd of cmdList) {
            const reqId = cmd?.request_id || cmd?.req_id;
            if (!reqId) continue;
            const data = await executeInboxCommand(cmd);
            const resultPayload = {
              request_id: reqId,
              peer_id: runtimeState.deviceId,
              result: data,
              status: getDshPanorama(runtimeState.activeCtx),
            };
            try {
              const ack = await cloudFetch("/api/dsh/result", "POST", resultPayload, 15000);
              if (ack && ack.ok === false && ack.error) throw new Error(ack.error);
            } catch (resErr) {
              logSentinel("WARN", `回传指令结果失败，已入重试队列 (req=${reqId}): ${resErr?.message || resErr}`);
              if (pendingInboxResults.length >= 50) pendingInboxResults.shift();
              pendingInboxResults.push({ reqId, payload: resultPayload, attempts: 1 });
            }
          }
        } else {
          runtimeState.vpsOnline = false;
          reconnectFailures += 1;
        }
      } catch (err) {
        if (String(err?.message || err || "").includes("instant_reconnect")) {
          abortedForReconnect = true;
          lastStatusPushAt = 0;
          reconnectFailures = 0;
        } else {
          runtimeState.vpsOnline = false;
          reconnectFailures += 1;
        }
      } finally {
        registerInboxAbortController(null);
      }
      if (!abortedForReconnect) {
        let delayMs = hadCommands ? 60 : 500;
        if (!runtimeState.vpsOnline) {
          const baseMs = Math.min(30000, 1000 * Math.pow(2, Math.min(Math.max(reconnectFailures - 1, 0), 5)));
          const jitterMs = Math.floor(Math.random() * 400);
          delayMs = baseMs + jitterMs;
        }
        await new Promise((r) => setTimeout(r, delayMs));
      }
    }
  })();
}

async function ensureSentinelRunningFromDshHost() {
  const port = Number(loadLocalConfig().local_port || 18195);
  try {
    const r = await fetch(`http://127.0.0.1:${port}/status`, {
      method: "GET",
      headers: { Connection: "close" },
      signal: AbortSignal.timeout(1200),
    });
    if (r.ok) return;
  } catch {}
  const sentinelRoot = process.env.DS_CLAW_SENTINEL_ROOT || __dirname;
  const trayExe = path.join(sentinelRoot, "DeepSeekClawSentinel.exe");
  const sentinelMjs = path.join(sentinelRoot, "sentinel.mjs");
  try {
    if (fs.existsSync(trayExe)) {
      const child = spawn(trayExe, ["--minimized"], { detached: true, stdio: "ignore", cwd: sentinelRoot, windowsHide: true });
      child.unref();
      return;
    }
    if (fs.existsSync(sentinelMjs)) {
      const child = spawn(process.execPath, [sentinelMjs], { detached: true, stdio: "ignore", cwd: sentinelRoot, windowsHide: true });
      child.unref();
      return;
    }
  } catch {}
  ensureLocalLoopbackServer();
}

export function registerUiRoutes(ctx) {
  if (ctx) {
    runtimeState.activeCtx = ctx;
    runtimeState.rootCtx = ctx;
  }
  ensureDshHostIpcServer();
  ensureSentinelRunningFromDshHost().catch(() => {});
  const registerOnConnection = (conn) => {
    if (connectionRouteRegistered || typeof conn?.fetch?.register !== "function") return;
    try {
      for (const rPath of API_ROUTES) {
        conn.fetch.register({
          path: rPath,
          methods: ["POST", "GET"],
          requestBody: "buffered",
          fetch: async (request) => {
            try {
              const payload = request.method === "POST" ? await request.json().catch(() => ({})) : {};
              const data = await forwardRequest(payload.localPath || null, payload.cloudPath || null, payload.method || "GET", payload.body ?? null);
              return new Response(JSON.stringify(data), { status: 200, headers: { "content-type": "application/json" } });
            } catch (err) {
              return new Response(JSON.stringify({ ok: false, error: String(err?.message || err) }), { status: 200, headers: { "content-type": "application/json" } });
            }
          },
        });
      }
      connectionRouteRegistered = true;
    } catch {}
  };
  let hasInject = false;
  try { hasInject = typeof ctx?.inject === "function"; } catch {}
  if (hasInject) {
    try {
      ctx.inject(["connection"], (cCtx) => {
        try {
          if (!runtimeState.activeCtx) runtimeState.activeCtx = cCtx;
          registerOnConnection(getService(cCtx, "connection"));
        } catch {}
      });
    } catch {}
    try {
      ctx.inject(["sessionController", "agents", "sessions"], (sCtx) => {
        try {
          runtimeState.rootCtx = sCtx;
          runtimeState.activeCtx = sCtx;
          const sc = getService(sCtx, "sessionController");
          const ag = getService(sCtx, "agents");
          const ss = getService(sCtx, "sessions");
          if (sc) runtimeState.services.sessionController = sc;
          if (ag) runtimeState.services.agents = ag;
          if (ss) runtimeState.services.sessions = ss;
        } catch {}
      });
    } catch {}
    try {
      ctx.inject(["speechToText"], (spCtx) => {
        try {
          const stt = getService(spCtx, "speechToText");
          if (stt) runtimeState.services.speechToText = stt;
        } catch {}
      });
    } catch {}
  }
}

export function apply(ctx, config = {}) {
  if (config?.uiOnly === true) {
    registerUiRoutes(ctx);
    return;
  }
  const reg = (tCtx) => {
    try {
      const toolsSvc = getService(tCtx, "tools");
      if (!toolsSvc || typeof toolsSvc.register !== "function") return false;
      registerAllDshTools(toolsSvc);
      return true;
    } catch {
      return false;
    }
  };
  let hasInject = false;
  try { hasInject = typeof ctx?.inject === "function"; } catch {}
  if (!reg(ctx) && hasInject) {
    try {
      ctx.inject(["tools"], reg);
    } catch {}
  }
}

if (process.env.DS_CLAW_SENTINEL_ONLY === "1") {
  ensureLocalLoopbackServer();
}

export {
  handleLocalRequest,
  forwardRequest,
  isPrimaryGatewayListening,
  ensureDshHostIpcServer,
  ensureLocalLoopbackServer,
} from "./lib-local-routes.mjs";

export default { name, version, description, apply };
