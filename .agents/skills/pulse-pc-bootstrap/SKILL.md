---
name: pulse-pc-bootstrap
description: Bootstrap and pair a Windows, macOS, or Linux PC to the user's Pulse Agent hub using a DSCLAW1 pair code or server URL + device token, supporting both DSH Desktop plugin mount and standalone Python desktop_agent.
---

# Pulse Agent PC Auto-Bootstrap Skill (`pulse-pc-bootstrap`)

Use this skill whenever the user wants an AI coding agent on their computer (Claude Code, Codex, Antigravity, Cursor, Windsurf, etc.) to automatically install, configure, and verify PC connection to their **Pulse Agent** server.

## Strict Safety Rules

1. **Never echo or leak secrets**: Treat the `DSCLAW1:<url>|<token>` pair code and device token as credentials.
   - Write them ONLY to local ignored config files (`desktop/dsh-plugin/bridge_config.json` or `~/.pulse-agent/pc_config.json`).
   - Never print the full `token` or `pair_code` in summaries, Markdown reports, or Git-tracked files.
2. **Never commit runtime configs**: Do not commit `bridge_config.json`, `pc_config.json`, `.env`, or `private/`.
3. **No stealth persistence**: Never create hidden Startup `.vbs` scripts or stealth registry Run entries. Only launch the desktop agent visibly when the user wants it running.

---

## Step 1: Parse Credentials & Probe Hub Reachability

Expect input in one of two forms:
- **Pair code**: `DSCLAW1:<SERVER_URL>|<DEVICE_TOKEN>` (strip `DSCLAW1:` and split on the **last** `|` character)
- **Explicit**: `<SERVER_URL>` + `<DEVICE_TOKEN>`

Check that the hub is reachable before installing anything:

```bash
curl -fsS "<SERVER_URL>/health"
```

If `<SERVER_URL>` returns `404` without trailing `/proactive-agent`, verify whether the user's reverse proxy serves at `/proactive-agent` vs root, and report clearly if unreachable.

---

## Step 2: Locate or Clone Repository

1. If you are already inside the `proactive-personal-agent` directory, stay here and pull only if clean/requested.
2. Otherwise clone into a clean local folder (e.g. `~/pulse-agent` on macOS/Linux or `%USERPROFILE%\pulse-agent` on Windows):

```bash
git clone --depth 1 https://github.com/uiwcvb/proactive-personal-agent.git pulse-agent
cd pulse-agent
```

---

## Step 3: Auto-Detect Runtime & Execute Setup

Inspect which runtime is present on this computer:

### Route A — DSH Desktop Plugin + Win32 CUA Bridge (Windows with DSH Desktop & Node 20+)

Check if `~/.dsh/profiles/desktop` exists **and** `node -v` is installed (`>= 20`):

```bash
node desktop/dsh-plugin/setup.mjs --pair-code "<PAIR_CODE>"
```

(Or pass `--server "<SERVER_URL>" --token "<DEVICE_TOKEN>"`.)

What this automatically does:
- Writes `desktop/dsh-plugin/bridge_config.json` (git-ignored)
- Mounts `dsh-deepseek-claw` into `~/.dsh/profiles/desktop` (and `DSH_WEB_PROFILE_DIR` if set)

After running:
- Verify `desktop/dsh-plugin/bridge_config.json` exists and contains `server_url` + non-default `token`.
- Remind the user to restart DSH Desktop once if it was already open.

### Route B — Standalone Lightweight Desktop Agent (Windows / macOS / Linux, Python 3.10+)

Always configure this if Route A does not apply (no DSH Desktop installed), or alongside Route A when the user also wants headless WebSocket desktop control (`/ws/device`):

1. Ensure a Python environment with `websockets` (and `pillow` on Windows for JPEG capture):

```bash
python -m venv .pc-venv
# Windows PowerShell: .\.pc-venv\Scripts\python.exe -m pip install -q websockets pillow
# macOS / Linux:      ./.pc-venv/bin/python -m pip install -q websockets
```

2. Configure, save credentials to `~/.pulse-agent/pc_config.json`, and run a live 1.5s WebSocket handshake check in one step:

```bash
# Windows:
.\.pc-venv\Scripts\python.exe desktop/desktop_agent.py --pair-code "<PAIR_CODE>" --save --check
# macOS / Linux:
./.pc-venv/bin/python desktop/desktop_agent.py --pair-code "<PAIR_CODE>" --save --check
```

Expected output contains `DESKTOP_AGENT_CONNECTED`. Once `--save` has written `~/.pulse-agent/pc_config.json`, future launches require no token on the command line:

```bash
python desktop/desktop_agent.py
```

3. Ask the user if they want the desktop agent left running now (in background/minimized terminal) so their phone/AI can control the PC immediately.

---

## Step 4: Verify & Report

Confirm:
1. `<SERVER_URL>/health` returned HTTP 200.
2. Configured mode (`Route A: DSH Plugin` and/or `Route B: Python Desktop Agent --check passed`).
3. Tell the user concisely that their PC is paired and ready to receive commands from the Pulse Agent phone app!

---

## Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| `DESKTOP_AGENT_CONNECTED` fails with HTTP 401/403 or immediate close | Re-check `DSCLAW1:<url>|<token>` for missing characters or extra whitespace; confirm server uses the same `device-token`. |
| `node` or `python` missing | Install Python 3.10+ (`winget install Python.Python.3.12` / `brew install python`) or Node 20+ before retrying Step 3. |
| Corporate/local proxy breaks `ws://` / `wss://` or TLS | Verify `wss://` proxy passes `Upgrade: websocket` and `Connection: upgrade` headers cleanly; test `<SERVER_URL>/health` over `curl`. |
| Windows PowerShell garbles Chinese logs | Run `$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8`. |
