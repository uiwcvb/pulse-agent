from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient

from main import create_app
from security import CryptoBox, b64, unb64


def _rsa_wrap_key(public_key_b64: str, aes_key: bytes) -> str:
    pub = serialization.load_der_public_key(unb64(public_key_b64))
    wrapped = pub.encrypt(
        aes_key,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return b64(wrapped)


def _make_open_envelope(crypto_pub: dict, aes_key: bytes, payload: dict) -> dict:
    sealed = CryptoBox.seal(aes_key, payload)
    sealed['key'] = _rsa_wrap_key(crypto_pub['public_key'], aes_key)
    return sealed


def test_cryptobox_pre_encryption_gzip_and_legacy_compat(tmp_path: Path):
    crypto = CryptoBox(tmp_path)
    aes_key = os.urandom(32)
    pub = crypto.public()

    small_payload = {'ok': True, 'timestamp': time.time(), 'msg': 'hi'}
    small_env = CryptoBox.seal(aes_key, small_payload)
    assert 'z' not in small_env
    assert CryptoBox.frame(aes_key, small_env) == small_payload

    big_payload = {
        'ok': True,
        'timestamp': time.time(),
        'items': [{'idx': i, 'text': '重复的高冗余同步消息块 ' * 4} for i in range(40)],
    }
    big_sealed = CryptoBox.seal(aes_key, big_payload)
    assert big_sealed.get('z') == 1
    plain_json_len = len(json.dumps(big_payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    assert len(unb64(big_sealed['data'])) < plain_json_len * 0.4
    assert CryptoBox.frame(aes_key, big_sealed) == big_payload

    # Verify CryptoBox.open handles compressed client envelope too
    open_env = _make_open_envelope(pub, aes_key, big_payload)
    assert open_env.get('z') == 1
    out_key, decoded = crypto.open(open_env)
    assert out_key == aes_key
    assert decoded['items'][15]['idx'] == 15

    # Verify legacy uncompressed envelope still decodes cleanly
    legacy_raw = json.dumps(big_payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    legacy_env = {'nonce': b64(nonce), 'data': b64(AESGCM(aes_key).encrypt(nonce, legacy_raw, None))}
    assert CryptoBox.frame(aes_key, legacy_env) == big_payload


def test_ws_rpc_multiplexing_and_connected_handshake(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('AGENT_TOKEN', 'test-token-v42')
    app = create_app(tmp_path, scheduler=False)
    aes_key = os.urandom(32)

    with TestClient(app) as client:
        pub = client.get('/api/transport').json()
        with client.websocket_connect('/ws/client') as ws:
            hello = _make_open_envelope(
                pub,
                aes_key,
                {'token': 'test-token-v42', 'source': 'phone', 'timestamp': time.time()},
            )
            ws.send_json(hello)
            connected = CryptoBox.frame(aes_key, ws.receive_json())
            assert connected['type'] == 'connected'
            assert connected['state_seq'] >= 1
            assert isinstance(connected.get('bots_ver'), str) and connected['bots_ver']
            assert isinstance(connected.get('pins_ver'), str) and connected['pins_ver']
            assert isinstance(connected.get('pins'), list)
            init_seq = int(connected['state_seq'])

            # Multiplexed GET /api/status via WS rpc_req
            ws.send_json(CryptoBox.seal(aes_key, {
                'type': 'rpc_req',
                'id': 'rpc-status-1',
                'method': 'GET',
                'path': '/api/status',
                'query': {'bots_ver': connected['bots_ver'], 'pins_ver': connected['pins_ver']},
            }))
            rpc_res = CryptoBox.frame(aes_key, ws.receive_json())
            assert rpc_res['type'] == 'rpc_res'
            assert rpc_res['id'] == 'rpc-status-1'
            assert rpc_res['status'] == 200
            assert rpc_res['body']['bots_unchanged'] is True
            assert 'bots' not in rpc_res['body']
            assert rpc_res['body']['pins_unchanged'] is True

            # Security gate blocks /api/secure or path traversal inside rpc_req cleanly
            ws.send_json(CryptoBox.seal(aes_key, {
                'type': 'rpc_req',
                'id': 'rpc-bad-1',
                'method': 'GET',
                'path': '/api/secure',
                'query': {},
            }))
            bad_res = CryptoBox.frame(aes_key, ws.receive_json())
            assert bad_res['type'] == 'rpc_res'
            assert bad_res['id'] == 'rpc-bad-1'
            assert bad_res['status'] == 400

            # POST /api/memories/pins via WS rpc_req returns pin & pushes ONE deduplicated pins_changed event
            ws.send_json(CryptoBox.seal(aes_key, {
                'type': 'rpc_req',
                'id': 'rpc-pin-1',
                'method': 'POST',
                'path': '/api/memories/pins',
                'body': {'action': 'add', 'text': 'WS RPC 实时同步记忆'},
            }))
            frames = [CryptoBox.frame(aes_key, ws.receive_json()), CryptoBox.frame(aes_key, ws.receive_json())]
            by_type = {f['type']: f for f in frames}
            assert 'rpc_res' in by_type and by_type['rpc_res']['id'] == 'rpc-pin-1'
            assert by_type['rpc_res']['status'] == 200
            assert by_type['rpc_res']['body']['pinned'] is True
            assert 'pins_changed' in by_type
            assert int(by_type['pins_changed']['state_seq']) > init_seq
            assert any('WS RPC 实时同步记忆' in p['text'] for p in by_type['pins_changed']['pins'])


@pytest.mark.asyncio
async def test_monotonic_messages_and_bg_task_non_terminal(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('AGENT_TOKEN', 'test-token-v42')
    app = create_app(tmp_path, scheduler=False)
    store = app.state.store
    brain = app.state.brain

    # Even if wall clock moves backwards, created_at inside Store.message stays strictly monotonic
    fake_clock = [2_000_000_000.0, 1_999_999_900.0, 1_999_999_950.0]
    monkeypatch.setattr(time, 'time', lambda: fake_clock.pop(0) if fake_clock else 2_000_000_005.0)
    m1 = store.message('user', 'first', bot_id='companion', source='pc')
    m2 = store.message('assistant', 'second', bot_id='companion', source='pc')
    m3 = store.message('assistant', 'third', bot_id='companion', source='pc')
    assert m2['created_at'] > m1['created_at']
    assert m3['created_at'] > m2['created_at']
    monkeypatch.undo()

    published: list[dict] = []

    async def capture(ev: dict):
        published.append(ev)

    brain.publish = capture  # type: ignore[method-assign]
    src_tok = brain.source_context.set('pc')
    bot_tok = brain.bot_context.set('companion')
    bot_lock = brain._get_bot_lock('companion', 'pc')
    await bot_lock.acquire()
    try:
        brain.executions_by_node_bot['pc:companion'] = {'bot_id': 'companion', 'source': 'pc', 'goal': 'running turn', 'trace': []}
        brain.streams_by_node_bot['pc:companion'] = {
            'bot_id': 'companion', 'source': 'pc', 'turn_id': 'turn-live-1', 'text': '正在继续生成...', 'seq': 10,
        }
        cmd = f'"{sys.executable}" -c "print(\'bg_done_ok\')"'
        res = await brain._tool_bg_task({'action': 'start', 'command': cmd, 'notify_on_exit': True})
        assert res['started'] is True

        deadline = time.perf_counter() + 4.0
        while time.perf_counter() < deadline:
            if any(ev.get('type') == 'message' and ev.get('message', {}).get('is_bg_result') for ev in published):
                break
            await asyncio.sleep(0.05)

        bg_msgs = [ev['message'] for ev in published if ev.get('type') == 'message' and ev.get('message', {}).get('is_bg_result')]
        assert len(bg_msgs) == 1
        bg_m = bg_msgs[0]
        assert bg_m['source'] == 'pc'
        assert bg_m['non_terminal'] is True
        assert bg_m['is_bg_result'] is True
        assert str(bg_m['turn_id']).startswith('bg-')
        assert 'bg_done_ok' in bg_m['text']

        # Urgent step-boundary interrupt queued for busy bot + active stream re-published after bg result
        q = brain.urgent_interrupts_by_node_bot.get('pc:companion', [])
        assert any(it.get('kind') == 'bg_task' for it in q)
        msg_idx = next(i for i, ev in enumerate(published) if ev.get('type') == 'message' and ev.get('message', {}).get('is_bg_result'))
        assert any(ev.get('type') == 'stream' and ev.get('stream', {}).get('turn_id') == 'turn-live-1' for ev in published[msg_idx + 1:])
    finally:
        brain.executions_by_node_bot.pop('pc:companion', None)
        brain.streams_by_node_bot.pop('pc:companion', None)
        bot_lock.release()
        brain.bot_context.reset(bot_tok)
        brain.source_context.reset(src_tok)


def test_realtime_state_sync_events_and_hash_gating(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('AGENT_TOKEN', 'test-token-v42')
    app = create_app(tmp_path, scheduler=False)
    aes_key = os.urandom(32)
    headers = {'Authorization': 'Bearer test-token-v42'}

    with TestClient(app) as client:
        pub = client.get('/api/transport').json()
        with client.websocket_connect('/ws/client') as ws:
            ws.send_json(_make_open_envelope(pub, aes_key, {'token': 'test-token-v42', 'source': 'phone', 'timestamp': time.time()}))
            conn = CryptoBox.frame(aes_key, ws.receive_json())
            assert conn['type'] == 'connected'

            # 1. Full /api/sync omits nothing when bots_ver/config_ver not passed
            full_sync = client.get('/api/sync?source=phone', headers=headers).json()
            assert 'bots' in full_sync and len(full_sync['bots']) >= 1
            assert 'config' in full_sync
            b_ver = full_sync['bots_ver']
            c_ver = full_sync['config_ver']
            p_ver = full_sync['pins_ver']

            # 2. Hash-gated /api/sync omits unchanged heavy objects
            delta_sync = client.get(
                f'/api/sync?source=phone&bots_ver={b_ver}&config_ver={c_ver}&pins_ver={p_ver}',
                headers=headers,
            ).json()
            assert delta_sync['bots_unchanged'] is True
            assert 'bots' not in delta_sync
            assert delta_sync['config_unchanged'] is True
            assert 'config' not in delta_sync
            assert delta_sync['pins_unchanged'] is True
            assert 'pins' not in delta_sync

            # 3. Put vault file -> vault_changed pushed with state_seq
            r_vault = client.put(
                '/api/vault',
                json={'path': 'notes/sync_check.md', 'content': '# Hello Sync'},
                headers=headers,
            )
            assert r_vault.status_code == 200
            ev_v = CryptoBox.frame(aes_key, ws.receive_json())
            assert ev_v['type'] == 'vault_changed'
            assert ev_v['path'] == 'notes/sync_check.md'
            assert ev_v['state_seq'] > conn['state_seq']

            # 4. Create + toggle routine -> schedules_changed pushed with incremented state_seq
            r_cr = client.post(
                '/api/routines',
                json={'goal': '每天检查同步状态', 'interval_hours': 24, 'bot_id': 'companion'},
                headers=headers,
            )
            assert r_cr.status_code == 200
            rid = r_cr.json()['id']
            # Flush vault + schedules events from create_routine
            evs = [CryptoBox.frame(aes_key, ws.receive_json()), CryptoBox.frame(aes_key, ws.receive_json())]
            sched_evs = [e for e in evs if e.get('type') == 'schedules_changed']
            assert len(sched_evs) == 1
            prev_seq = sched_evs[0]['state_seq']

            r_tog = client.put(f'/api/routines/{rid}', json={'enabled': False}, headers=headers)
            assert r_tog.status_code == 200
            evs2 = [CryptoBox.frame(aes_key, ws.receive_json()), CryptoBox.frame(aes_key, ws.receive_json())]
            sched_evs2 = [e for e in evs2 if e.get('type') == 'schedules_changed']
            assert len(sched_evs2) == 1
            assert sched_evs2[0]['state_seq'] > prev_seq

            # 5. Placement change -> bots_changed pushed + invalidates old bots_ver in /api/sync
            r_place = client.patch(
                '/api/bots/companion/placement',
                json={'pinned': True, 'source': 'phone'},
                headers=headers,
            )
            assert r_place.status_code == 200
            ev_bot = CryptoBox.frame(aes_key, ws.receive_json())
            assert ev_bot['type'] == 'bots_changed'
            assert ev_bot['bots_ver'] != b_ver

            re_sync = client.get(
                f'/api/sync?source=phone&bots_ver={b_ver}&config_ver={c_ver}&pins_ver={p_ver}',
                headers=headers,
            ).json()
            assert re_sync['bots_unchanged'] is False
            assert 'bots' in re_sync
            assert re_sync['config_unchanged'] is True
            assert 'config' not in re_sync
