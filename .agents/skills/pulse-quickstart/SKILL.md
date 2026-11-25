---
name: pulse-quickstart
description: Full-stack quickstart skill to deploy and launch Pulse Agent (server, optional VPS + 24/7 Cloud Desktop, non-root Android Shizuku/ADB client, and PC companion).
---

# Pulse Agent Full-Stack Quickstart Skill (`pulse-quickstart`)

Use this skill when a user asks an AI coding assistant to deploy, configure, or run the entire **Pulse Agent** system from this repository.

> Tip for non-technical phone users: If the user simply wants to type an **LLM API Key** into the Android app and let the in-app guide AI deploy a blank Linux VPS + 24/7 Cloud Desktop via SSH, tell them they can install the APK and do everything directly inside the first-run onboarding wizard!

---

## Strict Guardrails

1. **Zero Secret Leakage**: Never read/print real API keys, `device-token`, SSH passwords, private keys, or real VPS IPs into repository files, public docs, or transcripts. Always store secrets in `.env` or `private/` (git-ignored).
2. **Ask Before Remote Installs**: Ask the user which target topology they want before running package installs, Docker/systemd edits, or remote SSH deployments.

---

## Step 0: Collect 3 Scoping Choices

Before installing dependencies, confirm with the user:
1. **Deployment Mode**:
   - **Local Dev (`127.0.0.1:18190`)**: Fast local tryout on PC/Mac/Linux.
   - **Linux VPS 24/7 Public Hub**: Always-on server + optional 24/7 Cloud Desktop (`Xvfb + Chromium + Tmux`).
2. **Components to Enable**:
   - Hub Server (required) + Cloud Desktop (optional, Linux host) + Android App + PC Companion (`pulse-pc-bootstrap`).
3. **LLM Upstream**:
   - Provider (DeepSeek / OpenRouter / SiliconFlow / Gemini / OpenAI / custom OpenAI-compatible). Let the user enter the API key into `.env` or the app UI directly.

---

## Step 1: Environment Doctor Check & DSH Sidecar Fallback

Check runtime prerequisites:
- `python --version` (`>= 3.11`)
- `node --version` (`>= 20`, optional if using Python brain fallback)
- Free local ports: `18190` (FastAPI Hub), `18199` (optional Node DSH Sidecar), `18193` (optional Cloud Desktop).

### Critical Dependency Check (`@deepseek-ai/dsh-agent-loop`)
`server/dsh_brain/package.json` references internal `@deepseek-ai/*` packages for the Node DSH Sidecar (`BRAIN_ENGINE=dsh`).
- Test whether `cd server/dsh_brain && npm install` succeeds cleanly if the user wants `dsh` engine.
- **Zero-Block Fallback**: If npm registry access to `@deepseek-ai/*` is unavailable for external/open-source users, set **`BRAIN_ENGINE=legacy`**! The built-in Python LLM engine + 14 canonical tools + memory/compaction/blackboard work out-of-the-box with zero Node sidecar dependency.

---

## Step 2: Modular Startup & Real Health Verification

### Module A — Core Hub Server (`18190`)
```bash
cd server
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PS:  .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Set BRAIN_ENGINE=legacy (or dsh if Sidecar installed & running on :18199)
uvicorn main:app --host 127.0.0.1 --port 18190
```
**Real Verification**:
```bash
curl -fsS http://127.0.0.1:18190/health
curl -fsS http://127.0.0.1:18190/api/transport | head -c 120
```
Verify `/health` returns `200` and `/api/transport` returns a RSA `public_key`.

### Module B — Linux VPS Production Setup (Optional)
- Review [`scripts/deploy_vps.py`](../../../scripts/deploy_vps.py) and configure local git-ignored `private/deploy.json` if using automated deployment, or configure `proactive-agent.service` + Nginx reverse proxy under `/proactive-agent/` with WebSocket support:
  - Must pass `proxy_http_version 1.1`, `Upgrade $http_upgrade`, `Connection "upgrade"`, `proxy_buffering off`.
  - Set `DS_CLAW_PUBLIC_URL` and `PUBLIC_BLOCKED_HOSTS` in service environment (prevents Cloud Computer SSRF against host IP).

### Module C — 24/7 Cloud Desktop (Optional, Linux Host)
- Controlled account `proactive-desktop`, Xvfb `:99`, Openbox, Tmux, and Playwright Chromium on `127.0.0.1:18193` (accessible over local HTTP or SSH tunnel via `server/core/cloud_computer_deployer.py`).
- Verify `curl -fsS -H "Authorization: Bearer <COMPUTER_TOKEN>" http://127.0.0.1:18193/status` returns healthy desktop JSON.

### Module D — Android App Privilege Modes (Root / Shizuku / Built-in Wireless ADB)
1. **Already Rooted**: Auto-connects (`uid 0`).
2. **Non-Root Recommended — Wireless Shizuku (`uid 2000`)**:
   - Enable Android **Developer Options -> Wireless Debugging**.
   - Start **Shizuku** via Wireless Debugging (once running, it **survives turning Wi-Fi off or switching to 5G cellular**!).
   - In Pulse Agent **Settings -> 手机工作区**, tap **「一键授权 Shizuku」** — connects automatically upon approval.
3. **Non-Root Zero-Extra-App Fallback — Built-in Wireless ADB (`uid 2000`)**:
   - In **Settings -> 手机工作区**, tap **「开始无线配对通知」**, open **Pair device with pairing code**, and type the 6-digit code in the notification bar.

### Module E — Connect User's PC
Delegate PC setup to [`.agents/skills/pulse-pc-bootstrap/SKILL.md`](../pulse-pc-bootstrap/SKILL.md) (or fetch live from `GET <SERVER_URL>/bootstrap/pc-skill.md`), passing `DSCLAW1:<SERVER_URL>|<DEVICE_TOKEN>`.

---

## Troubleshooting

| Issue | Solution |
| :--- | :--- |
| `@deepseek-ai/*` npm packages fail to install | Use `BRAIN_ENGINE=legacy` on the Python server — all core chat, tools, Cloud OS, and Android VirtualDisplay features run natively. |
| Android connects to HTTP `/health` but WS/RPC fails behind Nginx | Ensure Nginx has `proxy_http_version 1.1`, `Upgrade`, `Connection "upgrade"`, and `proxy_buffering off`. |
| Xiaomi/HyperOS blocks auto-granting Accessibility under Shizuku | Enable **Developer Options -> USB debugging (Security settings)** or grant Accessibility & Overlay once manually in system Settings. |
| Phone clock skew causes encrypted `/api/secure` HTTP 400 | Turn on automatic network time synchronization on the phone/server. |
