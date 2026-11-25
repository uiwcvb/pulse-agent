#!/usr/bin/env node
// DeepSeek Claw Computer Use (ds-claw-cua) — Zero-Blackbox Native Windows Per-Monitor DPI Pure-Vision CUA MCP Server
import { spawn } from "node:child_process";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  getActiveAccentColor,
  getActiveBotName,
  hideHud,
  notifyHud,
  setActiveBot,
} from "./hud-overlay.mjs";
import { callWin32, killWin32Engine } from "./win32-engine.mjs";

const SERVER_NAME = "ds-claw-cua";
const SERVER_VERSION = "3.0.0";
const SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"];
const REQUEST_TIMEOUT_MS = Number(process.env.DS_CLAW_CUA_TIMEOUT_MS ?? 25_000);

let nextId = 1;
const lastWindowDims = new Map();
const text = (v) => ({ content: [{ type: "text", text: v == null ? "ok" : typeof v === "string" ? v : JSON.stringify(v) }] });
const obj = (properties, required) => ({ type: "object", properties, required });
const BOT_PARAM = {
  bot_name: {
    type: "string",
    description: "Optional active Bot name displayed on the desktop HUD banner (e.g. '司工' or '默认助手').",
  },
};
const WINDOW_ID = {
  window_id: { type: "number", description: "Window id from list_windows (0 = full virtual desktop)." },
  ...BOT_PARAM,
};

async function resolveWindowId(a = {}) {
  if (typeof a?.window_id === "number" && Number.isFinite(a.window_id)) return a.window_id;
  if (typeof a?.window_id === "string" && /^\d+$/.test(a.window_id.trim())) return Number(a.window_id.trim());
  const wins = await callWin32("list_windows", {}, REQUEST_TIMEOUT_MS);
  if (!Array.isArray(wins) || wins.length === 0) return 0;
  const q = String(a?.window_title || a?.title || a?.app || "").trim().toLowerCase();
  if (q) {
    const matched = wins.find(
      (w) => String(w.title || "").toLowerCase().includes(q) || String(w.app || "").toLowerCase().includes(q)
    );
    if (matched) return Number(matched.id);
    throw new Error(JSON.stringify({
      ok: false,
      error: "window_not_found",
      query: q,
      available_windows: wins.slice(0, 12).map((w) => ({ id: w.id, title: w.title, app: w.app })),
    }));
  }
  const visible = wins.find((w) => Number(w.id) > 0 && !w.is_minimized);
  return visible ? Number(visible.id) : 0;
}

async function getWindowDims(winId) {
  let dims = lastWindowDims.get(winId);
  if (!dims) {
    const w = await callWin32("get_window", { id: winId }, REQUEST_TIMEOUT_MS).catch(() => null);
    if (w?.width > 0 && w?.height > 0) {
      dims = { width: Number(w.width), height: Number(w.height) };
      lastWindowDims.set(winId, dims);
    }
  }
  if (!dims || !(dims.width > 0 && dims.height > 0)) {
    throw new Error(`Cannot determine dimensions for window #${winId}`);
  }
  return dims;
}

async function resolveWindowCoords(winId, a = {}) {
  let x = a.x != null ? Number(a.x) : null;
  let y = a.y != null ? Number(a.y) : null;
  const hasNorm = a.nx != null || a.ny != null || a.coord_space === "normalized_1000";
  const nx = a.nx != null ? Number(a.nx) : (a.coord_space === "normalized_1000" && x != null ? x : null);
  const ny = a.ny != null ? Number(a.ny) : (a.coord_space === "normalized_1000" && y != null ? y : null);
  if (hasNorm) {
    if (nx == null || ny == null || !Number.isFinite(nx) || !Number.isFinite(ny) || nx < 0 || nx > 1000 || ny < 0 || ny > 1000) {
      throw new Error(`Normalized coordinates (nx, ny) must both be finite numbers in [0, 1000], got (${a.nx}, ${a.ny})`);
    }
    const dims = await getWindowDims(winId);
    x = Math.round((nx / 1000) * dims.width);
    y = Math.round((ny / 1000) * dims.height);
  }
  return { x, y };
}

async function windowState(a = {}) {
  const winId = await resolveWindowId(a);
  notifyHud(`正在感知窗口 #${winId} 视觉画面`, 3200, a.bot_name);
  const st = await callWin32("get_window_state", {
    window_id: winId,
    include_screenshot: a.include_screenshot ?? true,
  }, REQUEST_TIMEOUT_MS);
  if (st?.width > 0 && st?.height > 0) {
    lastWindowDims.set(winId, { width: Number(st.width), height: Number(st.height) });
  }
  const content = [];
  if ((a.include_screenshot ?? true) && st?.screenshot_b64) {
    content.push({ type: "image", data: st.screenshot_b64, mimeType: st.mime || "image/jpeg" });
  }
  content.push({
    type: "text",
    text: [
      `bot: ${getActiveBotName()}`,
      `window_id: ${winId}`,
      `window: ${JSON.stringify(st?.window || {})}`,
      st?.focused_element ? `focused: ${st.focused_element}` : "",
      "纯视觉坐标规范：请结合图片边缘 0..1000 归一化刻度直接使用 nx, ny (0..1000) 传入 click/type_text/scroll/drag。",
    ].filter(Boolean).join("\n"),
  });
  return { content };
}

const TOOLS = [
  {
    name: "set_active_bot",
    description: "Set the active Bot identity and theme color displayed on the desktop CUA overlay.",
    inputSchema: obj({
      bot_name: { type: "string", description: "Bot display name." },
      accent_color: { type: "string", description: "Optional hex color." },
    }, ["bot_name"]),
    async run(a) {
      setActiveBot(a.bot_name, a.accent_color);
      notifyHud("已就绪，原生纯视觉 CUA 接管桌面", 3500);
      return text({ ok: true, active_bot: getActiveBotName(), accent_color: getActiveAccentColor() });
    },
  },
  {
    name: "list_windows",
    description: "List visible top-level windows plus window_id=0 (full virtual desktop) with physical Per-Monitor DPI bounds.",
    inputSchema: obj({ ...BOT_PARAM }, []),
    run: (a) => {
      notifyHud("正在扫描桌面窗口列表", 2600, a?.bot_name);
      return callWin32("list_windows", {}, REQUEST_TIMEOUT_MS).then(text);
    },
  },
  {
    name: "list_apps",
    description: "List visible windows/running apps.",
    inputSchema: obj({ ...BOT_PARAM }, []),
    run: (a) => callWin32("list_windows", {}, REQUEST_TIMEOUT_MS).then(text),
  },
  {
    name: "get_window",
    description: "Get bounds and title metadata for a window_id.",
    inputSchema: obj({ ...WINDOW_ID }, ["window_id"]),
    run: (a) => callWin32("get_window", { id: a.window_id }, REQUEST_TIMEOUT_MS).then(text),
  },
  {
    name: "launch_app",
    description: "Launch an application by executable name, path, or URL.",
    inputSchema: obj({ app: { type: "string" }, path: { type: "string" }, target: { type: "string" }, ...BOT_PARAM }, []),
    run: (a) => {
      const appTarget = String(a?.app || a?.path || a?.target || a?.url || "").trim();
      if (!appTarget) throw new Error("launch_app requires app, path, or target");
      notifyHud(`正在启动 ${path.basename(appTarget) || appTarget}`, 3600, a?.bot_name);
      return callWin32("launch_app", { app: appTarget }, REQUEST_TIMEOUT_MS).then(text);
    },
  },
  {
    name: "get_window_state",
    description: "Capture Per-Monitor DPI-aware window/desktop screenshot with outer 0..1000 ruler ticks.",
    inputSchema: obj({
      ...WINDOW_ID,
      include_screenshot: { type: "boolean" },
    }, []),
    run: windowState,
  },
  {
    name: "click",
    description: "Click by normalized 0..1000 (nx, ny) or window-relative pixel (x, y) via native Win32 SendInput.",
    inputSchema: obj({
      ...WINDOW_ID,
      nx: { type: "number" }, ny: { type: "number" },
      x: { type: "number" }, y: { type: "number" },
      click_count: { type: "number" },
      mouse_button: { type: "string", enum: ["left", "right", "middle"] },
    }, []),
    async run(a) {
      const winId = await resolveWindowId(a);
      const { x, y } = await resolveWindowCoords(winId, a);
      if (x == null || y == null) throw new Error("click requires normalized (nx, ny) or pixel (x, y)");
      notifyHud(`正在点击 (${Math.round(x)}, ${Math.round(y)})`, 3200, a.bot_name);
      await callWin32("click", {
        window_id: winId, x: Math.round(x), y: Math.round(y),
        mouse_button: a.mouse_button || "left", click_count: a.click_count || 1,
      }, REQUEST_TIMEOUT_MS);
      return text({ ok: true, window_id: winId, x: Math.round(x), y: Math.round(y) });
    },
  },
  {
    name: "type_text",
    description: "Click optional (nx, ny)/(x, y), optionally clear (Ctrl+A+BackSpace), and atomically paste Unicode text.",
    inputSchema: obj({
      ...WINDOW_ID,
      text: { type: "string" },
      nx: { type: "number" }, ny: { type: "number" },
      x: { type: "number" }, y: { type: "number" },
      clear: { type: "boolean" },
      submit: { type: "boolean" },
    }, ["text"]),
    async run(a) {
      const winId = await resolveWindowId(a);
      notifyHud(`正在输入文字「${String(a.text || "").slice(0, 14)}」`, 3600, a.bot_name);
      if (a.nx != null || a.ny != null || (a.x != null && a.y != null)) {
        const { x, y } = await resolveWindowCoords(winId, a);
        if (x != null && y != null) {
          await callWin32("click", { window_id: winId, x: Math.round(x), y: Math.round(y), mouse_button: "left", click_count: 1 }, REQUEST_TIMEOUT_MS);
        }
      }
      if (a.clear) {
        await callWin32("press_key", { window_id: winId, key: "Control_L+a" }, REQUEST_TIMEOUT_MS);
        await callWin32("press_key", { window_id: winId, key: "BackSpace" }, REQUEST_TIMEOUT_MS);
      }
      await callWin32("type_text", { window_id: winId, text: String(a.text ?? "") }, REQUEST_TIMEOUT_MS);
      if (a.submit) {
        await callWin32("press_key", { window_id: winId, key: "Return" }, REQUEST_TIMEOUT_MS);
      }
      return text({ ok: true, window_id: winId, length: String(a.text ?? "").length, submitted: Boolean(a.submit) });
    },
  },
  {
    name: "press_key",
    description: "Send key or hotkey chord via Win32 SendInput (e.g. Return, Tab, Escape, Control_L+a, Alt_L+F4).",
    inputSchema: obj({ ...WINDOW_ID, key: { type: "string" } }, ["key"]),
    async run(a) {
      const winId = await resolveWindowId(a);
      const key = String(a.key || a.keys || "Return").trim();
      notifyHud(`正在按键 [${key}]`, 3000, a.bot_name);
      await callWin32("press_key", { window_id: winId, key }, REQUEST_TIMEOUT_MS);
      return text({ ok: true, window_id: winId, key });
    },
  },
  {
    name: "scroll",
    description: "Scroll at (nx, ny)/(x, y) or window center.",
    inputSchema: obj({
      ...WINDOW_ID, nx: { type: "number" }, ny: { type: "number" },
      x: { type: "number" }, y: { type: "number" },
      scrollX: { type: "number" }, scrollY: { type: "number" },
      delta_x: { type: "number" }, delta_y: { type: "number" },
      pages: { type: "number" }, amount: { type: "number" },
      direction: { type: "string", enum: ["up", "down", "left", "right"] },
    }, []),
    async run(a) {
      const winId = await resolveWindowId(a);
      let { x, y } = await resolveWindowCoords(winId, a);
      if (x == null || y == null) {
        const dims = await getWindowDims(winId);
        x = Math.round(dims.width / 2);
        y = Math.round(dims.height / 2);
      }
      const dir = String(a.direction || "").trim().toLowerCase();
      const mag = Math.max(1, Math.min(20, Math.round(Number(a.pages || a.amount || 5) || 5)));
      const sx = a.scrollX ?? a.delta_x ?? (dir === "left" ? -mag : (dir === "right" ? mag : 0));
      const sy = a.scrollY ?? a.delta_y ?? (dir === "up" ? -mag : (dir === "down" ? mag : (sx !== 0 ? 0 : 5)));
      await callWin32("scroll", { window_id: winId, x: Math.round(x), y: Math.round(y), scrollX: Math.round(sx), scrollY: Math.round(sy) }, REQUEST_TIMEOUT_MS);
      return text({ ok: true, window_id: winId, x: Math.round(x), y: Math.round(y), scrollX: sx, scrollY: sy });
    },
  },
  {
    name: "drag",
    description: "Drag from normalized (from_nx, from_ny) / (nx, ny) or pixel (from_x, from_y) to (to_nx, to_ny) / (end_nx, end_ny) or (to_x, to_y).",
    inputSchema: obj({
      ...WINDOW_ID,
      nx: { type: "number" }, ny: { type: "number" },
      from_nx: { type: "number" }, from_ny: { type: "number" },
      to_nx: { type: "number" }, to_ny: { type: "number" },
      end_nx: { type: "number" }, end_ny: { type: "number" },
      from_x: { type: "number" }, from_y: { type: "number" },
      to_x: { type: "number" }, to_y: { type: "number" },
      coord_space: { type: "string" },
    }, []),
    async run(a) {
      const winId = await resolveWindowId(a);
      const start = await resolveWindowCoords(winId, {
        nx: a.from_nx ?? a.nx,
        ny: a.from_ny ?? a.ny,
        x: a.from_x,
        y: a.from_y,
        coord_space: a.coord_space,
      });
      const end = await resolveWindowCoords(winId, {
        nx: a.to_nx ?? a.end_nx,
        ny: a.to_ny ?? a.end_ny,
        x: a.to_x,
        y: a.to_y,
        coord_space: a.coord_space,
      });
      if (start.x == null || start.y == null || end.x == null || end.y == null) {
        throw new Error("drag requires start (from_nx/from_ny or from_x/from_y) and end (to_nx/to_ny or to_x/to_y) coordinates");
      }
      await callWin32("drag", {
        window_id: winId,
        from_x: Math.round(start.x), from_y: Math.round(start.y),
        to_x: Math.round(end.x), to_y: Math.round(end.y),
      }, REQUEST_TIMEOUT_MS);
      return text({ ok: true, window_id: winId, from_x: Math.round(start.x), from_y: Math.round(start.y), to_x: Math.round(end.x), to_y: Math.round(end.y) });
    },
  },
  {
    name: "activate_window",
    description: "Restore and bring window_id to foreground.",
    inputSchema: obj({ ...WINDOW_ID }, ["window_id"]),
    run: (a) => callWin32("activate_window", { window_id: a.window_id }, REQUEST_TIMEOUT_MS).then(text),
  },
  {
    name: "close_window",
    description: "Close window cleanly by window_id or title/app.",
    inputSchema: obj({ ...WINDOW_ID, title: { type: "string" }, window_title: { type: "string" }, app: { type: "string" } }, []),
    async run(a) {
      const winId = await resolveWindowId(a);
      if (winId > 0) {
        await callWin32("press_key", { window_id: winId, key: "Alt_L+F4" }, REQUEST_TIMEOUT_MS);
        return text({ ok: true, closed_window_id: winId });
      }
      throw new Error("Refusing Alt+F4 on desktop id 0; specify a window_id or title");
    },
  },
  {
    name: "reset_engine",
    description: "Restart native Win32 CUA worker.",
    inputSchema: obj({ ...BOT_PARAM }, []),
    async run(a) {
      if (a?.bot_name) setActiveBot(a.bot_name);
      const wasRunning = killWin32Engine("reset_engine");
      const wins = await callWin32("list_windows", {}, REQUEST_TIMEOUT_MS);
      return text({ ok: true, restarted: wasRunning, windows_count: wins.length });
    },
  },
];

const write = (m) => process.stdout.write(JSON.stringify(m) + "\n");
let chain = Promise.resolve();
function serialize(fn) {
  const run = chain.then(fn, fn);
  chain = run.then(() => {}, () => {});
  return run;
}

async function handle(msg) {
  const { id, method, params } = msg;
  const reply = (result) => { if (id != null) write({ jsonrpc: "2.0", id, result }); };
  const fail = (code, message) => { if (id != null) write({ jsonrpc: "2.0", id, error: { code, message } }); };
  try {
    switch (method) {
      case "initialize":
        return reply({
          protocolVersion: SUPPORTED_PROTOCOLS.includes(params?.protocolVersion) ? params.protocolVersion : SUPPORTED_PROTOCOLS.at(-1),
          capabilities: { tools: {} },
          serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
          instructions: `DeepSeek Claw Pure-Vision Win32 CUA: Per-Monitor DPI-aware Windows screenshot & SendInput engine.`,
        });
      case "tools/list":
        return reply({ tools: TOOLS.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })) });
      case "tools/call": {
        const tool = TOOLS.find((t) => t.name === params?.name);
        if (!tool) return fail(-32602, `unknown tool: ${params?.name}`);
        try { return reply(tool.name === "reset_engine" ? await tool.run(params.arguments ?? {}) : await serialize(() => tool.run(params.arguments ?? {}))); }
        catch (e) { return reply({ content: [{ type: "text", text: String(e?.message ?? e) }], isError: true }); }
      }
      case "ping":
        return reply({});
      default:
        return fail(-32601, `unknown method: ${method}`);
    }
  } catch (e) {
    fail(-32603, String(e?.message ?? e));
  }
}

async function selftest() {
  const windows = await callWin32("list_windows", {}, 25_000);
  if (!Array.isArray(windows) || windows.length === 0) throw new Error("list_windows returned empty");
  const st = await windowState({ window_id: windows[0].id, include_screenshot: true });
  const img = st.content.find((c) => c.type === "image");
  if (!img || !img.data || img.data.length < 100) throw new Error("no valid screenshot captured");
  console.log(`ok: ${windows.length} windows visible; captured ${img.data.length} base64 chars via native Win32 CUA`);
  killWin32Engine("selftest done");
}

if (process.argv.includes("--selftest")) {
  selftest().then(() => process.exit(0)).catch((e) => { console.error("FAIL:", e.message); process.exit(1); });
} else {
  let inBuf = "";
  process.stdin.on("data", (d) => {
    inBuf += d;
    let i;
    while ((i = inBuf.indexOf("\n")) >= 0) {
      const line = inBuf.slice(0, i).trim();
      inBuf = inBuf.slice(i + 1);
      if (!line) continue;
      try { handle(JSON.parse(line)); }
      catch { process.stderr.write(`[ds-claw-cua] unparsable: ${line.slice(0, 300)}\n`); }
    }
  });
  process.stdin.on("end", () => { killWin32Engine("client disconnected"); hideHud(); process.exit(0); });
  process.on("exit", () => { killWin32Engine("process exit"); hideHud(); });
  process.on("SIGTERM", () => process.exit(0));
}
