import asyncio
import base64
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from computer_client import ComputerClient
from computer_files import UploadSimple, UploadStart, WorkspaceFiles, mirror_upload_across_nodes
from main import create_app


@pytest.fixture
def client_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN", "test-file-token")
    app = create_app(tmp_path, scheduler=False)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer test-file-token"
        yield app, client


def test_workspace_files_optional_sha256_and_clean_paths(tmp_path):
    ws = tmp_path / "workspace"
    transfers = tmp_path / ".transfers"
    ws.mkdir()
    transfers.mkdir()
    files = WorkspaceFiles(ws, transfers)

    # 1. Chunked upload WITHOUT client-supplied sha256 writes clean filename
    payload = b"# Server Info\n" + b"x" * 1024
    start = files.start(UploadStart(name="server_info.md", size=len(payload)))
    key = start["id"]
    assert start["key"] == key
    from computer_files import UploadChunk
    files.chunk(key, UploadChunk(offset=0, data=base64.b64encode(payload).decode()))
    fin = files.finish(key)
    assert fin["path"] == "workspace/uploads/server_info.md"
    assert fin["bytes"] == len(payload)
    assert fin["sha256"] == ""
    assert (ws / "uploads" / "server_info.md").read_bytes() == payload

    # 2. Arbitrary/mismatched sha256 placeholder is accepted; byte-size mismatch or out-of-bounds chunk is rejected
    compat_start = files.start(UploadStart(name="compat.bin", size=3, sha256="not-checked-placeholder"))
    files.chunk(compat_start["id"], UploadChunk(offset=0, data=base64.b64encode(b"abc").decode()))
    compat_fin = files.finish(compat_start["id"])
    assert compat_fin["bytes"] == 3
    assert (ws / "uploads" / "compat.bin").read_bytes() == b"abc"

    short_start = files.start(UploadStart(name="short.bin", size=5, sha256="0" * 64))
    files.chunk(short_start["id"], UploadChunk(offset=0, data=base64.b64encode(b"ab").decode()))
    with pytest.raises(ValueError):
        files.finish(short_start["id"])
    with pytest.raises(ValueError):
        files.chunk(short_start["id"], UploadChunk(offset=2, data=base64.b64encode(b"cdef").decode()))
    files.cancel(short_start["id"])

    # 3. Single-shot UploadSimple works for both 0-byte and normal payloads (even with placeholder sha256)
    empty_res = files.put_simple(UploadSimple(name="empty.txt", data="", sha256="legacy-ignored"))
    assert empty_res["path"] == "workspace/uploads/empty.txt"
    assert empty_res["bytes"] == 0
    assert (ws / "uploads" / "empty.txt").read_bytes() == b""

    # 4. Bidirectional resolution (clean <-> legacy <10hex>-prefix) & traversal safety
    resolved_legacy = files.resolve_existing_path("workspace/uploads/62858d56ee-server_info.md")
    assert resolved_legacy.name == "server_info.md"
    resolved_rel = files.resolve_existing_path("uploads/server_info.md")
    assert resolved_rel.read_bytes() == payload

    legacy_file = ws / "uploads" / "abcdef1234-legacy_note.md"
    legacy_file.write_text("legacy", encoding="utf-8")
    assert files.resolve_existing_path("workspace/uploads/legacy_note.md") == legacy_file

    with pytest.raises(ValueError):
        files.resolve_existing_path("../escape.txt")


def test_cloud_desktop_client_upload_and_allowlist(tmp_path, monkeypatch):
    client = ComputerClient(base_url="http://127.0.0.1:9999", token="tok")
    src = tmp_path / "abcdef0123-demo.txt"
    src.write_bytes(b"hello cloud computer")
    calls = []

    async def fake_call(method, path, body=None):
        calls.append((method, path, body))
        if path == "/library/uploads":
            return {"id": "a" * 32, "offset": 0}
        if path.endswith("/finish"):
            return {"path": "workspace/uploads/demo.txt", "bytes": 20, "sha256": body or ""}
        return {"id": "a" * 32, "offset": 20}

    monkeypatch.setattr(client, "call", fake_call)
    res = asyncio.run(client.upload_file(src))
    assert res["path"] == "workspace/uploads/demo.txt"
    assert calls[0][1] == "/library/uploads"
    assert calls[0][2]["name"] == "demo.txt"
    assert calls[0][2]["size"] == len(b"hello cloud computer")
    assert calls[0][2].get("sha256", "") == ""

    # Size mismatch in finish response is rejected
    async def mismatch_call(method, path, body=None):
        if path == "/library/uploads":
            return {"id": "b" * 32, "offset": 0}
        if path.endswith("/finish"):
            return {"path": "workspace/uploads/demo.txt", "bytes": 999}
        return {"id": "b" * 32, "offset": 20}

    monkeypatch.setattr(client, "call", mismatch_call)
    with pytest.raises(ValueError, match="字节大小不一致"):
        asyncio.run(client.upload_file(src))

    # Invalid path rejected by allowlist before network
    raw_client = ComputerClient(base_url="http://127.0.0.1:9999", token="tok")
    with pytest.raises(ValueError, match="云电脑操作无效"):
        asyncio.run(raw_client.call("POST", "/evil/path"))


def test_fastapi_simple_upload_and_mirror(client_env, tmp_path, monkeypatch):
    app, client = client_env
    mirrored = []
    monkeypatch.setattr(app.state.brain.computer.client, "_explicit_token", "")

    async def fake_mirror(info, **kw):
        mirrored.append(info)

    raw = b"server-info-content-42"
    r = client.post("/api/files/upload", json={"name": "server_info.md", "data": base64.b64encode(raw).decode(), "sha256": "ignored"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "workspace/uploads/server_info.md"
    assert body["bytes"] == len(raw)

    # Verify mirror_upload_across_nodes notifies DSH and publishes event
    ws_root = app.state.brain.harness.root
    dsh_calls = []
    pub_calls = []

    class FakeClient:
        configured = True
        async def upload_file(self, src_path, remote_name=None):
            dsh_calls.append(("desktop", remote_name, Path(src_path).read_bytes()))
            return {"path": f"workspace/uploads/{remote_name}"}

    class FakeComp:
        client = FakeClient()

    asyncio.run(
        mirror_upload_across_nodes(
            body,
            workspace_root=ws_root,
            computer=FakeComp(),
            dispatch_dsh=lambda p: dsh_calls.append(("dsh", p)),
            publish=lambda ev, data: pub_calls.append((ev, data)),
        )
    )
    assert any(c[0] == "desktop" and c[1] == "server_info.md" for c in dsh_calls)
    dsh_payload = [c[1] for c in dsh_calls if c[0] == "dsh"][0]
    assert dsh_payload["op"] == "sync_uploaded_file"
    assert dsh_payload["workspace_path"] == "uploads/server_info.md"
    assert base64.b64decode(dsh_payload["data_b64"]) == raw
    assert pub_calls and pub_calls[0][0] == "file.uploaded"

    # Cloud desktop push_file with inline content + clean path resolution
    uploaded_desktop = {}

    async def fake_upload_file(src_path, remote_name=None):
        uploaded_desktop["name"] = remote_name
        uploaded_desktop["bytes"] = Path(src_path).read_bytes()
        return {"path": f"workspace/uploads/{remote_name}", "bytes": len(uploaded_desktop["bytes"]), "sha256": "ok"}

    monkeypatch.setattr(app.state.brain.computer.client, "_explicit_token", "tok")
    monkeypatch.setattr(app.state.brain.computer.client, "upload_file", fake_upload_file)
    push_res = asyncio.run(app.state.brain._tool_cloud_desktop({"op": "push_file", "path": "workspace/uploads/62858d56ee-server_info.md"}))
    assert push_res["ok"] is True
    assert uploaded_desktop["name"] == "server_info.md"
    assert uploaded_desktop["bytes"] == raw


def test_dsh_file_bridge_node_integration(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    bridge_path = (Path(__file__).resolve().parents[2] / "desktop" / "dsh-plugin" / "lib-file-bridge.mjs").as_uri()
    pc_ws = tmp_path / "pc_ws"
    ext_file = tmp_path / "external_secret.md"
    ext_file.write_text("# VPS Table\nhost=1.2.3.4\n", encoding="utf-8")

    script = f"""
import fs from 'node:fs';
import path from 'node:path';
import {{
  handleLocalUploadRpc,
  resolveSmartPcPath,
  syncWorkspaceFileToPc,
  executePcCloudDesktopTool,
}} from {json.dumps(bridge_path)};

const calls = [];
const mockCloudFetch = async (urlPath, method, body) => {{
  calls.push({{ urlPath, method, body }});
  if (urlPath === '/api/files/upload') {{
    return {{ path: 'workspace/uploads/' + body.name, name: body.name, bytes: Buffer.from(body.data, 'base64').length, sha256: body.sha256 }};
  }}
  if (urlPath === '/api/computer/command') {{
    return {{ ok: true, op: body.op, desktop_path: body.path }};
  }}
  return {{ ok: true }};
}};

const upRes = await handleLocalUploadRpc({{
  name: 'server_info.md',
  data_b64: Buffer.from('# Test\\nok', 'utf-8').toString('base64')
}}, mockCloudFetch);
if (!upRes.ok || upRes.path !== 'workspace/uploads/server_info.md') throw new Error('upload failed: ' + JSON.stringify(upRes));
if (upRes.sha256 !== '') throw new Error('expected empty sha256 without hashing: ' + JSON.stringify(upRes));
if (!fs.existsSync(upRes.local_path)) throw new Error('local mirror missing');

const resolvedLegacy = resolveSmartPcPath('workspace/uploads/62858d56ee-server_info.md', {json.dumps(str(pc_ws))});
if (!fs.existsSync(resolvedLegacy) || path.basename(resolvedLegacy) !== 'server_info.md') {{
  throw new Error('legacy alias resolve failed: ' + resolvedLegacy);
}}

const synced = await syncWorkspaceFileToPc({{
  payload: {{ name: 'from_phone.txt', data_b64: Buffer.from('hi pc', 'utf-8').toString('base64') }},
  cloudFetch: mockCloudFetch
}});
if (!synced.ok || !fs.existsSync(synced.localPath)) throw new Error('syncWorkspaceFileToPc failed');

const deskRes = await executePcCloudDesktopTool({{
  op: 'push_file',
  path: {json.dumps(str(ext_file))}
}}, mockCloudFetch);
if (!deskRes.ok || deskRes.data.desktop_path !== 'workspace/uploads/external_secret.md') {{
  throw new Error('executePcCloudDesktopTool failed: ' + JSON.stringify(deskRes));
}}
console.log('DSH_BRIDGE_OK');
"""
    env = {"DSH_PC_WORKSPACE": str(pc_ws), "PATH": ""}
    import os
    merged_env = {**os.environ, "DSH_PC_WORKSPACE": str(pc_ws)}
    proc = subprocess.run([node, "--input-type=module", "-e", script], env=merged_env, capture_output=True, text=True, check=True)
    assert "DSH_BRIDGE_OK" in proc.stdout
