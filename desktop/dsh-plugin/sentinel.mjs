/**
 * DeepSeek Claw Permanent Background Sentinel Daemon (`sentinel.mjs` v3.0)
 * - Exclusively owns `127.0.0.1:18197` (single-instance control port) AND `127.0.0.1:18195` (permanent local gateway).
 * - Never yields `18195`, eliminating Keep-Alive port-handoff deadlocks completely.
 * - Includes built-in self-heal loop verifying `18195` stays bound and logging structured diagnostics to `~/.dsh/sentinel.log`.
 */
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { logSentinel } from "./lib-sentinel-net.mjs";

process.env.DS_CLAW_SENTINEL_ONLY = "1";
process.title = "DeepSeek-Claw-Sentinel";

const DSH_HOME = path.join(os.homedir(), ".dsh");
const PID_FILE = path.join(DSH_HOME, "sentinel.pid");

function isPidAlive(pid) {
  if (!Number.isFinite(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err?.code === "EPERM";
  }
}

function recoverStaleLockfile() {
  try {
    if (!fs.existsSync(PID_FILE)) return;
    const raw = fs.readFileSync(PID_FILE, "utf8").trim();
    const oldPid = Number(raw);
    if (oldPid === process.pid) return;
    if (isPidAlive(oldPid)) return;
    fs.unlinkSync(PID_FILE);
    logSentinel("WARN", `发现并清理失效 PID 锁文件 (stale pid=${oldPid || raw || "empty"})`);
  } catch (err) {
    logSentinel("WARN", `检查 PID 锁文件异常: ${err?.message || err}`);
  }
}

function writePidLockfile() {
  try {
    fs.mkdirSync(DSH_HOME, { recursive: true });
    fs.writeFileSync(PID_FILE, String(process.pid), "utf8");
  } catch (err) {
    logSentinel("WARN", `写入 sentinel.pid 失败: ${err?.message || err}`);
  }
}

function cleanupPidLockfile() {
  try {
    if (!fs.existsSync(PID_FILE)) return;
    const owner = Number(fs.readFileSync(PID_FILE, "utf8").trim());
    if (owner === process.pid) fs.unlinkSync(PID_FILE);
  } catch {}
}

recoverStaleLockfile();

process.on("uncaughtException", (err) => {
  logSentinel("ERROR", `UncaughtException: ${err?.stack || err?.message || err}`);
});
process.on("unhandledRejection", (reason) => {
  logSentinel("WARN", `UnhandledRejection: ${reason?.stack || reason?.message || reason}`);
});
process.on("exit", cleanupPidLockfile);
process.on("SIGINT", () => { cleanupPidLockfile(); process.exit(0); });
process.on("SIGTERM", () => { cleanupPidLockfile(); process.exit(0); });

let gatewayModule = null;

const singleInstanceLock = http.createServer((req, res) => {
  res.setHeader("Connection", "close");
  if (req.url === "/shutdown" && req.method === "POST") {
    logSentinel("INFO", `收到 /shutdown 指令，准备优雅退出 (PID ${process.pid})`);
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ ok: true, shutting_down: true, pid: process.pid }));
    setTimeout(() => {
      cleanupPidLockfile();
      process.exit(0);
    }, 120);
    return;
  }
  const gwListening = Boolean(gatewayModule?.isPrimaryGatewayListening?.());
  res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify({
    ok: true,
    sentinel: true,
    version: "4.0.0",
    pid: process.pid,
    port18195_listening: gwListening,
  }));
});

singleInstanceLock.keepAliveTimeout = 1000;
singleInstanceLock.on("error", (err) => {
  if (err?.code === "EADDRINUSE") {
    logSentinel("INFO", `18197 已有活跃 Sentinel 实例在运行，当前副本退出 (PID ${process.pid})`);
    process.exit(0);
  }
  logSentinel("ERROR", `Sentinel 控制端口监听失败: ${err?.message || err}`);
  cleanupPidLockfile();
  process.exit(1);
});

singleInstanceLock.listen(18197, "127.0.0.1", async () => {
  writePidLockfile();
  logSentinel("INFO", `Sentinel 守护内核启动成功 (PID ${process.pid}, 控制端口 127.0.0.1:18197)`);
  try {
    gatewayModule = await import("./index.js");
    gatewayModule.ensureLocalLoopbackServer?.();
    setInterval(() => {
      try {
        if (gatewayModule && !gatewayModule.isPrimaryGatewayListening?.()) {
          logSentinel("WARN", "自检发现 18195 未处于监听态，立即触发自愈重绑");
          gatewayModule.ensureLocalLoopbackServer?.();
        }
      } catch {}
    }, 5000).unref();
  } catch (err) {
    logSentinel("ERROR", `加载 index.js 失败: ${err?.stack || err?.message || err}`);
    process.exit(1);
  }
});
