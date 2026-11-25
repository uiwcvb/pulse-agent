from __future__ import annotations

import base64
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from computer_models import InputAction
from core.blackboard_scheduler_helpers import TZ, parse_due_timestamp
from core.cloud_stuck_detector import CloudStuckDetector
from core.dsh_brain_adapter import DshBrainAdapter
from harness import Harness
from main import create_app
from plugins.gmail_plugin import GmailToolPlugin


def _build_test_app(tmp_path: Path):
    app = create_app(tmp_path, scheduler=False)
    store = app.state.store
    store.update_config({
        'api_key': 'sk-test-key',
        'default_model': 'deepseek-reasoner',
        'enabled': True,
    })
    return app, app.state.brain, store


# ---------------------------------------------------------------------------
# 1. Blackboard & Scheduler fixes (BUG-2026-11-22-03 & prompt/label/clear_done)
# ---------------------------------------------------------------------------


def test_parse_due_timestamp_accepts_iso_aliases_and_rejects_past_or_invalid():
    now = datetime(2026, 11, 22, 12, 0, 0, tzinfo=TZ).timestamp()
    due1, meta1 = parse_due_timestamp({'run_at': '2026-11-22 14:30'}, now)
    assert due1 > now
    assert meta1['kind'] == 'alarm'

    due2, _ = parse_due_timestamp({'at': '2026-11-22T07:00:00Z'}, now)
    assert due2 > now  # 15:00 Asia/Shanghai > 12:00 Asia/Shanghai

    due3, meta3 = parse_due_timestamp({'delay_minutes': 15}, now)
    assert due3 == pytest.approx(now + 900, abs=1.0)
    assert meta3['kind'] in ('alarm', 'once', 'timer')

    with pytest.raises(ValueError):
        parse_due_timestamp({'when': '2020-01-01 00:00'}, now)

    with pytest.raises(ValueError):
        parse_due_timestamp({'when': 'not-a-valid-date-time'}, now)


@pytest.mark.asyncio
async def test_blackboard_engine_rejects_past_schedule_unknown_action_and_clears_done(tmp_path: Path):
    _, brain, _ = _build_test_app(tmp_path)
    engine = brain.blackboard_engine

    # Past time must return explicit error (never silently fall through or schedule in the past)
    past_res = await engine.execute({
        'action': 'alarm',
        'prompt': '该吃饭了',
        'run_at': '2020-01-01 10:00',
        '_caller_bot_id': 'companion',
    })
    assert past_res['ok'] is False
    assert '早于' in past_res['error'] or '未来' in past_res['error']

    # Unparseable time must fail
    bad_time_res = await engine.execute({
        'action': 'schedule',
        'label': '提醒',
        'when': 'whenever-maybe',
        '_caller_bot_id': 'companion',
    })
    assert bad_time_res['ok'] is False

    # Valid future alarm with prompt/run_at works via _tool_blackboard and appears in list with scheduled_due_iso
    future_dt = (datetime.now(TZ) + timedelta(hours=2)).strftime('%Y-%m-%d %H:%M')
    ok_res = await brain._tool_blackboard({
        'action': 'alarm',
        'prompt': '午后巡检邮件',
        'at': future_dt,
        '_caller_bot_id': 'companion',
    })
    assert ok_res['ok'] is True
    post_id = (ok_res.get('post') or ok_res.get('topic') or {}).get('post_id') or ok_res.get('post_id')
    assert post_id

    listed = await engine.execute({'action': 'list', 'limit': 20})
    assert listed['ok'] is True
    matched = [t for t in listed['posts'] if t['post_id'] == post_id]
    assert len(matched) == 1
    assert matched[0].get('scheduled_due_iso')

    # Unknown action returns error with supported actions list rather than silently returning list
    unk = await engine.execute({'action': 'nonexistent_op_xyz'})
    assert unk['ok'] is False
    assert 'clear_done' in unk['error'] and 'alarm' in unk['error']

    # Post + complete -> clear_done archives done topic
    p = await engine.execute({
        'action': 'post',
        'label': '一次性闭环测试',
        '_caller_bot_id': 'companion',
    })
    pid = p['post']['post_id']
    await engine.execute({'action': 'complete', 'post_id': pid, 'summary': 'done', '_caller_bot_id': 'companion'})
    cleared = await engine.execute({'action': 'clear_done'})
    assert cleared['ok'] is True
    assert cleared['cleared'] >= 1


# ---------------------------------------------------------------------------
# 2. Phone Controller fixes (BUG-2026-11-22-04 & 06)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phone_controller_aliases_available_ops_live_status_and_stale_notifications(tmp_path: Path):
    _, brain, _ = _build_test_app(tmp_path)
    calls: list[tuple[str, dict]] = []

    async def fake_raw_phone_rpc(op: str, params: dict, timeout: float = 20.0):
        calls.append((op, dict(params)))
        if op == 'status':
            return {'ok': True, 'root': False, 'privilege_mode': 'none', 'device': {'brand': 'Xiaomi', 'model': '23116PN5BC', 'sdk': 35}}
        if op == 'shell' and params.get('command') == 'id -u':
            return {'ok': True, 'output': '0\n'}
        if op == 'notifications':
            return {'ok': False, 'error': 'phone RPC timeout after 8s'}
        return {'ok': True, 'op': op}

    brain._raw_phone_rpc = fake_raw_phone_rpc  # type: ignore[method-assign]

    # Pre-RPC rejection for unknown op includes available_ops
    bad_op = await brain._tool_phone({'op': 'imaginary_swipe_magic'})
    assert bad_op['ok'] is False
    avail = set(bad_op.get('available_ops') or [])
    assert {'back', 'home', 'app', 'status', 'notifications'}.issubset(avail)
    assert calls == []

    # Navigation & app aliases translate accurately
    await brain._tool_phone({'op': 'back'})
    await brain._tool_phone({'op': 'home'})
    await brain._tool_phone({'op': 'enter'})
    await brain._tool_phone({'op': 'keyevent', 'keycode': '4'})
    await brain._tool_phone({'op': 'app', 'target': 'com.tencent.mm'})
    action_calls = [(c_op, c_p) for c_op, c_p in calls if c_op in ('key', 'launch')]
    assert action_calls[0][0] == 'key' and action_calls[0][1].get('key') in ('back', 'KEYCODE_BACK')
    assert action_calls[1][0] == 'key' and action_calls[1][1].get('key') in ('home', 'KEYCODE_HOME')
    assert action_calls[2][0] == 'key' and action_calls[2][1].get('key') in ('enter', 'KEYCODE_ENTER')
    assert action_calls[3][0] == 'key' and action_calls[3][1].get('key') == '4'
    assert action_calls[4][0] == 'launch' and action_calls[4][1].get('app') == 'com.tencent.mm'

    # Live status reconciles root_live / privilege_live & top-level brand/model/sdk
    calls.clear()
    st = await brain._tool_phone({'op': 'state'})
    assert st['ok'] is True
    assert st['root_live'] is True
    assert st['privilege_live'] is True
    assert st['brand'] == 'Xiaomi' and st['model'] == '23116PN5BC' and st['sdk'] == 35

    # Stale cache notifications when RPC times out must be labeled explicitly
    nhub = getattr(brain, 'notification_hub', None) or brain.notif_hub
    nhub.ingest_raw_notifications([
        {'package': 'com.tencent.mm', 'app': '微信', 'title': '好友', 'text': '旧通知内容'},
    ])
    notif_res = await brain._tool_phone({'op': 'notifications'})
    assert notif_res['ok'] is False
    assert notif_res['live_polled'] is False
    assert notif_res['is_stale_cache'] is True
    assert 'cached_notifications' in notif_res and len(notif_res['cached_notifications']) >= 1
    assert not notif_res.get('notifications')
    assert notif_res.get('cache_warning')


# ---------------------------------------------------------------------------
# 3. Cloud Desktop lean shell, eval_js & push/pull fixes (BUG-2026-11-22-01/05)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_desktop_lean_shell_eval_js_and_push_pull_aliases(tmp_path: Path):
    _, brain, _ = _build_test_app(tmp_path)
    sent_calls: list[tuple[str, str, dict | None]] = []

    class FakeClient:
        async def status(self, force: bool = False):
            return {'ok': True}

        async def call(self, method: str, path: str, body: dict | None = None):
            sent_calls.append((method, path, dict(body) if isinstance(body, dict) else body))
            if path == '/input':
                kind = (body or {}).get('kind')
                if kind == 'bash':
                    return {'ok': True, 'op': 'bash', 'output': 'hi\n', 'exit_code': 0}
                if kind == 'eval_js':
                    return {'ok': True, 'op': 'eval_js', 'value': 'Example Domain'}
            if path == '/files':
                return {'ok': True, 'files': [{'path': 'workspace/report.md'}]}
            return {'ok': True}

        async def upload_file(self, src_file: Path, remote_name: str = ''):
            return {'ok': True, 'path': f'workspace/uploads/{remote_name or src_file.name}', 'bytes': src_file.stat().st_size}

        async def get_file(self, rel_p: str, dest: Path):
            dest.write_text('pulled', encoding='utf-8')
            return {'ok': True, 'bytes': 6}

    class FakeComputer:
        def __init__(self):
            self.client = FakeClient()

        def enabled(self):
            return True

    brain.computer = FakeComputer()  # type: ignore[assignment]

    # InputAction supports eval_js + script
    act = InputAction(kind='eval_js', script='document.title')
    assert act.kind == 'eval_js' and act.script == 'document.title'

    # cloud(op="bash") is lean by default (include_frame=False)
    res_lean = await brain._tool_cloud_desktop({'op': 'bash', 'command': 'echo hi'})
    assert res_lean['ok'] is True
    assert sent_calls[-1][1] == '/input' and sent_calls[-1][2]['kind'] == 'bash'
    assert sent_calls[-1][2]['include_frame'] is False

    # Passing screenshot=True opts back in to frame capture
    res_shot = await brain._tool_cloud_desktop({'op': 'bash', 'command': 'echo hi', 'screenshot': True})
    assert res_shot['ok'] is True
    assert sent_calls[-1][2]['include_frame'] is True

    # cloud(op="cdp"|"eval_js") maps cleanly & defaults lean
    res_js = await brain._tool_cloud_desktop({'op': 'cdp', 'script': 'document.title'})
    assert res_js['ok'] is True
    assert sent_calls[-1][2]['kind'] == 'eval_js' and sent_calls[-1][2]['script'] == 'document.title'
    assert sent_calls[-1][2]['include_frame'] is False

    # push reads existing nested path rather than failing when name differs
    nested = brain.harness.root / 'nested'
    nested.mkdir(parents=True, exist_ok=True)
    (nested / 'report.md').write_text('hello-push', encoding='utf-8')
    push_res = await brain._tool_cloud_desktop({
        'op': 'push',
        'path': 'nested/report.md',
        'name': 'report_renamed.md',
    })
    assert push_res['ok'] is True
    assert push_res['workspace_path'] == 'nested/report.md'
    assert push_res['name'] == 'report_renamed.md'

    pull_res = await brain._tool_cloud_desktop({'op': 'pull', 'src': '~/workspace/report.md'})
    assert pull_res['ok'] is True
    assert pull_res['source_path'] == 'workspace/report.md'

    bad = await brain._tool_cloud_desktop({'op': 'invalid_cloud_op'})
    assert bad['ok'] is False and 'available_ops' in bad


# ---------------------------------------------------------------------------
# 4. Harness read_file / path / bash node & locate / PC-offline bash guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_harness_path_read_file_and_bash_pc_offline_guard(tmp_path: Path):
    _, brain, _ = _build_test_app(tmp_path)
    h = brain.harness

    # Normalizes workspace/vault POSIX absolute paths and /workspace-media/ URLs
    sample = h.root / 'notes_demo.txt'
    lines = [f'line-{i:02d}' + (' needle_target [literal]' if i == 15 else '') for i in range(1, 31)]
    sample.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    assert h.path(sample.as_posix()) == sample.resolve()

    vault_note = brain.vault.root / 'notes' / 'abs_check.md'
    vault_note.parent.mkdir(parents=True, exist_ok=True)
    vault_note.write_text('# OK\n', encoding='utf-8')
    assert h.path(vault_note.as_posix()) == vault_note.resolve()

    with pytest.raises(ValueError):
        h.path('/etc/passwd')

    # tail support
    tail_res = await h.execute('read_file', {'path': 'notes_demo.txt', 'tail': 3})
    assert 'error' not in tail_res
    assert 'line-28' in tail_res['content'] and 'line-30' in tail_res['content']
    assert 'line-01' not in tail_res['content']

    # grep + literal + context support
    grep_res = await h.execute('read_file', {
        'path': 'notes_demo.txt',
        'grep': '[literal]',
        'literal': True,
        'context': 1,
    })
    assert 'error' not in grep_res
    assert 'line-14' in grep_res['content'] and 'line-15' in grep_res['content'] and 'line-16' in grep_res['content']
    assert 'line-01' not in grep_res['content']

    # Truncation banner is placed at top of content
    big = h.root / 'big.txt'
    big.write_text('A' * 12000, encoding='utf-8')
    trunc = await h.execute('read_file', {'path': 'big.txt', 'max_chars': 100})
    assert 'error' not in trunc and trunc.get('truncated') is True
    assert trunc['content'].startswith('[⚠️ 已截断')

    # Local sandbox bash labels node="hub_linux_sandbox" (simulated Linux host when running suite on Windows)
    class _FakeStdout:
        def __init__(self, payload: bytes):
            self._buf = payload
            self._done = False

        async def read(self, _n: int = 4096) -> bytes:
            if self._done:
                return b''
            self._done = True
            return self._buf

    class _FakeProc:
        def __init__(self):
            self.pid = 424242
            self.returncode = 0
            self.stdout = _FakeStdout(b'ok\n')

        async def wait(self):
            return 0

        def kill(self):
            pass

    async def _fake_exec(*a, **kw):
        return _FakeProc()

    with patch('harness.sys.platform', 'linux'), patch('harness.asyncio.create_subprocess_exec', _fake_exec):
        sh_res = await h.execute('bash', {'command': 'echo ok'})
    assert 'error' not in sh_res
    assert int(sh_res.get('exit_code', -1)) == 0
    assert sh_res.get('node') == 'hub_linux_sandbox'

    # bash targeting offline pc_windows MUST error explicitly instead of falling back to Linux sandbox
    pc_off = await brain._tool_bash({'command': 'echo wrong_host', 'target': 'pc_windows'})
    assert pc_off['ok'] is False
    assert '未在线' in pc_off['error'] and pc_off.get('pc_online') is False and pc_off.get('node') == 'pc_offline'

    # search_files(op="locate") scans local hub workspace & memory_vault
    loc = await brain._tool_search_files({'op': 'locate', 'pattern': '*abs_check*'})
    assert loc['ok'] is True
    assert loc['count'] >= 1
    assert any(m.get('node') == 'memory_vault' for m in loc['matches'])


# ---------------------------------------------------------------------------
# 5. CloudStuckDetector & GmailToolPlugin fixes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_stuck_detector_forbids_stale_ref_loop(tmp_path: Path):
    det = CloudStuckDetector()
    unchanged = {'ok': True, 'ui_changed': False, 'frame_hash': 'same01'}
    assert det.observe('cloud', {'op': 'click', 'ref': '@b42', 'nx': 300, 'ny': 400}, unchanged).stage == 'ok'
    assert det.observe('cloud', {'op': 'click', 'ref': '@b42', 'nx': 302, 'ny': 401}, unchanged).stage == 'ok'
    nudge = det.observe('cloud', {'op': 'click', 'ref': '@b42', 'nx': 301, 'ny': 399}, unchanged)
    assert nudge.stage == 'nudge' and nudge.rule == 'visual_stagnation'
    assert 'eval_js' in nudge.message
    assert '@bN' in nudge.message

    # End-to-end via DshBrainAdapter.execute_tool_from_sidecar:
    # 3 unchanged cloud clicks emit _stuck_nudge on the returned tool payload
    _, brain, _ = _build_test_app(tmp_path)
    adapter = getattr(brain, '_dsh_adapter', None) or DshBrainAdapter(brain)
    adapter._get_or_init_turn_acc('turn_stuck_1', turn_origin='phone')

    async def fake_harness_exec(name: str, args: dict):
        return {'ok': True, 'op': 'click', 'ui_changed': False, 'frame_hash': 'frozen_frame_1'}

    brain.harness.execute = fake_harness_exec  # type: ignore[method-assign]
    r1 = await adapter.execute_tool_from_sidecar(name='cloud', args={'op': 'click', 'ref': '@b42', 'nx': 300, 'ny': 400}, bot_id='companion', source='phone', turn_id='turn_stuck_1')
    r2 = await adapter.execute_tool_from_sidecar(name='cloud', args={'op': 'click', 'ref': '@b42', 'nx': 302, 'ny': 401}, bot_id='companion', source='phone', turn_id='turn_stuck_1')
    r3 = await adapter.execute_tool_from_sidecar(name='cloud', args={'op': 'click', 'ref': '@b42', 'nx': 301, 'ny': 399}, bot_id='companion', source='phone', turn_id='turn_stuck_1')
    assert '_stuck_nudge' not in r1['result']
    assert '_stuck_nudge' not in r2['result']
    assert 'eval_js' in r3['result'].get('_stuck_nudge', '')


@pytest.mark.asyncio
async def test_gmail_plugin_mime_decode_oauth_refresh_and_needs_reauth(tmp_path: Path):
    ws = tmp_path / 'ws'
    ws.mkdir(parents=True, exist_ok=True)
    raw_text = base64.urlsafe_b64encode('验证码是 987654，请查收！'.encode('utf-8')).decode('ascii').rstrip('=')
    payload = {
        'mimeType': 'multipart/alternative',
        'parts': [
            {'mimeType': 'text/plain', 'body': {'data': raw_text}},
            {'mimeType': 'text/html', 'body': {'data': raw_text}},
        ],
    }
    decoded = GmailToolPlugin._extract_gmail_payload_body(payload)
    assert '987654' in decoded

    # Write expired oauth_token + refresh_token and test 401 -> force refresh retry succeeds
    cfg_dir = ws / '.config'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / 'gmail_auth.json').write_text(
        '{"oauth_token":"expired_tok","refresh_token":"ref_tok","client_id":"cid","client_secret":"csec","expires_at":9999999999}',
        encoding='utf-8',
    )

    plugin = GmailToolPlugin()
    call_log: list[str] = []

    class FakeResp:
        def __init__(self, status_code: int, body: dict):
            self.status_code = status_code
            self._body = body
            self.text = str(body)

        def json(self):
            return self._body

    class FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, headers=None, params=None, **kw):
            return await self.get(url, headers=headers, params=params)

        async def get(self, url, headers=None, params=None):
            call_log.append(f'GET:{(headers or {}).get("Authorization")}')
            if (headers or {}).get('Authorization') == 'Bearer expired_tok':
                return FakeResp(401, {'error': 'invalid_token'})
            return FakeResp(200, {'messages': []})

        async def post(self, url, data=None, **kw):
            call_log.append(f'POST:{url}')
            return FakeResp(200, {'access_token': 'fresh_tok', 'expires_in': 3600})

    with patch('plugins.gmail_plugin.httpx.AsyncClient', FakeAsyncClient):
        res_ok = await plugin.execute({'action': 'list'}, {'root': str(ws), 'data_dir': str(tmp_path)})
    assert res_ok['ok'] is True
    assert 'GET:Bearer expired_tok' in call_log
    assert 'POST:https://oauth2.googleapis.com/token' in call_log
    assert 'GET:Bearer fresh_tok' in call_log

    # Unrecoverable 401 without refresh/IMAP returns structured needs_reauth + auth_url
    (cfg_dir / 'gmail_auth.json').write_text(
        '{"oauth_token":"dead_tok","client_id":"cid_only"}',
        encoding='utf-8',
    )

    class DeadAsyncClient(FakeAsyncClient):
        async def get(self, url, headers=None, params=None):
            return FakeResp(401, {'error': 'revoked'})

    with patch('plugins.gmail_plugin.httpx.AsyncClient', DeadAsyncClient):
        res_dead = await plugin.execute({'action': 'list'}, {'root': str(ws), 'data_dir': str(tmp_path)})
    assert res_dead['ok'] is False
    assert res_dead.get('needs_reauth') is True
    assert 'auth_url' in res_dead and 'cid_only' in res_dead['auth_url']
    assert len(res_dead.get('reauth_options') or []) >= 2


@pytest.mark.asyncio
async def test_bot_roster_node_isolation_across_ws_and_http(tmp_path: Path):
    import asyncio
    from fastapi.testclient import TestClient

    app, brain, store = _build_test_app(tmp_path)
    se = brain.sync_engine

    # 1. sync_engine._prepare_bots_event must strictly scope bots by node source
    ev_phone = se._prepare_bots_event(bot_id='pc_engineer', source='phone', force=True)
    assert ev_phone is not None
    assert ev_phone['bots']
    assert all((b.get('home_node') or 'phone') not in ('pc', 'pc2') for b in ev_phone['bots'])

    ev_pc = se._prepare_bots_event(bot_id='pc_engineer', source='pc', force=True)
    assert ev_pc is not None
    assert ev_pc['bots']
    assert all((b.get('home_node') or 'phone') in ('pc', 'pc2', 'all') for b in ev_pc['bots'])

    # 2. publish('bots_changed') must deliver per-client scoped rosters to phone vs pc WS queues
    q_phone: asyncio.Queue = asyncio.Queue(16)
    q_pc: asyncio.Queue = asyncio.Queue(16)
    pub_fn = brain.publish
    freevars = pub_fn.__code__.co_freevars
    clients_map = pub_fn.__closure__[freevars.index('clients')].cell_contents
    clients_map['test_phone_ws'] = {'queue': q_phone, 'source': 'phone', 'device_id': 'dev_ph'}
    clients_map['test_pc_ws'] = {'queue': q_pc, 'source': 'pc', 'device_id': 'dev_pc'}
    try:
        await se.notify_bots_changed(bot_id='pc_engineer', source='pc', force=True)
        got_phone = q_phone.get_nowait()
        got_pc = q_pc.get_nowait()
        assert got_phone['type'] == 'bots_changed'
        assert got_pc['type'] == 'bots_changed'
        assert all((b.get('home_node') or 'phone') not in ('pc', 'pc2') for b in got_phone['bots'])
        assert all((b.get('home_node') or 'phone') in ('pc', 'pc2', 'all') for b in got_pc['bots'])
        assert got_phone['primary_bot_id'] != 'pc_engineer'
    finally:
        clients_map.pop('test_phone_ws', None)
        clients_map.pop('test_pc_ws', None)

    # 3. PUT /api/bots/primary and DELETE /api/bots/{id} must return only the caller's node bots
    tok = (tmp_path / 'device-token').read_text('utf-8').strip()
    with TestClient(app) as client:
        hdrs = {'Authorization': f'Bearer {tok}', 'X-Node-Source': 'phone'}
        r_prim = client.put('/api/bots/primary?source=phone', headers=hdrs, json={'bot_id': 'companion'})
        assert r_prim.status_code == 200
        prim_bots = r_prim.json()['bots']
        assert prim_bots and all((b.get('home_node') or 'phone') not in ('pc', 'pc2') for b in prim_bots)

        r_new = client.post('/api/bots?source=phone', headers=hdrs, json={'name': '临时手机测试助手', 'home_node': 'phone'})
        assert r_new.status_code == 200
        new_id = r_new.json()['id']

        r_del = client.delete(f'/api/bots/{new_id}?source=phone', headers=hdrs)
        assert r_del.status_code == 200
        del_bots = r_del.json()['bots']
        assert del_bots and all((b.get('home_node') or 'phone') not in ('pc', 'pc2') for b in del_bots)


@pytest.mark.asyncio
async def test_dsh_immediate_media_push_and_block_sleep_after_screenshot(tmp_path: Path):
    _, brain, _ = _build_test_app(tmp_path)
    adapter = DshBrainAdapter(brain)
    turn_id = 'turn_media_push_1'
    adapter._get_or_init_turn_acc(turn_id)

    emitted_calls: list[dict] = []

    async def fake_emit(text, kind='curiosity', track=True, **extra):
        emitted_calls.append({'text': text, 'kind': kind, 'track': track, **extra})
        return {'id': 'msg_push_1', 'text': text, **extra}

    async def fake_exec(name, args):
        return {
            'ok': True,
            'op': 'send_screenshot',
            'chat_image_url': '/workspace-media/qr-pay.jpg',
        }

    with patch.object(brain, 'emit', side_effect=fake_emit), patch.object(brain.harness, 'execute', side_effect=fake_exec):
        res1 = await adapter.execute_tool_from_sidecar(
            name='cloud',
            args={'op': 'send_screenshot'},
            bot_id='companion',
            source='phone',
            turn_id=turn_id,
        )
        assert res1['ok'] is True
        assert res1['result'].get('delivered_immediately') is True
        assert len(emitted_calls) == 1
        assert emitted_calls[0]['non_terminal'] is True
        assert emitted_calls[0]['stickers'] == ['/workspace-media/qr-pay.jpg']
        assert adapter._turn_acc[turn_id]['stickers'] == []
        assert adapter._turn_acc[turn_id]['emitted_stickers'] == ['/workspace-media/qr-pay.jpg']


@pytest.mark.asyncio
async def test_blackboard_self_mention_deferred_and_hop_callback(tmp_path: Path):
    _, brain, _ = _build_test_app(tmp_path)
    # 1. Explicit self-mention in a scheduled post must still resolve to author (not empty targets!)
    res = await brain.blackboard_engine.execute({
        'action': 'schedule',
        'text': '@companion 提醒主人核对订单',
        'delay_minutes': 5,
        '_caller_bot_id': 'companion',
        '_caller_source': 'phone',
    })
    assert res['ok'] is True
    assert 'companion' in res['post']['target_bot_ids']

    # 2. Completion callback (`黑板任务结项回执`) must not be dropped even at MAX_BLACKBOARD_HOP_DEPTH
    hop_tok = brain.hop_depth.set(brain.blackboard_engine.MAX_BLACKBOARD_HOP_DEPTH)
    try:
        with patch.object(brain.blackboard_engine, '_async_wake_bot', new=AsyncMock()):
            woken = brain.blackboard_engine._schedule_wakeup(
                res['post'],
                [('companion', 'companion', 'phone')],
                reason_label='黑板任务结项回执',
            )
            assert len(woken) == 1
    finally:
        brain.hop_depth.reset(hop_tok)


@pytest.mark.asyncio
async def test_bg_task_exit_wakes_idle_ai_via_run_scheduled(tmp_path: Path):
    import asyncio
    _, brain, _ = _build_test_app(tmp_path)
    woke_calls: list[dict] = []

    async def fake_run_scheduled(goal: str, bot_id: str | None = None, label: str = ''):
        woke_calls.append({'goal': goal, 'bot_id': bot_id, 'label': label})
        return {'ok': True}

    token_bot = brain.bot_context.set('companion')
    token_src = brain.source_context.set('phone')
    try:
        with patch.object(brain, 'run_scheduled', side_effect=fake_run_scheduled):
            res = await brain._tool_bg_task({'action': 'start', 'command': 'echo BG_DONE_OK', 'notify_on_exit': True})
            assert res.get('started') is True
            tid = res['task_id']
            for _ in range(40):
                if woke_calls:
                    break
                await asyncio.sleep(0.1)
            assert len(woke_calls) == 1
            assert woke_calls[0]['bot_id'] == 'companion'
            assert tid in woke_calls[0]['goal']
            assert 'BG_DONE_OK' in woke_calls[0]['goal']
    finally:
        brain.bot_context.reset(token_bot)
        brain.source_context.reset(token_src)


@pytest.mark.asyncio
async def test_search_files_grep_no_recursion_error_and_subtask_graceful_finish(tmp_path: Path):
    _, brain, _ = _build_test_app(tmp_path)
    # 1. search_files(op="grep") must dispatch to grep_search without RecursionError
    (brain.harness.root / 'demo.txt').write_text('hello frp tunnel 18787\n', encoding='utf-8')
    res_grep = await brain._tool_search_files({'op': 'grep', 'query': 'frp'})
    assert isinstance(res_grep, dict)
    assert 'error' not in res_grep

    # 2. subtask uses silent LLM (on_stream=None) and forces clean summary on final step
    seen_on_stream: list[object] = []
    call_n = {'n': 0}

    async def fake_complete_stream(**kwargs):
        seen_on_stream.append(kwargs.get('on_stream'))
        call_n['n'] += 1
        if call_n['n'] == 1:
            return {
                'content': '',
                'tool_calls': [{'id': 'c1', 'function': {'name': 'search_files', 'arguments': '{"op":"grep","query":"frp"}'}}],
            }
        # Final step has tools=None and produces clean summary
        assert kwargs.get('tools') is None
        return {'content': '查明：demo.txt 中包含 frp tunnel 18787。'}

    with patch.object(brain.llm, 'complete_stream', side_effect=fake_complete_stream):
        sub_res = await brain._tool_run_subtask({'goal': '查明 frp 端口', 'max_steps': 2, 'wait': True})
    assert sub_res['ok'] is True
    assert '18787' in sub_res['result_summary']
    assert all(cb is None for cb in seen_on_stream)


@pytest.mark.asyncio
async def test_blackboard_string_watch_and_compact_read(tmp_path: Path):
    _, brain, _ = _build_test_app(tmp_path)
    # 1. String watch + trigger_value + watch_interval_sec must attach watch_active=True (BUG-2026-11-23-06)
    res_w = await brain.blackboard_engine.execute({
        'action': 'post',
        'text': '监控域名解析状态',
        'watch': 'curl -s https://example.com',
        'trigger_value': '"Status":0',
        'watch_interval_sec': 120,
        '_caller_bot_id': 'companion',
        '_caller_source': 'phone',
    })
    assert res_w['ok'] is True
    assert res_w['watch_active'] is True
    assert res_w['post']['watch']['kind'] == 'cmd_contains'
    assert res_w['post']['watch']['expect'] == '"Status":0'
    assert res_w['post']['watch']['interval_sec'] == 120

    # 2. blackboard(read, limit=2) must return compact summaries instead of 300KB full replies (BUG-2026-11-22-10)
    for i in range(5):
        p = await brain.blackboard_engine.execute({
            'action': 'post',
            'text': f'长帖 {i} ' + ('A' * 1500),
            '_caller_bot_id': 'companion',
            '_caller_source': 'phone',
        })
        await brain.blackboard_engine.execute({
            'action': 'reply',
            'post_id': p['post_id'],
            'text': 'B' * 2000,
            '_caller_bot_id': 'companion',
            '_caller_source': 'phone',
        })
    res_r = await brain.blackboard_engine.execute({
        'action': 'read',
        'limit': 2,
        '_caller_bot_id': 'companion',
        '_caller_source': 'phone',
    })
    assert res_r['ok'] is True
    assert len(res_r['active_posts']) <= 2
    assert 'replies' not in res_r['active_posts'][0]
    assert len(res_r['active_posts'][0]['text']) <= 320


def test_compaction_summary_validator_accepts_fenced_and_english_headings():
    from core.compaction_service import is_valid_compaction_summary
    raw = "```markdown\n<compaction_summary>\n## Objective\nDeploy snowline blog to fubuki.com.cn.\n\n## Next Move\nVerify TLS.\n</compaction_summary>\n```"
    res = is_valid_compaction_summary(raw)
    assert res[0] is True
    assert res[1].startswith('### 📋 核心进展与上下文摘要')
    assert '```' not in res[1]
    assert '<compaction_summary>' not in res[1]






