/**
 * Native Windows Per-Monitor DPI Aware CUA Engine (`win32-engine.mjs`).
 * Spawns persistent `win32-host.ps1` STA worker communicating over clean newline JSON RPC.
 */
import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HOST_PS1 = path.join(HERE, "win32-host.ps1");

let psProc = null;
let readyPromise = null;
let reqSeq = 1;
const pending = new Map();

export function killWin32Engine(reason = "stopped") {
  const dying = psProc;
  psProc = null;
  readyPromise = null;
  for (const [, entry] of pending) {
    clearTimeout(entry.timer);
    entry.reject(new Error(`win32-engine stopped: ${reason}`));
  }
  pending.clear();
  if (dying) {
    try { dying.kill(); } catch {}
    return true;
  }
  return false;
}

function ensureWorker() {
  if (psProc && psProc.exitCode === null && !psProc.killed && readyPromise) {
    return readyPromise;
  }
  if (process.platform !== "win32") {
    return Promise.reject(new Error("ds-claw-cua native Win32 engine only runs on Windows"));
  }
  psProc = spawn("powershell.exe", ["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Sta", "-File", HOST_PS1], {
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  const selfProc = psProc;
  let buf = "";
  let resolvedReady = false;

  readyPromise = new Promise((resolve, reject) => {
    const readyTimer = setTimeout(() => {
      if (!resolvedReady) {
        killWin32Engine("startup timeout");
        reject(new Error("Native Win32 CUA PowerShell worker startup timeout"));
      }
    }, 20_000);

    selfProc.stdout.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        if (line === "DSB_WIN32_READY") {
          resolvedReady = true;
          clearTimeout(readyTimer);
          resolve(selfProc);
          continue;
        }
        let msg;
        try { msg = JSON.parse(line); } catch { continue; }
        const p = pending.get(msg.id);
        if (!p) continue;
        pending.delete(msg.id);
        clearTimeout(p.timer);
        if (msg.ok) p.resolve(msg.result);
        else p.reject(new Error(msg.error || "Win32 CUA error"));
      }
    });

    selfProc.stderr.on("data", (d) => {
      process.stderr.write(`[ds-claw-cua-win32] ${d}`);
    });

    const onAbort = (err) => {
      clearTimeout(readyTimer);
      if (psProc === selfProc) psProc = null;
      readyPromise = null;
      for (const [, p] of pending) {
        clearTimeout(p.timer);
        p.reject(err);
      }
      pending.clear();
      if (!resolvedReady) reject(err);
    };
    selfProc.on("error", onAbort);
    selfProc.on("exit", (code) => onAbort(new Error(`Win32 worker exited (${code})`)));
  });

  return readyPromise;
}

export async function callWin32(method, params = {}, timeoutMs = 20_000) {
  const proc = await ensureWorker();
  const id = reqSeq++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Win32 CUA ${method} timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    proc.stdin.write(JSON.stringify({ id, method, params }) + "\n");
  });
}
