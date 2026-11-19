"""Regression & static guard suite for the 5-Blade Overhaul (no toxic caps, reapers, or secret leaks)."""
from __future__ import annotations

import base64
import ipaddress
import json
import re
import sys
import types
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from core.blackboard_scheduler_helpers import next_cron_timestamp
from main import create_app
from vault_manager import Vault


def _make_brain(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('AGENT_TOKEN', 'test-token-guard')
    app = create_app(tmp_path / 'data', scheduler=False)
    app.state.store.config.small_brain.api_key = 'sk-test-guard-1234567890'
    app.state.store.config.small_brain.base_url = 'https://api.example.com/v1'
    app.state.store.config.small_brain.model = 'deepseek-v4.1-flash'
    app.state.store.config.big_brain.api_key = 'sk-test-guard-1234567890'
    app.state.store.config.big_brain.base_url = 'https://api.example.com/v1'
    app.state.store.config.big_brain.model = 'deepseek-v4.1-flash'
    return app.state.brain


def _make_luhn_card(prefix15: str) -> str:
    digits = [int(c) for c in prefix15]
    total = 0
    for idx, d in enumerate(reversed(digits + [0])):
        val = d * 2 if idx % 2 == 1 else d
        if val > 9:
            val -= 9
        total += val
    check = (10 - (total % 10)) % 10
    return f'{prefix15}{check}'


def test_static_no_toxic_limits_guard():
    banned_tokens = (
        '_strip_multimodal_images',
        'reap_idle_browsers',
        '_browser_idle_reaper',
        '_stop_cloud_computer_fire_and_forget',
        '_auto_release_phone_if_used',
        'IDLE_AUTO_RELEASE_MS',
        'MAX_HIST_STEPS_PER_TURN',
        'repeat(24)',
        'budget_warned_15',
        'codex-computer-use.exe',
        'luakit-webkit2',
    )
    roots = (
        SERVER_DIR,
        REPO_ROOT / 'android' / 'app' / 'src' / 'main',
        REPO_ROOT / 'desktop' / 'dsh-plugin',
    )
    skip_parts = {'.venv', '__pycache__', '.pytest_cache', 'node_modules', 'tests', 'data', 'build', '.gradle'}
    skip_files = {'bridge_config.json'}
    text_exts = {'.py', '.kt', '.java', '.js', '.mjs', '.css', '.ps1', '.md', '.json', '.xml', '.gradle'}
    public_ip_url_re = re.compile(r'https?://(\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?(?:/|\b)')

    def _is_disallowed_ip_literal(ip_text: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip_text)
        except ValueError:
            return False
        if addr.is_loopback or addr.is_unspecified or addr.is_private:
            return False
        # Allow standard RFC 5737 documentation/test IPv4 ranges if used in docs/examples
        if any(
            addr in ipaddress.ip_network(net)
            for net in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24')
        ):
            return False
        return True

    hits: list[str] = []
    scan_files: list[Path] = []
    for base in roots:
        if base.exists():
            scan_files.extend(p for p in base.rglob('*') if p.is_file())
    for doc_name in ('README.md', 'CODEBASE_MAP.md'):
        doc_path = REPO_ROOT / doc_name
        if doc_path.is_file():
            scan_files.append(doc_path)

    for p in scan_files:
        if p.name in skip_files or p.suffix.lower() not in text_exts:
            continue
        rel_parts = set(p.relative_to(REPO_ROOT).parts)
        if rel_parts & skip_parts:
            continue
        txt = p.read_text('utf-8', errors='ignore')
        for tok in banned_tokens:
            if tok in txt:
                hits.append(f'{p.relative_to(REPO_ROOT)} contains {tok}')
        for m in public_ip_url_re.finditer(txt):
            if _is_disallowed_ip_literal(m.group(1)):
                hits.append(f'{p.relative_to(REPO_ROOT)} contains hardcoded public IP URL {m.group(0)}')

    assert not hits, 'Found banned legacy symbols/IPs:\n' + '\n'.join(hits)


def test_vault_write_guard_redacts_secrets_and_pii(tmp_path: Path):
    vault = Vault(tmp_path / 'vault')
    sample_sk = 'sk-amr-' + 'AbCdEf1234567890XyZ'
    sample_cn_id = '11010519491231002X'
    sample_card = _make_luhn_card('622202100111222')
    pem_block = '-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBALeFakeKeyMaterialHere\n-----END RSA PRIVATE KEY-----'

    raw_doc = (
        '# 敏感信息测试\n'
        f'- key: {sample_sk}\n'
        f'- id: {sample_cn_id}\n'
        f'- card: {sample_card}\n'
        f'{pem_block}\n'
    )
    vault.write('notes/secret_check.md', raw_doc)
    saved = vault.read('notes/secret_check.md')

    assert sample_sk not in saved
    assert 'sk-amr***REDACTED***' in saved
    assert sample_cn_id not in saved
    assert '110*************2X' in saved
    assert sample_card not in saved
    assert '6222********' in saved
    assert 'MIIBOgIBAAJBALeFakeKeyMaterialHere' not in saved
    assert '[PRIVATE_KEY_REDACTED]' in saved


@pytest.mark.asyncio
async def test_3frame_rolling_vision_window_and_reasoning_content_preserved(tmp_path: Path, monkeypatch):
    brain = _make_brain(tmp_path, monkeypatch)
    fake_png_b64 = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'0' * 32).decode('ascii')
    shot_idx = 0

    async def fake_execute(name: str, args: dict):
        nonlocal shot_idx
        shot_idx += 1
        return {
            'ok': True,
            'frame_hash': f'fh_{shot_idx}',
            '_multimodal_b64': fake_png_b64,
            '_multimodal_mime': 'image/png',
            '_multimodal_caption': f'frame_cap_{shot_idx}',
        }

    brain.harness.execute = fake_execute  # type: ignore[method-assign]
    recorded_calls: list[list[dict]] = []
    step_n = 0

    async def fake_complete(messages, tools=None, big=False, **kwargs):
        nonlocal step_n
        step_n += 1
        recorded_calls.append(messages)
        if step_n <= 4:
            return {
                'role': 'assistant',
                'content': f'第 {step_n} 步检查',
                'reasoning_content': f'internal_cot_{step_n}',
                'tool_calls': [{
                    'id': f'call_vis_{step_n}',
                    'type': 'function',
                    'function': {'name': 'list_dir', 'arguments': json.dumps({'path': f'step_{step_n}'})},
                }],
            }
        return {'role': 'assistant', 'content': '四帧界面变化已核对完成。', 'tool_calls': []}

    brain.complete = fake_complete  # type: ignore[method-assign]
    out = await brain.chat('请连续检查四步画面', bot_id='companion', source='phone')
    assert '四帧界面变化已核对完成' in out['text']
    assert step_n == 5

    last_msgs = recorded_calls[-1]
    assistant_cots = [m.get('reasoning_content') for m in last_msgs if m.get('role') == 'assistant' and m.get('reasoning_content')]
    assert assistant_cots == ['internal_cot_1', 'internal_cot_2', 'internal_cot_3', 'internal_cot_4']

    tail = last_msgs[-1]
    assert tail['role'] == 'user' and isinstance(tail['content'], list)
    text_parts = [p['text'] for p in tail['content'] if p.get('type') == 'text']
    img_parts = [p for p in tail['content'] if p.get('type') == 'image_url']
    assert len(img_parts) == 3
    joined_caps = '\n'.join(text_parts)
    assert '[首帧基准] frame_cap_1' in joined_caps
    assert 'frame_cap_2' not in joined_caps
    assert '[上一步画面] frame_cap_3' in joined_caps
    assert '[当前最新画面] frame_cap_4' in joined_caps


@pytest.mark.asyncio
async def test_reply_op_silent_emits_zero_visible_bubbles(tmp_path: Path, monkeypatch):
    brain = _make_brain(tmp_path, monkeypatch)
    published: list[dict] = []
    orig_publish = brain.publish

    async def capture_publish(evt: dict):
        published.append(evt)
        return await orig_publish(evt)

    brain.publish = capture_publish  # type: ignore[method-assign]

    async def silent_complete(messages, tools=None, big=False, **kwargs):
        return {
            'role': 'assistant',
            'content': '',
            'tool_calls': [{
                'id': 'call_silent_1',
                'type': 'function',
                'function': {'name': 'reply', 'arguments': json.dumps({'op': 'silent', 'reason': 'user_sleeping'})},
            }],
        }

    brain.complete = silent_complete  # type: ignore[method-assign]
    before_count = len([m for m in brain.store.state['messages'] if m.get('role') == 'assistant'])
    out = await brain.chat('夜间主动巡检', bot_id='companion', source='phone')
    after_count = len([m for m in brain.store.state['messages'] if m.get('role') == 'assistant'])

    assert out.get('status') == 'silent'
    assert out.get('silent') is True
    assert after_count == before_count
    assert not any(
        e.get('type') == 'message' and isinstance(e.get('message'), dict) and e['message'].get('role') == 'assistant'
        for e in published
    )


@pytest.mark.asyncio
async def test_admin_bot_config_locked_outside_user_direct(tmp_path: Path, monkeypatch):
    brain = _make_brain(tmp_path, monkeypatch)

    tok_origin = brain.turn_origin_context.set('autonomous')
    tok_hop = brain.hop_depth.set(0)
    try:
        res_auto = await brain._tool_admin({'op': 'bot', 'action': 'create', 'name': 'RogueBot'})
        assert res_auto.get('ok') is False
        assert '权限锁定' in str(res_auto.get('error') or '')
    finally:
        brain.hop_depth.reset(tok_hop)
        brain.turn_origin_context.reset(tok_origin)

    tok_origin = brain.turn_origin_context.set('user_direct')
    tok_hop = brain.hop_depth.set(1)
    try:
        res_hop = await brain._tool_admin({'op': 'bot', 'action': 'delete', 'bot_id': 'research'})
        assert res_hop.get('ok') is False
        assert '权限锁定' in str(res_hop.get('error') or '')
    finally:
        brain.hop_depth.reset(tok_hop)
        brain.turn_origin_context.reset(tok_origin)

    tok_origin = brain.turn_origin_context.set('user_direct')
    tok_hop = brain.hop_depth.set(0)
    brain.activity = 'ready'
    try:
        res_ok = await brain._tool_admin({'op': 'bot', 'action': 'create', 'name': 'DirectAllowedBot', 'bot_id': 'direct_allowed'})
        assert res_ok.get('created') is True
    finally:
        brain.hop_depth.reset(tok_hop)
        brain.turn_origin_context.reset(tok_origin)


@pytest.mark.asyncio
async def test_blackboard_anti_storm_mute_and_cron_expr(tmp_path: Path, monkeypatch):
    brain = _make_brain(tmp_path, monkeypatch)

    mute_res = await brain.blackboard_engine.execute({'action': 'mute'})
    assert mute_res['blackboard_muted'] is True
    muted_post = await brain.blackboard_engine.execute({'action': 'post', 'mentions': ['@research'], 'text': 'Muted Ping'})
    assert muted_post['ok'] is True
    assert muted_post['woken'] == []

    unmute_res = await brain.blackboard_engine.execute({'action': 'unmute'})
    assert unmute_res['blackboard_muted'] is False

    tok_hop = brain.hop_depth.set(2)
    try:
        hop_post = await brain.blackboard_engine.execute({'action': 'post', 'mentions': ['@research'], 'text': 'Depth 2 Ping'})
        assert hop_post['ok'] is True
        assert hop_post['woken'] == []
    finally:
        brain.hop_depth.reset(tok_hop)

    sched_res = await brain.blackboard_engine.execute({
        'action': 'schedule',
        'text': '每日晨间简报',
        'cron_expr': '30 9 * * 1-5',
        'exec_action': 'notify_user',
    })
    assert sched_res['ok'] is True
    assert sched_res['status'] == 'scheduled'
    assert sched_res['post']['schedule']['kind'] == 'routine'
    assert sched_res['post']['schedule']['cron_expr'] == '30 9 * * 1-5'
    assert sched_res['post']['due_at'] > 0
    assert next_cron_timestamp('*/15 * * * *', 1_700_000_000.0) > 1_700_000_000.0


@pytest.mark.asyncio
async def test_cloud_upload_alias_and_focused_type(tmp_path: Path, monkeypatch):
    brain = _make_brain(tmp_path, monkeypatch)
    calls: list[tuple] = []

    class FakeClient:
        async def upload_file(self, src_file: Path, remote_name: str = ''):
            calls.append(('upload_file', src_file.name, remote_name))
            return {'ok': True, 'path': f'workspace/uploads/{remote_name}', 'bytes': src_file.stat().st_size, 'sha256': 'abc'}

        async def call(self, method: str, path: str, body=None, **kw):
            calls.append((method, path, body))
            return {'ok': True, 'title': 'Cloud', 'url': 'https://example.org', 'x': 300, 'y': 200}

    brain.computer = types.SimpleNamespace(client=FakeClient())

    uploads_dir = brain.harness.root / 'uploads'
    uploads_dir.mkdir(parents=True, exist_ok=True)
    (uploads_dir / 'sample.txt').write_text('hello-cloud', encoding='utf-8')

    up_res = await brain._tool_cloud_desktop({'op': 'upload', 'path': 'sample.txt'})
    assert up_res['ok'] is True
    assert up_res['op'] == 'push_file'
    assert up_res['desktop_path'] == 'workspace/uploads/sample.txt'
    assert any(c[0] == 'upload_file' and c[2] == 'sample.txt' for c in calls)

    type_ok = await brain._tool_cloud_desktop({'op': 'type', 'text': 'hello focused', 'submit': True})
    assert type_ok['ok'] is True
    assert any(c[1] == '/input' and c[2]['kind'] == 'type' and c[2]['text'] == 'hello focused' and c[2]['submit'] is True for c in calls if len(c) > 2)

    type_blocked = await brain._tool_cloud_desktop({'op': 'type', 'text': 'nope', 'allow_focused': False})
    assert type_blocked['ok'] is False
    assert 'allow_focused=False' in str(type_blocked.get('error') or '')
