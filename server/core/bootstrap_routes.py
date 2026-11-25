"""
[Module Responsibility]
Public read-only bootstrap Skill endpoints (`GET /bootstrap/pc-skill.md`, `GET /bootstrap/quickstart-skill.md`)
serving non-secret onboarding skills so PC AI agents can configure desktop companions from a single prompt.
"""
from __future__ import annotations

from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PC_SKILL_PATH = _REPO_ROOT / '.agents' / 'skills' / 'pulse-pc-bootstrap' / 'SKILL.md'
_QUICKSTART_SKILL_PATH = _REPO_ROOT / '.agents' / 'skills' / 'pulse-quickstart' / 'SKILL.md'

_PC_SKILL_FALLBACK = """---
name: pulse-pc-bootstrap
description: Bootstrap and pair a Windows, macOS, or Linux PC to Pulse Agent using a DSCLAW1 pair code.
---

# Pulse Agent PC Auto-Bootstrap Skill

1. Parse `DSCLAW1:<SERVER_URL>|<DEVICE_TOKEN>` and verify `curl -fsS "<SERVER_URL>/health"` returns HTTP 200.
2. Locate or clone `https://github.com/uiwcvb/proactive-personal-agent.git`.
3. Never commit or print the raw device token in public logs or Git-tracked files.
4. Choose setup route:
   - If DSH Desktop (`~/.dsh/profiles/desktop`) + Node 20+ exist:
     `node desktop/dsh-plugin/setup.mjs --pair-code "<PAIR_CODE>"`
   - Standalone Python 3.10+ desktop agent:
     Install `websockets` (and `pillow` on Windows), run
     `python desktop/desktop_agent.py --pair-code "<PAIR_CODE>" --save --check`,
     then launch `python desktop/desktop_agent.py`.
"""

_QUICKSTART_SKILL_FALLBACK = """---
name: pulse-quickstart
description: Deploy Pulse Agent server, optional VPS 24/7 Cloud Desktop, non-root Android Shizuku client, and PC companion.
---

# Pulse Agent Full-Stack Quickstart Skill

1. Ask deployment mode (local `127.0.0.1` vs Linux VPS) and target clients.
2. On open-source hosts without private `@deepseek-ai/*` registry packages, set `BRAIN_ENGINE=legacy`.
3. Verify `/health` and `/api/transport` return HTTP 200 before pairing phone or PC.
4. For non-root Android phones, recommend wireless Shizuku (`uid=2000`) or built-in wireless ADB pairing.
5. For PC setup, follow `.agents/skills/pulse-pc-bootstrap/SKILL.md`.
"""


def _read_skill_text(path: Path, fallback: str) -> str:
    if path.is_file():
        try:
            text = path.read_text(encoding='utf-8').strip()
            if text:
                return text + '\n'
        except OSError:
            pass
    return fallback


def install_bootstrap_routes(app: FastAPI) -> None:
    """Register public non-secret bootstrap skill endpoints."""

    @app.get('/bootstrap/pc-skill.md', response_class=PlainTextResponse)
    async def bootstrap_pc_skill() -> PlainTextResponse:
        return PlainTextResponse(
            _read_skill_text(_PC_SKILL_PATH, _PC_SKILL_FALLBACK),
            media_type='text/markdown; charset=utf-8',
        )

    @app.get('/bootstrap/quickstart-skill.md', response_class=PlainTextResponse)
    async def bootstrap_quickstart_skill() -> PlainTextResponse:
        return PlainTextResponse(
            _read_skill_text(_QUICKSTART_SKILL_PATH, _QUICKSTART_SKILL_FALLBACK),
            media_type='text/markdown; charset=utf-8',
        )
