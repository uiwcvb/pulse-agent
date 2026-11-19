import asyncio
import io
import os
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from core.cloud_computer_deployer import build_cloud_os_tarball, load_or_create_ed25519_key
from core.onboarding_service import handle_system_config_tool, infer_provider_defaults
from core.phone_ui_formatter import parse_phone_nodes, resolve_phone_grid_or_norm
from core.prompt_guards import clean_and_filter_dot_text, strip_fake_action_stage_directions
from main import create_app


def test_infer_provider_defaults():
    ds = infer_provider_defaults({"api_key": "sk-test", "api_base_url": "https://api.deepseek.com/v1"})
    assert ds["api_base_url"] == "https://api.deepseek.com/v1"
    assert ds["api_key"] == "sk-test"
    assert ds["default_model"]

    oai = infer_provider_defaults({"provider": "openai", "api_key": "sk-oai"})
    assert "openai.com" in oai["api_base_url"]
    assert oai["default_model"]


def test_cloud_computer_deployer_artifacts(tmp_path):
    priv, pub = load_or_create_ed25519_key(tmp_path)
    assert "BEGIN OPENSSH PRIVATE KEY" in priv
    assert pub.startswith("ssh-ed25519 ")
    priv2, pub2 = load_or_create_ed25519_key(tmp_path)
    assert priv == priv2 and pub == pub2

    blob = build_cloud_os_tarball(Path(__file__).resolve().parents[1])
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "computer_runtime.py" in names
    assert "computer_files.py" in names


def test_system_config_tool_and_helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "tok-onboard")
    app = create_app(tmp_path, scheduler=False)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer tok-onboard"
        status = asyncio.run(handle_system_config_tool(app.state.brain, {"op": "status"}))
        assert status["ok"] is True
        res = asyncio.run(
            handle_system_config_tool(
                app.state.brain,
                {"op": "update", "auto_compact_tokens": 120000},
            )
        )
        assert res["ok"] is True
        assert app.state.store.config.auto_compact_tokens == 120000

    assert resolve_phone_grid_or_norm({"grid": "B3"}, 1000, 2000) == (250, 500)
    nodes = parse_phone_nodes({"nodes": [{"ref": "n1", "text": "确认", "cx": 500, "cy": 1200, "clickable": True}]})
    assert len(nodes) == 1 and nodes[0]["text"] == "确认"
    assert strip_fake_action_stage_directions("（正在打开网页查看）好的") == "好的"
    assert clean_and_filter_dot_text("  你好  ") == "你好"


def test_source_files_under_1000_lines():
    repo = Path(__file__).resolve().parents[2]
    roots = [
        repo / "server",
        repo / "desktop" / "dsh-plugin",
        repo / "android" / "app" / "src" / "main" / "java" / "dev" / "bubuki" / "proactive",
    ]
    exts = {".py", ".kt", ".java", ".js", ".mjs"}
    oversized = []
    for base in roots:
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix not in exts:
                continue
            if any(part in {".venv", "node_modules", "__pycache__", "build"} for part in p.parts):
                continue
            n = len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
            if n >= 1000:
                oversized.append(f"{p.relative_to(repo)}:{n}")
    assert not oversized, f"Files >= 1000 lines found: {oversized}"
