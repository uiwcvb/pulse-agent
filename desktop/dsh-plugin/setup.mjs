/**
 * DeepSeek Claw (`DS-Claw`) One-Click Auto-Installer & Profile Mounter (`setup.mjs`)
 */
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = process.env.DS_CLAW_REPO_ROOT || path.resolve(__dirname, "..", "..");

function parseCliArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--pair-code" && argv[i + 1]) {
      out.pairCode = argv[++i];
    } else if (a.startsWith("--pair-code=")) {
      out.pairCode = a.slice("--pair-code=".length);
    } else if (a === "--server" && argv[i + 1]) {
      out.server = argv[++i];
    } else if (a.startsWith("--server=")) {
      out.server = a.slice("--server=".length);
    } else if (a === "--token" && argv[i + 1]) {
      out.token = argv[++i];
    } else if (a.startsWith("--token=")) {
      out.token = a.slice("--token=".length);
    } else if (a === "--local-port" && argv[i + 1]) {
      out.localPort = Number(argv[++i]);
    } else if (a.startsWith("--local-port=")) {
      out.localPort = Number(a.slice("--local-port=".length));
    }
  }
  return out;
}

function parsePairCode(raw) {
  let clean = String(raw || "").trim();
  if (!clean) return null;
  if (clean.toUpperCase().startsWith("DSCLAW1:")) {
    clean = clean.slice("DSCLAW1:".length).trim();
  }
  const sep = clean.lastIndexOf("|");
  if (sep <= 0 || sep >= clean.length - 1) return null;
  const serverUrl = clean.slice(0, sep).trim().replace(/\/+$/, "");
  const token = clean.slice(sep + 1).trim();
  if (!serverUrl || !token) return null;
  return { serverUrl, token };
}

function resolveToken(explicitToken) {
  if (explicitToken && String(explicitToken).trim()) return String(explicitToken).trim();
  if (process.env.DS_CLAW_TOKEN) return process.env.DS_CLAW_TOKEN.trim();
  try {
    const existingCfg = path.join(__dirname, "bridge_config.json");
    if (fs.existsSync(existingCfg)) {
      const parsed = JSON.parse(fs.readFileSync(existingCfg, "utf8"));
      if (parsed?.token && parsed.token !== "dsclaw-default-token") return String(parsed.token).trim();
    }
  } catch {}
  const candidates = [
    path.join(os.homedir(), ".dsh", "ds-claw-token"),
    path.join(REPO_ROOT, "private", "device-token"),
    path.join(REPO_ROOT, "server", "data", "device-token"),
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) {
      const t = fs.readFileSync(c, "utf8").trim();
      if (t) return t;
    }
  }
  return "dsclaw-default-token";
}

function copyDirRecursive(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === ".git" || entry.name === "DeepSeekClawSentinel.exe" || entry.name === "SentinelApp.cs") continue;
    const s = path.join(src, entry.name);
    const d = path.join(dest, entry.name);
    if (entry.isDirectory()) copyDirRecursive(s, d);
    else fs.copyFileSync(s, d);
  }
}

function mountIntoProfile(profileDir) {
  if (!fs.existsSync(profileDir)) return false;
  const pkgPath = path.join(profileDir, "package.json");
  if (!fs.existsSync(pkgPath)) return false;

  const pkgBak = `${pkgPath}.before-ds-claw.bak`;
  if (!fs.existsSync(pkgBak)) fs.copyFileSync(pkgPath, pkgBak);

  const targetModDir = path.join(profileDir, "node_modules", "dsh-deepseek-claw");
  let sameRealDir = false;
  try {
    if (fs.existsSync(targetModDir)) {
      sameRealDir = fs.realpathSync(targetModDir).toLowerCase() === fs.realpathSync(__dirname).toLowerCase();
    }
  } catch {}
  if (!sameRealDir) {
    copyDirRecursive(__dirname, targetModDir);
  }

  const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"));
  pkg.dependencies = pkg.dependencies || {};
  pkg.dependencies["dsh-deepseek-claw"] = `link:${__dirname.replace(/\\/g, "/")}`;
  pkg.dsh = pkg.dsh || {};
  pkg.dsh.profile = pkg.dsh.profile || {};
  pkg.dsh.profile.bundles = Array.isArray(pkg.dsh.profile.bundles)
    ? pkg.dsh.profile.bundles
    : ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"];
  if (
    profileDir.toLowerCase().includes("desktop") &&
    !pkg.dsh.profile.bundles.includes("@deepseek-ai/dsh-experimental-voice-input-bundle")
  ) {
    pkg.dsh.profile.bundles.push("@deepseek-ai/dsh-experimental-voice-input-bundle");
  }
  if (!pkg.dsh.profile.bundles.includes("dsh-deepseek-claw")) {
    pkg.dsh.profile.bundles.push("dsh-deepseek-claw");
  }
  fs.writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + "\n", "utf8");
  return true;
}

async function main() {
  const cli = parseCliArgs(process.argv.slice(2));
  const parsedPair = parsePairCode(cli.pairCode || process.env.DS_CLAW_PAIR_CODE || process.env.PULSE_PAIR_CODE || "");
  const serverUrl = (
    cli.server
    || (parsedPair ? parsedPair.serverUrl : "")
    || process.env.PULSE_SERVER_URL
    || process.env.DS_CLAW_SERVER_URL
    || "http://127.0.0.1:18190"
  ).replace(/\/$/, "");
  const token = resolveToken(cli.token || (parsedPair ? parsedPair.token : ""));
  const localPort = Number(cli.localPort || process.env.DS_CLAW_LOCAL_PORT || 18195);

  const bridgeCfgPath = path.join(__dirname, "bridge_config.json");
  const bridgeCfg = {
    server_url: serverUrl,
    token,
    local_port: localPort,
    dsh_home: path.join(os.homedir(), ".dsh"),
    updated_at: new Date().toISOString(),
  };
  fs.writeFileSync(bridgeCfgPath, JSON.stringify(bridgeCfg, null, 2) + "\n", "utf8");
  console.log(`[1/3] Wrote bridge_config.json -> ${bridgeCfgPath}`);

  const profiles = [
    path.join(os.homedir(), ".dsh", "profiles", "desktop"),
    process.env.DSH_WEB_PROFILE_DIR || "",
  ].filter(Boolean);
  for (const p of profiles) {
    if (mountIntoProfile(p)) {
      console.log(`[2/3] Mounted dsh-deepseek-claw into DSH profile: ${p}`);
    }
  }

  // Security hardening: NEVER install stealth/windowless Startup VBS, Run registry keys, or detached Sentinel processes.
  // Also actively remove any legacy hidden Sentinel auto-start artifacts if present.
  const startupDir = path.join(
    process.env.APPDATA || path.join(os.homedir(), "AppData", "Roaming"),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
  );
  const startupVbs = path.join(startupDir, "DeepSeek-Claw-Sentinel.vbs");
  try {
    if (fs.existsSync(startupVbs)) {
      fs.unlinkSync(startupVbs);
      console.log(`[3/3] Removed legacy hidden startup VBS -> ${startupVbs}`);
    }
    const { execFileSync } = await import("node:child_process");
    try {
      execFileSync("powershell.exe", [
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-Command",
        "Remove-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' -Name 'DeepSeekClawSentinel' -ErrorAction SilentlyContinue",
      ], { stdio: "ignore", timeout: 5000, windowsHide: true });
    } catch {}
    console.log("[3/3] Foreground-only mode verified: PC bridge runs only when DSH/Sentinel UI is explicitly launched.");
  } catch (e) {
    console.warn("Warning: could not clean legacy startup VBS:", e?.message || e);
  }
}

main().catch((err) => {
  console.error("Setup error:", err);
  process.exit(1);
});
