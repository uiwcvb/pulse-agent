import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HUD_SCRIPT = path.join(HERE, "hud-banner.ps1");

let activeBotName = (process.env.DS_CLAW_BOT_NAME || "主助理").trim();
let activeAccentColor = resolveBotAccent(activeBotName, process.env.DS_CLAW_ACCENT_COLOR);
let configSyncedForBot = "";
let hudProc = null;

export function resolveBotAccent(botName, customColor) {
  if (customColor && /^#[0-9a-fA-F]{6}$/.test(customColor)) return customColor;
  const name = String(botName || "");
  if (name.includes("鲸") || /whale|jingyu/i.test(name)) return "#4da6ff";
  if (name.includes("主助理") || /fox|companion|pc_engineer/i.test(name)) return "#f38ba8";
  return "#cba6f7";
}

export function getRuntimeConfigDir() {
  return process.env.CODEX_HOME ?? path.join(os.homedir(), ".codex");
}

export function getActiveBotName() {
  return activeBotName;
}

export function getActiveAccentColor() {
  return activeAccentColor;
}

export function syncNativeOverlayConfig(botName = activeBotName, accentColor = activeAccentColor) {
  try {
    const cfgDir = path.join(getRuntimeConfigDir(), "computer-use");
    fs.mkdirSync(cfgDir, { recursive: true });
    const cfgPath = path.join(cfgDir, "config.json");
    const cfg = {
      locale: "zh-CN",
      accentColor,
      strings: {
        usingComputer: `✦ ${botName} 正在操控你的电脑`,
        escToCancel: "按 Esc 停止操控",
      },
    };
    fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2), "utf8");
    configSyncedForBot = `${botName}|${accentColor}`;
  } catch {
    // Non-fatal if config dir is read-only
  }
}

export function setActiveBot(botName, customAccent) {
  const clean = String(botName || "").trim();
  if (!clean) return false;
  const nextAccent = resolveBotAccent(clean, customAccent);
  const changed = clean !== activeBotName || nextAccent !== activeAccentColor;
  activeBotName = clean;
  activeAccentColor = nextAccent;
  if (changed || configSyncedForBot !== `${activeBotName}|${activeAccentColor}`) {
    syncNativeOverlayConfig(activeBotName, activeAccentColor);
  }
  return changed;
}

export function ensureHudProcess() {
  if (process.platform !== "win32" || process.env.DS_CLAW_ENABLE_PS_HUD !== "1") return null;
  if (!fs.existsSync(HUD_SCRIPT)) return null;
  if (hudProc && hudProc.exitCode === null && !hudProc.killed) return hudProc;
  try {
    hudProc = spawn(
      "powershell.exe",
      ["-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", HUD_SCRIPT],
      { stdio: ["pipe", "ignore", "ignore"], windowsHide: true }
    );
    hudProc.on("error", () => { hudProc = null; });
    hudProc.on("exit", () => { hudProc = null; });
    return hudProc;
  } catch {
    hudProc = null;
    return null;
  }
}

export function hideHud() {
  if (!hudProc) return;
  try {
    if (hudProc.stdin && !hudProc.stdin.destroyed) {
      hudProc.stdin.write(JSON.stringify({ cmd: "exit" }) + "\n");
    }
    hudProc.kill();
  } catch {}
  hudProc = null;
}

export function notifyHud(actionText, duration = 2200, maybeBotName = null) {
  if (maybeBotName) setActiveBot(maybeBotName);
  else if (!configSyncedForBot) syncNativeOverlayConfig(activeBotName, activeAccentColor);

  const p = ensureHudProcess();
  if (!p || !p.stdin || p.stdin.destroyed) return;
  try {
    p.stdin.write(
      JSON.stringify({
        bot: activeBotName,
        action: actionText,
        accent: activeAccentColor,
        duration,
      }) + "\n"
    );
  } catch {}
}

syncNativeOverlayConfig(activeBotName, activeAccentColor);
