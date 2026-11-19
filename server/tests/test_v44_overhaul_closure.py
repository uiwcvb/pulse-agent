"""v4.0 overhaul closure tests covering SQLite WAL/order_seq, inbox sync_versions, VisionEngine v4.0, and HTTP-400 multimodal fallback."""
import asyncio
import base64
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from core.vision_engine import (  # noqa: E402
    compute_frame_diff_pct,
    draw_vision_ruler_overlay,
    zoom_region_from_args,
)
from main import create_app  # noqa: E402
from security import CryptoBox  # noqa: E402
from vault_manager import TZ  # noqa: E402


def _make_open_envelope(crypto_pub: dict, aes_key: bytes, payload: dict) -> dict:
    pub_der = base64.b64decode(crypto_pub['public_key'])
    pub_key = serialization.load_der_public_key(pub_der)
    enc_key = pub_key.encrypt(
        aes_key,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    sealed = CryptoBox.seal(aes_key, payload)
    sealed['key'] = base64.b64encode(enc_key).decode('ascii')
    return sealed


def _make_jpeg_bytes(color: tuple[int, int, int], box: tuple[int, int, int, int] | None = None) -> bytes:
    img = Image.new('RGB', (320, 200), color)
    if box is not None:
        ImageDraw.Draw(img).rectangle(box, fill=(245, 245, 245))
    buf = BytesIO()
    img.save(buf, format='JPEG', quality=88)
    return buf.getvalue()


def test_sqlite_wal_and_deterministic_order_seq(tmp_path: Path):
    app = create_app(tmp_path, scheduler=False)
    with TestClient(app):
        db_path = tmp_path / 'pulse_state.db'
        assert db_path.exists()
        conn = sqlite3.connect(db_path)
        try:
            mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
            assert str(mode).lower() == 'wal'
        finally:
            conn.close()

        store = app.state.store
        m1 = store.message('assistant', 'first foreground message', kind='chat', bot_id='companion', source='phone')
        m2 = store.message(
            'assistant',
            'late background command result with earlier created',
            kind='chat',
            bot_id='companion',
            source='phone',
            created=1000.0,
        )
        m3 = store.message('user', 'third message after command', kind='chat', bot_id='companion', source='phone')
        assert 0 < m1['order_seq'] < m2['order_seq'] < m3['order_seq']

        store.save()
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute('SELECT id, order_seq FROM messages ORDER BY order_seq ASC').fetchall()
        finally:
            conn.close()
        ids_in_order = [r[0] for r in rows]
        assert ids_in_order[-3:] == [m1['id'], m2['id'], m3['id']]


def test_dsh_inbox_sync_versions_and_single_pins_changed_ws_dedupe(tmp_path: Path):
    app = create_app(tmp_path, scheduler=False)
    token = (tmp_path / 'device-token').read_text('utf-8').strip()
    headers = {'Authorization': f'Bearer {token}'}
    aes_key = os.urandom(32)

    with TestClient(app) as client:
        inbox = client.post('/api/dsh/inbox?drain=0', headers=headers, json={'source': 'pc'})
        assert inbox.status_code == 200
        sv = inbox.json()['sync_versions']
        assert {'state_seq', 'messages_epoch', 'bots_ver', 'pins_ver', 'config_ver', 'schedules_ver'}.issubset(sv.keys())

        se = app.state.brain.sync_engine
        ver_before = se.vault_ver()
        (app.state.vault.root / 'INDEX.md').write_text('# auto index\n', encoding='utf-8')
        (app.state.vault.root / 'skills').mkdir(parents=True, exist_ok=True)
        (app.state.vault.root / 'skills' / 'INDEX.md').write_text('# skills index\n', encoding='utf-8')
        assert se.vault_ver() == ver_before

        pub = client.get('/api/transport').json()
        with client.websocket_connect('/ws/client') as ws:
            hello = _make_open_envelope(pub, aes_key, {'token': token, 'source': 'phone', 'timestamp': time.time()})
            ws.send_json(hello)
            conn_pkt = CryptoBox.frame(aes_key, ws.receive_json())
            assert conn_pkt['type'] == 'connected'

            pin_resp = client.post(
                '/api/memories/pins',
                headers=headers,
                json={'action': 'add', 'text': '喜欢热牛奶', 'source': 'phone'},
            )
            assert pin_resp.status_code == 200

            ws.send_json(CryptoBox.seal(aes_key, {'type': 'ping'}))
            events: list[str] = []
            for _ in range(12):
                pkt = CryptoBox.frame(aes_key, ws.receive_json())
                if pkt.get('type') == 'pong':
                    break
                if pkt.get('type'):
                    events.append(str(pkt['type']))
            assert events.count('pins_changed') == 1
            assert 'vault_changed' not in events


def test_vision_v4_overlay_diff_zoom_and_cloud_desktop_multimodal(tmp_path: Path):
    raw1 = _make_jpeg_bytes((20, 80, 180))
    raw2 = _make_jpeg_bytes((20, 80, 180), box=(40, 40, 220, 160))

    annotated, iw, ih = draw_vision_ruler_overlay(raw1, pad_px=28)
    assert (iw, ih) == (320, 200)
    with Image.open(BytesIO(annotated)) as im:
        assert im.size == (376, 256)

    assert compute_frame_diff_pct(raw1, raw1) == 0.0
    assert compute_frame_diff_pct(raw1, raw2) > 1.0

    z_bytes, z_meta = zoom_region_from_args(raw1, {'box': [100, 100, 500, 500]})
    assert len(z_bytes) > 0 and z_meta.get('crop_px')

    app = create_app(tmp_path, scheduler=False)
    b64_raw1 = base64.b64encode(raw1).decode('ascii')

    class _FakeComputerClient:
        async def status(self, force: bool = False):
            return {
                'available': True,
                'url': 'https://example.com',
                'title': 'Demo',
                'windows': [{'id': '0x1', 'title': 'Demo Browser'}],
                'dom_elements': [],
            }

        async def call(self, method: str, path: str, body: dict | None = None):
            assert path == '/frame'
            return {'image': b64_raw1, 'width': 320, 'height': 200, 'title': 'Demo'}

    brain = app.state.brain
    orig_computer = brain.computer
    brain.computer = SimpleNamespace(client=_FakeComputerClient())
    try:
        res_status = asyncio.run(brain._tool_cloud_desktop({'op': 'status'}))
        assert res_status.get('ok') is True
        assert res_status.get('_multimodal_b64')
        assert len(res_status.get('x11_windows') or []) == 1
        assert 'ui_changed' in res_status and 'frame_diff_pct' in res_status

        res_zoom = asyncio.run(brain._tool_cloud_desktop({'op': 'zoom', 'box': [100, 100, 500, 500]}))
        assert res_zoom.get('ok') is True
        assert res_zoom.get('_multimodal_b64')
        assert res_zoom.get('zoom_meta')
        assert len(res_zoom.get('x11_windows') or []) == 1
    finally:
        brain.computer = orig_computer


def test_llm_client_http400_never_strips_images_and_reroutes_vision(tmp_path: Path):
    app = create_app(tmp_path, scheduler=False)
    app.state.store.update_config(
        {
            'api_base_url': 'https://pri.example/v1',
            'api_key': 'pk',
            'default_model': 'pri-m',
        }
    )

    def _has_image_url(msgs: list[dict]) -> bool:
        for m in msgs:
            c = m.get('content')
            if isinstance(c, list) and any(isinstance(b, dict) and b.get('type') == 'image_url' for b in c):
                return True
        return False

    silent_calls: list[dict] = []
    stream_calls: list[dict] = []

    class _FakeResp:
        def __init__(self, status_code: int, text_body: str = '', json_body: dict | None = None, sse_lines: list[str] | None = None):
            self.status_code = status_code
            self.text = text_body
            self._json_body = json_body
            self._sse_lines = sse_lines or []
            self.headers = {'content-type': 'text/event-stream' if sse_lines else 'application/json'}

        def json(self):
            return self._json_body

        async def aread(self) -> bytes:
            return self.text.encode('utf-8')

        async def aiter_lines(self):
            for line in self._sse_lines:
                yield line

    class _FakeStreamCtx:
        def __init__(self, resp: _FakeResp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeHttpClient:
        async def post(self, url: str, headers: dict | None = None, json: dict | None = None, timeout=None):
            model_nm = (json or {}).get('model')
            has_img = _has_image_url((json or {}).get('messages') or [])
            silent_calls.append({'url': url, 'model': model_nm, 'has_img': has_img})
            if model_nm != 'deepseek-v4-flash-vision-exp':
                return _FakeResp(400, text_body='{"error":"image_url unsupported"}')
            return _FakeResp(
                200,
                json_body={'choices': [{'message': {'content': '视觉路由成功', 'reasoning_content': 'seen_img'}}], 'usage': {'prompt_tokens': 8, 'completion_tokens': 4}},
            )

        def stream(self, method: str, url: str, headers: dict | None = None, json: dict | None = None):
            import json as _json
            model_nm = (json or {}).get('model')
            has_img = _has_image_url((json or {}).get('messages') or [])
            stream_calls.append({'url': url, 'model': model_nm, 'has_img': has_img})
            if model_nm != 'deepseek-v4-flash-vision-exp':
                return _FakeStreamCtx(_FakeResp(400, text_body='{"error":"image_url unsupported"}'))
            lines = [
                'data: ' + _json.dumps({'choices': [{'delta': {'reasoning_content': 'step_think ', 'content': '看图成功'}}]}, ensure_ascii=False),
                'data: [DONE]',
            ]
            return _FakeStreamCtx(_FakeResp(200, sse_lines=lines))

    llm = app.state.brain.llm
    llm._get_http_client = lambda: _FakeHttpClient()
    mm_msgs = [
        {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': '看图说话'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,AAAA'}},
            ],
        }
    ]

    with pytest.raises(ValueError):
        asyncio.run(llm.complete_silent(mm_msgs, bot_id='companion'))
    assert all(c['has_img'] is True for c in silent_calls)

    silent_calls.clear()
    app.state.store.update_config(
        {
            'api_base_url': 'http://127.0.0.1:18290/v1',
            'api_key': 'sk-secret-do-not-leak',
            'default_model': 'deepseek-v4.1-flash',
        }
    )
    res_silent = asyncio.run(llm.complete_silent(mm_msgs, bot_id='companion'))
    assert res_silent['content'] == '视觉路由成功'
    assert res_silent.get('reasoning_content') == 'seen_img'
    assert len(silent_calls) == 2
    assert silent_calls[0]['model'] == 'deepseek-v4.1-flash' and silent_calls[1]['model'] == 'deepseek-v4-flash-vision-exp'
    assert silent_calls[0]['has_img'] is True and silent_calls[1]['has_img'] is True

    res_stream = asyncio.run(llm.complete_stream(mm_msgs, bot_id='companion'))
    assert res_stream['content'] == '看图成功'
    assert res_stream.get('reasoning_content') == 'step_think'
    assert len(stream_calls) == 2
    assert stream_calls[1]['model'] == 'deepseek-v4-flash-vision-exp'
    assert stream_calls[0]['has_img'] is True and stream_calls[1]['has_img'] is True

    ws_settings = json.loads((tmp_path / 'workspace' / 'settings.json').read_text(encoding='utf-8'))
    assert ws_settings.get('api_key') == ''
    assert ws_settings.get('small_brain', {}).get('api_key') == ''
    assert ws_settings.get('big_brain', {}).get('api_key') == ''
    assert ws_settings.get('has_api_key') is True
    assert app.state.store.config.effective_api_key() == 'sk-secret-do-not-leak'


def test_trusted_phone_device_binding_blocks_rogue_device_result(tmp_path: Path):
    app = create_app(tmp_path, scheduler=False)
    app.state.store.state['trusted_phone_device_id'] = 'PJX110'
    brain = app.state.brain

    async def _run():
        fut = asyncio.get_running_loop().create_future()
        brain.phone_pending['req-1'] = fut
        brain.resolve_phone_command({'request_id': 'req-1', 'result': {'model': '2311DRK48G', 'error': 'rogue'}})
        assert 'req-1' in brain.phone_pending and not fut.done()
        brain.resolve_phone_command({'request_id': 'req-1', 'result': {'model': 'PJX110', 'ok': True}})
        assert fut.done() and fut.result()['model'] == 'PJX110'

    asyncio.run(_run())
