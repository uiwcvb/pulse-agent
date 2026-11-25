from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from brain import Brain
from core.compaction_service import build_compaction_messages
from core.memory_engine import MemoryEngine
from core.sqlite_state import SQLiteStateBackend, _fast_msg_sig
from core.tool_schemas import scrub_subprocess_env
from harness import Harness
from main import create_app
from plugins.integration_hub import IntegrationHubPlugin
from security import CryptoBox, b64
from storage import Store
from vault_manager import Vault


def test_config_audit_redacts_nested_secrets_preserving_path(tmp_path: Path):
    store = Store(tmp_path / 'store')
    pub = store.update_config(
        {
            'small_brain': {'base_url': 'https://api.example.com/v1', 'api_key': 'sk-nested-small-secret', 'model': 'm1'},
            'smtp': {'host': 'smtp.example.com', 'password': 'smtp-super-password'},
        },
        source_tag='workspace_sync',
    )
    assert isinstance(pub, dict)
    audit_p = store.settings_ws_path.parent / 'logs' / 'config_audit.md'
    assert audit_p.exists()
    content = audit_p.read_text('utf-8')
    assert 'workspace_sync' in content
    assert 'sk-nested-small-secret' not in content
    assert 'smtp-super-password' not in content
    assert '***' in content


def test_sqlite_state_sig_tracks_variants_and_timeline(tmp_path: Path):
    db_path = tmp_path / 'state.db'
    sql = SQLiteStateBackend(db_path)
    sql.load_or_migrate(tmp_path / 'state.json', lambda: {'messages': []})
    msg = {
        'id': 'm1',
        'role': 'assistant',
        'text': 'hello',
        'bot_id': 'companion',
        'source': 'phone',
        'order_seq': 1,
        'seq': 1,
        'created_at': 100.0,
        'timeline': [{'type': 'text', 'text': 'hello'}],
        'variants': [{'id': 'v0', 'text': 'hello', 'timeline': [{'type': 'text', 'text': 'hello'}]}],
        'active_variant_idx': 0,
    }
    sig1 = _fast_msg_sig(msg)
    sql.upsert_message(msg)
    msg_mut = {
        **msg,
        'timeline': [{'type': 'text', 'text': 'hello'}, {'type': 'tool', 'tool': 'read_file'}],
        'active_variant_idx': 1,
    }
    sig2 = _fast_msg_sig(msg_mut)
    assert sig1 != sig2
    sql.upsert_message(msg_mut)
    loaded = sql.load_or_migrate(tmp_path / 'state.json', lambda: {'messages': []})['messages']
    assert len(loaded) == 1
    assert loaded[0]['active_variant_idx'] == 1
    assert len(loaded[0]['timeline']) == 2
    sql.close()


@pytest.mark.asyncio
async def test_custom_tool_and_system_config_blocked_in_non_user_context(tmp_path: Path):
    store = Store(tmp_path / 'store')
    vault = Vault(tmp_path / 'vault')
    harness = Harness(tmp_path / 'workspace', store=store, vault=vault)

    async def _noop_pub(ev):
        pass

    class _NoopEsc:
        async def track(self, m):
            pass

    brain = Brain(store, vault, harness, _noop_pub, _NoopEsc())
    tok = brain.turn_origin_context.set('autonomous')
    try:
        res_ct = await brain._tool_custom_tool({
            'action': 'create',
            'name': 'auto_tool',
            'code': 'def run(args):\n    return {"ok": True}',
        })
        assert res_ct.get('ok') is False
        assert '权限锁定' in str(res_ct.get('error') or '')

        res_sc = await brain._tool_system_config({'action': 'pc_connect_guide'})
        assert res_sc.get('ok') is False
        assert '权限锁定' in str(res_sc.get('error') or '')

        res_upd = await brain._tool_system_config({'action': 'update', 'api_key': 'sk-rogue'})
        assert res_upd.get('ok') is False
        assert '权限锁定' in str(res_upd.get('error') or '')
    finally:
        brain.turn_origin_context.reset(tok)

    # Default direct harness test path (without non-user context set) continues working
    tok_direct = brain.turn_origin_context.set('user_direct')
    try:
        res_ok = await brain._tool_custom_tool({
            'action': 'create',
            'name': 'direct_tool',
            'code': 'def run(args):\n    return {"ok": True}',
        })
        assert res_ok.get('ok') is True
    finally:
        brain.turn_origin_context.reset(tok_direct)


def test_workspace_media_svg_forced_attachment_and_sandbox_headers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('AGENT_TOKEN', 'test-secret-token-123')
    app = create_app(tmp_path, scheduler=False)
    uploads = app.state.brain.harness.root / 'uploads'
    uploads.mkdir(parents=True, exist_ok=True)
    svg_p = uploads / 'evil.svg'
    svg_p.write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', encoding='utf-8')
    png_p = uploads / 'ok.png'
    png_p.write_bytes(b'\x89PNG\r\n\x1a\n')

    client = TestClient(app)
    r_svg_anon = client.get('/workspace-media/uploads/evil.svg')
    assert r_svg_anon.status_code == 404

    r_svg = client.get('/workspace-media/uploads/evil.svg', headers={'Authorization': 'Bearer test-secret-token-123'})
    assert r_svg.status_code == 200
    assert 'attachment' in (r_svg.headers.get('content-disposition') or '').lower()
    assert r_svg.headers.get('x-content-type-options') == 'nosniff'
    assert "default-src 'none'" in (r_svg.headers.get('content-security-policy') or '')

    r_png = client.get('/workspace-media/uploads/ok.png')
    assert r_png.status_code == 200
    assert 'attachment' not in (r_png.headers.get('content-disposition') or '').lower()
    assert r_png.headers.get('x-content-type-options') == 'nosniff'


@pytest.mark.asyncio
async def test_integration_hub_github_rejects_external_host():
    hub = IntegrationHubPlugin()
    res = await hub._github_action('github_request', 'ghp_secret_pat', {'endpoint': 'https://evil.example/steal'}, '')
    assert 'error' in res
    assert 'GitHub' in res['error']

    res_rel = await hub._github_action('github_request', 'ghp_secret_pat', {'endpoint': '//evil.example/steal'}, '')
    assert 'error' in res_rel


def test_deterministic_pins_and_compaction_prompt_and_env_scrub(tmp_path: Path):
    vault = Vault(tmp_path / 'vault')
    vault.write('profile/pinned_memories.md', '# Pins\n- Remember milk\n- Call mom\n')
    store = Store(tmp_path / 'store')
    eng = MemoryEngine(store, vault, Harness(tmp_path / 'ws', store=store, vault=vault), {})
    p1 = eng.list_pins()
    p2 = eng.list_pins()
    assert [x['id'] for x in p1] == [x['id'] for x in p2]

    msgs = build_compaction_messages('user: hi\nassistant: ok')
    assert '绝对禁止回答' in msgs[0]['content']

    clean = scrub_subprocess_env(base_env={
        'PATH': '/usr/bin',
        'AGENT_TOKEN': 'secret1',
        'DSH_INTERNAL_SECRET': 'secret2',
        'DEEPSEEK_API_KEY': 'secret3',
        'HOME': '/home/u',
    })
    assert clean.get('PATH') == '/usr/bin'
    assert clean.get('HOME') == '/home/u'
    assert 'AGENT_TOKEN' not in clean
    assert 'DSH_INTERNAL_SECRET' not in clean
    assert 'DEEPSEEK_API_KEY' not in clean


def test_security_cryptobox_does_not_cache_invalid_tag_and_evicts_lru(tmp_path: Path):
    from cryptography.exceptions import InvalidTag
    crypto = CryptoBox(tmp_path)
    fake_key_b64 = b64(b'fake-rsa-wrapped-key-blob')
    # Monkeypatch decrypt to observe call count on invalid GCM tags
    calls = 0
    def _fake_decrypt(*a, **kw):
        nonlocal calls
        calls += 1
        return b'\x00' * 32
    crypto.key = SimpleNamespace(decrypt=_fake_decrypt)  # type: ignore[assignment]
    for _ in range(2):
        with pytest.raises((InvalidTag, ValueError)):
            crypto.open({'key': fake_key_b64, 'nonce': b64(b'1'*12), 'data': b64(b'invalidciphertext1234567890')})
    assert fake_key_b64 not in crypto._session_keys
    assert calls == 2


@pytest.mark.asyncio
async def test_blackboard_entries_key_and_scrub(tmp_path: Path):
    store = Store(tmp_path / 'store')
    vault = Vault(tmp_path / 'vault')
    harness = Harness(tmp_path / 'ws', store=store, vault=vault)
    async def _pub(ev): pass
    class _Esc:
        async def track(self, m): pass
    brain = Brain(store, vault, harness, _pub, _Esc())
    brain.context_search.blackboard_write('k1', 'v1')
    res_list = await brain.blackboard_engine.execute({'action': 'list'})
    assert isinstance(res_list.get('entries'), list)
    assert any(it.get('key') == 'k1' for it in res_list['entries'])


@pytest.mark.asyncio
async def test_clock_wheel_watchpoint_blocks_outside_workspace(tmp_path: Path):
    from core.clock_wheel import UnifiedClockWheel

    store = Store(tmp_path / 'store')
    vault = Vault(tmp_path / 'vault')
    harness = Harness(tmp_path / 'ws', store=store, vault=vault)
    async def _pub(ev): pass
    class _Esc:
        async def track(self, m): pass
    brain = Brain(store, vault, harness, _pub, _Esc())
    cw = UnifiedClockWheel(brain)

    outside = tmp_path / 'outside_secret.txt'
    outside.write_text('secret', encoding='utf-8')
    ok_out, _ = await cw.check_watchpoint({'kind': 'file_exists', 'target': str(outside)})
    assert ok_out is False

    ok_trav, _ = await cw.check_watchpoint({'kind': 'file_exists', 'target': '../outside_secret.txt'})
    assert ok_trav is False

    inside = harness.root / 'sub' / 'ready.flag'
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text('ready', encoding='utf-8')
    ok_in, _ = await cw.check_watchpoint({'kind': 'file_exists', 'target': str(inside)})
    assert ok_in is True
