/**
 * [Module Responsibility]
 * DSH PC 本地与云端工具集 (`lib-dsh-pc-tools.mjs`)。
 * 从 `lib-dsh-sessions.mjs` 拆出，负责：
 * 1. Git Bash / PowerShell 执行器 (`runLocalPcShellCommand`) 及后台任务全生命周期跟踪 (`task_status` / `task_logs` / `task_kill`)。
 * 2. 本机文件读写编辑搜索 (`runLocalPcFsOperation`)。
 * 3. 本机 CUA 桌面控制代理 (`executeInteractiveCuaTool`) 与 DSH ToolRegistry 注册 (`registerAllDshTools`)。
 */
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { spawn, execFile } from "node:child_process";
import { callCuaTool } from "./cua-bridge.mjs";
import {
  getDefaultPcWorkspaceDir,
  resolveSmartPcPath,
  walkPcDir,
  globToRegExp,
  pullWorkspaceFileToPc,
  executePcCloudDesktopTool,
} from "./lib-file-bridge.mjs";
import { HOSTNAME, PC_DEVICE_ID, runtimeState, cloudFetch } from "./lib-chat-cache.mjs";
import { isDshHostIpcAlive, proxyToDshHostIpc } from "./lib-dsh-sessions.mjs";

let cachedGitBashPath;
export function detectSystemGitBash() {
  if (cachedGitBashPath !== undefined) return cachedGitBashPath;
  const candidates = [
    "C:\\Program Files\\Git\\bin\\bash.exe",
    "C:\\Program Files\\Git\\usr\\bin\\bash.exe",
    process.env.PROGRAMFILES ? path.join(process.env.PROGRAMFILES, "Git", "bin", "bash.exe") : "",
    process.env["PROGRAMFILES(X86)"] ? path.join(process.env["PROGRAMFILES(X86)"], "Git", "bin", "bash.exe") : "",
    process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, "Programs", "Git", "bin", "bash.exe") : "",
  ].filter(Boolean);
  for (const c of candidates) {
    if (/\\system32\\bash\.exe$/i.test(c)) continue;
    try { if (fs.existsSync(c)) { cachedGitBashPath = c; return c; } } catch {}
  }
  cachedGitBashPath = "";
  return "";
}

export { getDefaultPcWorkspaceDir, walkPcDir, globToRegExp };

export function resolvePcPath(rawPath) {
  return resolveSmartPcPath(rawPath, getDefaultPcWorkspaceDir());
}

export async function executeInteractiveCuaTool(action, args = {}, botName = "主助理") {
  const localRes = await callCuaTool(action, args, botName).catch((e) => ({ ok: false, error: String(e?.message || e), text: String(e?.message || e) }));
  if (localRes?.ok) return localRes;
  const errStr = String(localRes?.text || localRes?.error || "");
  if (!runtimeState.activeCtx && /EnumWindows|0x00000000/i.test(errStr) && (await isDshHostIpcAlive())) {
    const proxied = await proxyToDshHostIpc("/local/cua", { ...args, action, bot_name: botName }, 65000);
    if (proxied && typeof proxied === "object") return proxied;
  }
  return localRes;
}

function getBgTaskMap() {
  if (!(runtimeState.backgroundTasks instanceof Map)) {
    runtimeState.backgroundTasks = new Map();
  }
  return runtimeState.backgroundTasks;
}

function isPidAlive(pid) {
  const num = Number(pid);
  if (!Number.isFinite(num) || num <= 0) return false;
  try {
    process.kill(num, 0);
    return true;
  } catch (e) {
    return e?.code === "EPERM";
  }
}

export function manageLocalPcBackgroundTask(op, args = {}) {
  const tasks = getBgTaskMap();
  const taskId = String(args.task_id || args.id || "").trim();
  if (op === "task_status") {
    if (!taskId) {
      const list = Array.from(tasks.values()).map((t) => {
        if (t.status === "running" && t.pid && !isPidAlive(t.pid)) {
          t.status = "exited";
          t.finishedAt = t.finishedAt || Date.now();
        }
        return { ...t };
      });
      return { ok: true, count: list.length, tasks: list.slice(-50), hostname: HOSTNAME };
    }
    const task = tasks.get(taskId);
    if (!task) return { ok: false, error: `后台任务不存在: ${taskId}`, hostname: HOSTNAME };
    if (task.status === "running" && task.pid && !isPidAlive(task.pid)) {
      task.status = "exited";
      task.finishedAt = task.finishedAt || Date.now();
    }
    return { ok: true, ...task, hostname: HOSTNAME };
  }
  if (op === "task_logs") {
    const task = tasks.get(taskId);
    if (!task) return { ok: false, error: `后台任务不存在: ${taskId}`, hostname: HOSTNAME };
    let logText = "";
    try {
      if (task.log_file && fs.existsSync(task.log_file)) {
        const raw = fs.readFileSync(task.log_file, "utf8");
        const tailBytes = Math.max(512, Math.min(32000, Number(args.max_chars) || 12000));
        logText = raw.slice(-tailBytes);
      }
    } catch (e) {
      return { ok: false, error: `读取任务日志失败: ${String(e?.message || e)}`, task_id: taskId, hostname: HOSTNAME };
    }
    return { ok: true, task_id: taskId, status: task.status, pid: task.pid, log_file: task.log_file, logs: logText, hostname: HOSTNAME };
  }
  if (op === "task_kill") {
    const task = tasks.get(taskId);
    if (!task) return { ok: false, error: `后台任务不存在: ${taskId}`, hostname: HOSTNAME };
    if (!task.pid || !isPidAlive(task.pid)) {
      task.status = task.status === "running" ? "exited" : task.status;
      task.finishedAt = task.finishedAt || Date.now();
      return { ok: true, task_id: taskId, already_stopped: true, status: task.status, hostname: HOSTNAME };
    }
    return new Promise((resolve) => {
      execFile("taskkill.exe", ["/T", "/F", "/PID", String(task.pid)], { windowsHide: true }, (err, _out, stderr) => {
        if (err && isPidAlive(task.pid)) {
          return resolve({ ok: false, task_id: taskId, pid: task.pid, error: String(stderr || err.message || err), hostname: HOSTNAME });
        }
        task.status = "killed";
        task.finishedAt = Date.now();
        resolve({ ok: true, task_id: taskId, pid: task.pid, status: "killed", hostname: HOSTNAME });
      });
    });
  }
  return { ok: false, error: `未知后台任务操作: ${op}`, hostname: HOSTNAME };
}

export function runLocalPcShellCommand(args = {}) {
  const op = String(args.op || args.task_op || "").trim().toLowerCase();
  if (op === "task_status" || op === "task_logs" || op === "task_kill") {
    return Promise.resolve(manageLocalPcBackgroundTask(op, args));
  }
  const cmdStr = String(args.command || args.cmd || args.text || "").trim();
  if (!cmdStr) return Promise.resolve({ ok: false, error: "command 不能为空", hostname: HOSTNAME });
  const timeoutMs = Math.max(1000, Math.min(120000, Number(args.timeout || args.timeout_sec || 20) * 1000));
  const cwd = args.cwd ? resolvePcPath(args.cwd) : getDefaultPcWorkspaceDir();
  const reqShell = String(args.shell || "auto").trim().toLowerCase();
  const gitBash = (reqShell === "powershell" || reqShell === "pwsh") ? "" : detectSystemGitBash();
  const useBash = Boolean(gitBash);
  const exe = useBash ? gitBash : "powershell.exe";
  const exeArgs = useBash
    ? ["-lc", cmdStr]
    : ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; ${cmdStr}`];
  const env = useBash
    ? { ...process.env, MSYS_NO_PATHCONV: "1", MSYS2_ARG_CONV_EXCL: "*", LANG: "C.UTF-8", LC_ALL: "C.UTF-8" }
    : process.env;
  const shellUsed = useBash ? "git-bash" : "powershell";
  if (args.background === true) {
    try {
      const logDir = path.join(cwd, ".bg_logs");
      fs.mkdirSync(logDir, { recursive: true });
      const tid = `pc-bg-${crypto.randomBytes(4).toString("hex")}`;
      const logPath = path.join(logDir, `${tid}.log`);
      const outFd = fs.openSync(logPath, "a");
      const bgChild = spawn(exe, exeArgs, { cwd, env, detached: true, stdio: ["ignore", outFd, outFd], windowsHide: true });
      const taskRecord = {
        task_id: tid,
        pid: bgChild.pid,
        command: cmdStr.slice(0, 400),
        cwd,
        shell_used: shellUsed,
        log_file: logPath,
        status: "running",
        exit_code: null,
        startedAt: Date.now(),
        finishedAt: null,
      };
      const tasks = getBgTaskMap();
      tasks.set(tid, taskRecord);
      if (tasks.size > 100) {
        const oldestKey = tasks.keys().next().value;
        if (oldestKey) tasks.delete(oldestKey);
      }
      bgChild.on("exit", (code) => {
        if (taskRecord.status === "running") {
          taskRecord.status = code === 0 ? "completed" : "failed";
        }
        taskRecord.exit_code = code;
        taskRecord.finishedAt = Date.now();
      });
      bgChild.on("error", (err) => {
        taskRecord.status = "failed";
        taskRecord.error = String(err?.message || err);
        taskRecord.finishedAt = Date.now();
      });
      bgChild.unref();
      try { fs.closeSync(outFd); } catch {}
      return Promise.resolve({ ok: true, background: true, task_id: tid, pid: bgChild.pid, log_file: logPath, cwd, shell_used: shellUsed, hostname: HOSTNAME });
    } catch (e) {
      return Promise.resolve({ ok: false, error: String(e?.message || e), cwd, hostname: HOSTNAME });
    }
  }
  return new Promise((resolve) => {
    let settled = false;
    const child = execFile(
      exe,
      exeArgs,
      { cwd, env, maxBuffer: 4 * 1024 * 1024, windowsHide: true },
      (err, stdout, stderr) => {
        if (settled) return;
        settled = true;
        clearTimeout(killTimer);
        const exitCode = err && typeof err.code === "number" ? err.code : (err ? 1 : 0);
        resolve({
          ok: !err && exitCode === 0, hostname: HOSTNAME, device_id: PC_DEVICE_ID,
          cwd, shell_used: shellUsed,
          exit_code: exitCode, stdout: String(stdout || "").trim().slice(0, 16000),
          stderr: String(stderr || "").trim().slice(0, 4000),
          error: err ? String(err.message || err).slice(0, 500) : undefined,
        });
      }
    );
    const killTimer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try {
        if (child.pid) {
          execFile("taskkill.exe", ["/T", "/F", "/PID", String(child.pid)], { windowsHide: true }, () => {});
        } else { child.kill("SIGKILL"); }
      } catch {}
      resolve({
        ok: false, hostname: HOSTNAME, device_id: PC_DEVICE_ID,
        cwd, shell_used: shellUsed,
        exit_code: -1, stdout: "", stderr: "",
        error: `Command timed out after ${Math.round(timeoutMs / 1000)}s`,
      });
    }, timeoutMs);
    try { child.stdin?.end(); } catch {}
  });
}

export async function runLocalPcFsOperation(args = {}) {
  const fsOp = String(args.fs_op || args.op || "read_file").toLowerCase();
  const rawArgPath = String(args.path || args.file_path || args.filename || "").trim();
  const targetPath = resolvePcPath(rawArgPath);
  try {
    if (fsOp === "read_file" || fsOp === "read") {
      if (!fs.existsSync(targetPath) && rawArgPath && /^(?:workspace\/)?uploads\//i.test(rawArgPath.replace(/\\/g, "/"))) {
        await pullWorkspaceFileToPc({ cloudFetch, remotePath: rawArgPath, localTarget: targetPath }).catch(() => {});
      }
      if (!fs.existsSync(targetPath)) {
        return { ok: false, error: `文件不存在: ${targetPath}`, path: targetPath, hostname: HOSTNAME };
      }
      const stat = fs.statSync(targetPath);
      if (stat.isDirectory()) {
        const items = fs.readdirSync(targetPath);
        return { ok: true, path: targetPath, is_directory: true, entries: items.slice(0, 200), hostname: HOSTNAME };
      }
      if (Boolean(args.base64) || String(args.encoding || "").toLowerCase() === "base64") {
        if (stat.size > 50 * 1024 * 1024) {
          return { ok: false, error: `文件过大 (${stat.size} bytes > 50MB)`, path: targetPath, hostname: HOSTNAME };
        }
        const buf = fs.readFileSync(targetPath);
        return {
          ok: true, path: targetPath, size_bytes: buf.length,
          data_b64: buf.toString("base64"), hostname: HOSTNAME,
        };
      }
      const ext = path.extname(targetPath).toLowerCase();
      const imageMimes = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
      };
      if (imageMimes[ext]) {
        const buf = fs.readFileSync(targetPath);
        return {
          ok: true, path: targetPath, size_bytes: buf.length,
          _multimodal_b64: buf.toString("base64"), _multimodal_mime: imageMimes[ext], hostname: HOSTNAME,
        };
      }
      const raw = fs.readFileSync(targetPath, "utf8"); const lines = raw.split(/\r?\n/);
      const rawStart = args.start_line !== undefined && args.start_line !== null ? Number(args.start_line) : 1;
      const rawEnd = args.end_line !== undefined && args.end_line !== null ? Number(args.end_line) : rawStart + 250;
      if (rawStart < 1 || rawEnd < rawStart) {
        return {
          ok: false,
          error: `无效的行号范围: start_line=${rawStart}, end_line=${rawEnd}（要求 1 <= start_line <= end_line）`,
          path: targetPath, total_lines: lines.length, hostname: HOSTNAME,
        };
      }
      if (lines.length > 0 && rawStart > lines.length) {
        return {
          ok: false,
          error: `起始行号越界: start_line=${rawStart} 超出文件总行数 (${lines.length} 行)`,
          path: targetPath, total_lines: lines.length, hostname: HOSTNAME,
        };
      }
      const start = Math.max(1, rawStart); const end = Math.min(lines.length, rawEnd);
      const content = lines.slice(start - 1, end).join("\n").slice(0, 24000);
      return { ok: true, path: targetPath, start_line: start, end_line: end, total_lines: lines.length, content, hostname: HOSTNAME };
    }
    const dedupAdjacentLongLines = (txt) => {
      const lines = String(txt || "").split("\n"); const out = [];
      let dedupCount = 0;
      for (const ln of lines) {
        const s = ln.trim();
        if (s.length >= 12 && out.length > 0 && out[out.length - 1].trim() === s) {
          dedupCount++;
          continue;
        }
        out.push(ln);
      }
      return { text: out.join("\n"), dedupCount };
    };
    if (fsOp === "write_file" || fsOp === "write") {
      const content = String(args.content ?? ""); const append = Boolean(args.append);
      fs.mkdirSync(path.dirname(targetPath), { recursive: true });
      let dedupCount = 0;
      if (append && fs.existsSync(targetPath)) {
        const existing = fs.readFileSync(targetPath, "utf8");
        const sep = existing && !existing.endsWith("\n") && !content.startsWith("\n") ? "\n" : "";
        const merged = dedupAdjacentLongLines(existing + sep + content);
        dedupCount = merged.dedupCount;
        fs.writeFileSync(targetPath, merged.text, "utf8");
      } else { fs.writeFileSync(targetPath, content, "utf8"); }
      const resObj = { ok: true, path: targetPath, append, bytes_written: Buffer.byteLength(content, "utf8"), hostname: HOSTNAME };
      if (dedupCount > 0) resObj.deduplicated_adjacent_lines = dedupCount;
      return resObj;
    }
    if (fsOp === "edit_file" || fsOp === "edit") {
      if (!fs.existsSync(targetPath)) {
        return { ok: false, error: `文件不存在: ${targetPath}`, path: targetPath, hostname: HOSTNAME };
      }
      const raw = fs.readFileSync(targetPath, "utf8");
      const oldText = String(args.old ?? args.old_text ?? args.target ?? "");
      const newText = String(args.new ?? args.new_text ?? args.replacement ?? "");
      if (!oldText || !raw.includes(oldText)) {
        return { ok: false, error: `未在文件中找到待替换内容 (${targetPath})`, path: targetPath, hostname: HOSTNAME };
      }
      const replaceAll = Boolean(args.replace_all);
      const rawUpdated = replaceAll ? raw.split(oldText).join(newText) : raw.replace(oldText, () => newText);
      const merged = dedupAdjacentLongLines(rawUpdated);
      fs.writeFileSync(targetPath, merged.text, "utf8");
      const resObj = { ok: true, path: targetPath, updated: true, hostname: HOSTNAME };
      if (merged.dedupCount > 0) resObj.deduplicated_adjacent_lines = merged.dedupCount;
      return resObj;
    }
    if (fsOp === "list_dir" || fsOp === "ls") {
      if (!fs.existsSync(targetPath)) {
        return { ok: false, error: `目录不存在: ${targetPath}`, path: targetPath, hostname: HOSTNAME };
      }
      const maxDepth = Math.max(1, Math.min(5, Number(args.depth || 1)));
      const entries = walkPcDir(targetPath, maxDepth, 250);
      return { ok: true, path: targetPath, count: entries.length, entries, hostname: HOSTNAME };
    }
    if (fsOp === "glob_files" || fsOp === "glob") {
      if (!fs.existsSync(targetPath)) {
        return { ok: false, error: `目录不存在: ${targetPath}`, path: targetPath, hostname: HOSTNAME };
      }
      const pattern = String(args.pattern || "**/*").trim(); const rx = globToRegExp(pattern);
      const allEntries = walkPcDir(targetPath, 6, 1500); const matches = [];
      for (const item of allEntries) {
        if (item.type === "file" && (rx.test(item.name) || rx.test(path.basename(item.name)))) {
          matches.push(item.path);
          if (matches.length >= 100) break;
        }
      }
      return { ok: true, path: targetPath, pattern, count: matches.length, matches, hostname: HOSTNAME };
    }
    if (fsOp === "grep_search" || fsOp === "grep") {
      const query = String(args.query || args.pattern || "").trim();
      const grepPath = resolvePcPath(args.path || args.file_path || args.target || args.dir || args.filename || "");
      if (!query) return { ok: false, error: "query 不能为空", path: grepPath, hostname: HOSTNAME };
      if (!fs.existsSync(grepPath)) {
        return { ok: false, error: `路径不存在: ${grepPath}`, path: grepPath, hostname: HOSTNAME };
      }
      let rx = null;
      try { rx = new RegExp(query, "i"); } catch { rx = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"); }
      const skipExts = new Set([".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico", ".pdf", ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".pyc", ".db", ".sqlite", ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".wav"]);
      const stat = fs.statSync(grepPath);
      const filesToScan = stat.isFile()
        ? [grepPath]
        : walkPcDir(grepPath, 5, 800).filter((e) => e.type === "file").map((e) => e.path);
      const matches = [];
      for (const fp of filesToScan) {
        if (matches.length >= 80) break;
        if (skipExts.has(path.extname(fp).toLowerCase())) continue;
        try {
          const fstat = fs.statSync(fp);
          if (fstat.size > 1024 * 1024 || fstat.size === 0) continue;
          const rawBuf = fs.readFileSync(fp);
          if (rawBuf.subarray(0, 4096).includes(0)) continue;
          const lines = rawBuf.toString("utf8").split(/\r?\n/);
          for (let i = 0; i < lines.length; i++) {
            if (rx.test(lines[i])) {
              matches.push(`${fp}:${i + 1}: ${lines[i].slice(0, 200)}`);
              if (matches.length >= 80) break;
            }
          }
        } catch {}
      }
      return { ok: true, path: grepPath, query, count: matches.length, matches, hostname: HOSTNAME };
    }
    return { ok: false, error: `未知本地文件操作: ${fsOp}`, hostname: HOSTNAME };
  } catch (e) { return { ok: false, path: targetPath, error: String(e?.message || e), hostname: HOSTNAME }; }
}

export function registerAllDshTools(toolsSvc) {
  const outSchema = {
    schema: { type: "object", properties: { ok: { type: "boolean" }, summary: { type: "string" }, data: {} }, required: ["ok", "summary"] },
    render: (_a, v) => [{ type: "text", text: v.summary }],
  };
  toolsSvc.register({
    name: "blackboard",
    description: "跨端多 Bot 统一协作黑板（秒回不阻塞）：支持 @mention 唤醒 (@Bot名 / @self / @everyone)、看板认领与交接 (claim/complete/artifact_path)、按需审查门禁 (review_by)、定时闹钟 (at_time/delay_minutes/daily_time/exec_action) 与 0-Token 事件观察哨 (watch)。",
    timeoutMs: 30000,
    parameters: {
      type: "object", additionalProperties: false,
      properties: {
        action: { type: "string", enum: ["post", "reply", "read", "list", "claim", "complete", "review", "cancel", "write", "delete", "set_todos"] },
        topic_id: { type: "string" }, title: { type: "string" }, content: { type: "string" },
        mentions: { type: "array", items: { type: "string" } }, status_filter: { type: "string" },
        artifact_path: { type: "string" }, next_action: { type: "string" },
        review_by: { type: "string" }, require_review: { type: "boolean" }, verdict: { type: "string", enum: ["approved", "rejected"] },
        at_time: { type: "string" }, delay_minutes: { type: "number" }, delay_seconds: { type: "number" }, daily_time: { type: "string" },
        exec_action: { type: "string", enum: ["wake_bot", "ring_alarm", "notify_user"] },
        watch: { type: "object" }, key: { type: "string" }, limit: { type: "number" },
      },
      required: ["action"],
    },
    output: outSchema, isConcurrencySafe: (args) => args?.action === "read" || args?.action === "list",
    async execute(args) {
      const res = await cloudFetch("/api/blackboard", "POST", { ...args, from_bot_id: "pc_engineer", from_bot_name: "电脑端 DSH", from_source: "pc" }, 20000);
      return { ok: Boolean(res && !res.error), summary: JSON.stringify(res || {}, null, 2).slice(0, 3200), data: res || {} };
    },
  });
  toolsSvc.register({
    name: "peek_bots",
    description: "只读快速窥视工具：秒级读取所有或指定 Bot（手机端与电脑端）的当前运行状态、正在调用的工具及最近消息摘要，了解各 Bot 干到哪了。",
    timeoutMs: 15000,
    parameters: {
      type: "object", additionalProperties: false,
      properties: { target: { type: "string" }, limit: { type: "number" } },
    },
    output: outSchema, isConcurrencySafe: () => true,
    async execute(args) {
      const target = encodeURIComponent(String(args?.target || "all").trim());
      const limit = Math.max(1, Math.min(15, Number(args?.limit) || 5));
      const res = await cloudFetch(`/api/peek-bots?target=${target}&limit=${limit}`, "GET", null, 15000);
      return { ok: Boolean(res?.ok !== false), summary: JSON.stringify(res || {}, null, 2).slice(0, 3200), data: res || {} };
    },
  });
  toolsSvc.register({
    name: "cloud_desktop",
    description: "直接操控 24 小时常驻 Linux 云电脑（无头/虚拟桌面 + Chromium 浏览器 + bash 终端）：支持 exec、screenshot、open_url、browser_snapshot、browser_click、browser_type、browser_eval、click、type、key、scroll、status。",
    timeoutMs: 90000,
    parameters: {
      type: "object", additionalProperties: false,
      properties: {
        op: { type: "string", enum: ["status", "screenshot", "bash", "exec", "open", "navigate", "click", "type", "key", "scroll", "files", "list_files", "pull_file", "push_file", "send_screenshot"] },
        action: { type: "string", enum: ["status", "screenshot", "exec", "bash", "open_url", "browser_snapshot", "browser_click", "browser_type", "browser_eval", "click", "type", "key", "scroll"] },
        command: { type: "string" }, url: { type: "string" }, target: { type: "string" }, path: { type: "string" }, name: { type: "string" }, content: { type: "string" }, data_b64: { type: "string" },
        selector: { type: "string" }, ref: { type: "string" }, nx: { type: "number" }, ny: { type: "number" },
        text: { type: "string" }, script: { type: "string" }, x: { type: "number" }, y: { type: "number" },
        key: { type: "string" }, direction: { type: "string" }, submit: { type: "boolean" }, timeout: { type: "number" },
      },
    },
    output: outSchema, isConcurrencySafe: (args) => (args?.op || args?.action) === "status" || (args?.op || args?.action) === "browser_snapshot",
    async execute(args) {
      return await executePcCloudDesktopTool(args, cloudFetch);
    },
  });
  toolsSvc.register({
    name: "message_phone_bot",
    description: "跨端兼容工具：查看手机/平板端最近对话 (op='status'|'peek_dialogs')，或在共享黑板异步 @ 手机助理 (op='send_message'，毫秒级返回不阻塞)。",
    timeoutMs: 30000,
    parameters: {
      type: "object", additionalProperties: false,
      properties: {
        op: { type: "string", enum: ["status", "peek_dialogs", "list_bots", "send_message"] },
        limit: { type: "number" }, source: { type: "string", enum: ["phone", "pc"] },
        bot_id: { type: "string" }, message: { type: "string" },
      },
      required: ["op"],
    },
    output: outSchema, isConcurrencySafe: (args) => args?.op !== "send_message",
    async execute(args) {
      const op = args?.op || "send_message";
      if (op !== "send_message") {
        const limit = Math.max(1, Math.min(50, Number(args?.limit) || 5));
        const targetSource = args?.source === "pc" ? "pc" : "phone";
        const st = await cloudFetch(`/api/dsh/status?limit=${limit}&source=${targetSource}`, "GET");
        const dialogs = st?.dialogs || st?.phone_dialogs || st?.phone_last_3_dialogs || [];
        const lines = dialogs.map((d, i) => `${i + 1}. U: ${d.user || "(空)"} -> A(${d.bot_name || "主助理"}): ${d.assistant || "(待回复)"}`);
        return { ok: true, summary: `手机/平板状态: ${st.phone_online ? "在线" : "后台待命"} | 最近 ${dialogs.length} 条对话:\n${lines.join("\n") || "（暂无）"}`, data: st };
      }
      const msg = String(args?.message || "").trim();
      if (!msg) return { ok: false, summary: "message 不能为空", data: {} };
      const targetBot = String(args?.bot_id || "companion").trim();
      const res = await cloudFetch("/api/blackboard", "POST", {
        action: "post", title: msg.slice(0, 48), content: `@${targetBot} ${msg}`,
        mentions: [targetBot], from_bot_id: "pc_engineer", from_bot_name: "电脑端 DSH", from_source: "pc",
      }, 15000);
      return { ok: Boolean(res?.ok), summary: res?.ok ? `✅ 已在黑板发布并异步唤醒 @${targetBot} (topic_id=${res.topic?.id || ""})，稍后可用 peek_bots 查看进展。` : `黑板投递失败: ${res?.error}`, data: res || {} };
    },
  });
  toolsSvc.register({
    name: "cua_desktop", description: "本机 Windows 桌面操控工具 (`ds-claw-cua`)：枚举窗口、获取 UIA 组件树、点击、输入、按键、滚动、拖拽与等待。",
    timeoutMs: 90000,
    parameters: {
      type: "object", additionalProperties: false,
      properties: {
        action: { type: "string", enum: ["list_windows", "list_apps", "get_window_state", "click", "double_click", "right_click", "type_text", "set_value", "press_key", "scroll", "scroll_element", "drag", "perform_secondary_action", "launch_app", "activate_window", "close_window", "wait_for"] },
        bot_name: { type: "string" },
        window_id: { type: "number" },
        window_title: { type: "string" },
        title: { type: "string" },
        element_index: { type: "number" },
        ref: { type: "string" },
        nx: { type: "number" },
        ny: { type: "number" },
        x: { type: "number" },
        y: { type: "number" },
        coord_space: { type: "string" },
        click_count: { type: "number" },
        mouse_button: { type: "string", enum: ["left", "right", "middle"] },
        text: { type: "string" },
        value: { type: "string" },
        clear: { type: "boolean" },
        submit: { type: "boolean" },
        key: { type: "string" },
        direction: { type: "string", enum: ["up", "down", "left", "right"] },
        scrollX: { type: "number" },
        scrollY: { type: "number" },
        delta_x: { type: "number" },
        delta_y: { type: "number" },
        pages: { type: "number" },
        from_x: { type: "number" },
        from_y: { type: "number" },
        to_x: { type: "number" },
        to_y: { type: "number" },
        pattern: { type: "string" },
        timeout_seconds: { type: "number" },
        absent: { type: "boolean" },
        full_tree: { type: "boolean" },
        app: { type: "string" },
        filter: { type: "string" },
        include_screenshot: { type: "boolean" },
        include_text: { type: "boolean" },
      },
      required: ["action"],
    },
    output: outSchema,
    isConcurrencySafe: (args) => args?.action === "list_windows" || args?.action === "list_apps",
    async execute(args) {
      const action = args?.action || "list_windows";
      try {
        const res = await executeInteractiveCuaTool(action, args, args?.bot_name || "主助理");
        const detail = res?.error && !res?.text ? res.error : (res?.text || (res?.ok ? "(ok)" : "(failed)"));
        return { ok: Boolean(res?.ok), summary: `${res?.banner || "CUA"} · [${action}]\n${detail}`, data: res };
      } catch (err) { return { ok: false, summary: `桌面操控失败: ${String(err?.message || err)}`, data: {} }; }
    },
  });
  toolsSvc.register({
    name: "shared_memory", description: "手机↔平板↔电脑跨端统一共享长期记忆工具 (`list` | `remember` | `forget` | `search`)。",
    timeoutMs: 30000,
    parameters: {
      type: "object", additionalProperties: false,
      properties: {
        op: { type: "string", enum: ["list", "remember", "forget", "search", "update_impression", "update_traits"] },
        text: { type: "string" },
        pin_id: { type: "string" },
      },
      required: ["op"],
    },
    output: outSchema, isConcurrencySafe: (args) => args?.op === "list" || args?.op === "search",
    async execute(args) {
      const op = String(args?.op || "list").trim().toLowerCase();
      const res = await cloudFetch("/api/dsh/shared-memory", "POST", { op, source: "pc", text: args?.text || "", query: args?.text || "", content: args?.text || "", pin_id: args?.pin_id || args?.text || "" });
      if (!res?.ok) return { ok: false, summary: `共享记忆操作失败: ${res?.error}`, data: res || {} };
      return { ok: true, summary: `✅ 共享记忆 (${op}) 已同步，当前共 ${(res?.shared_memories?.pins || []).length} 条置顶记忆。`, data: res };
    },
  });
}
