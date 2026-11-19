"""Real Node DSH Sidecar + Python FastAPI Gateway loopback integration tests."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from main import create_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


def _find_node() -> str | None:
    return shutil.which('node')


@pytest.mark.skipif(_find_node() is None, reason='Node.js runtime not available')
def test_dsh_sidecar_full_react_loop_and_abort(tmp_path: Path):
    """Spawn real Node DSH Sidecar + FastAPI uvicorn + Mock OpenAI SSE server and verify end-to-end flow."""
    node_bin = _find_node()
    assert node_bin

    # 1. Mock OpenAI /chat/completions SSE server using stdlib http.server in background thread
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    llm_state = {'mode': 'normal', 'calls': []}

    class MockOpenAiHandler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, format, *args):
            return

        def do_POST(self):
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length).decode('utf-8', 'replace')
            body = json.loads(raw or '{}')
            llm_state['calls'].append(body)
            msgs = body.get('messages') or []
            has_tool_result = any(isinstance(m, dict) and m.get('role') == 'tool' for m in msgs)

            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()

            def write_chunk(raw_bytes: bytes):
                if not raw_bytes:
                    return
                self.wfile.write(f'{len(raw_bytes):x}\r\n'.encode('ascii') + raw_bytes + b'\r\n')
                self.wfile.flush()

            def send_sse(obj: dict):
                write_chunk(f'data: {json.dumps(obj, ensure_ascii=False)}\n\n'.encode('utf-8'))

            try:
                if llm_state['mode'] == 'normal':
                    if not has_tool_result:
                        send_sse({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'reasoning_content': '先调用 memory 工具固定规则。'}}]})
                        send_sse({'choices': [{'index': 0, 'delta': {'content': '先帮你在记忆库记录一下。'}}]})
                        send_sse({
                            'choices': [{
                                'index': 0,
                                'delta': {
                                    'tool_calls': [{
                                        'index': 0,
                                        'id': 'call_pin_1',
                                        'type': 'function',
                                        'function': {
                                            'name': 'memory',
                                            'arguments': json.dumps({'op': 'pin', 'text': 'DSH 引擎集成测试通过'}, ensure_ascii=False),
                                        },
                                    }]
                                },
                            }]
                        })
                        send_sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 120, 'completion_tokens': 18}})
                    else:
                        send_sse({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'reasoning_content': '置顶完成。'}}]})
                        send_sse({'choices': [{'index': 0, 'delta': {'content': '已经通过 DSH 引擎记下置顶规则啦。'}}]})
                        send_sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 160, 'completion_tokens': 12}})
                else:
                    # slow_abort mode: emit intermediate narration + tool call on step 1, then stall on step 2 until cancelled
                    if not has_tool_result:
                        send_sse({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'reasoning_content': '检查置顶列表'}}]})
                        send_sse({'choices': [{'index': 0, 'delta': {'content': '正在检查最后一步配置。'}}]})
                        send_sse({
                            'choices': [{
                                'index': 0,
                                'delta': {
                                    'tool_calls': [{
                                        'index': 0,
                                        'id': 'call_pin_2',
                                        'type': 'function',
                                        'function': {
                                            'name': 'memory',
                                            'arguments': json.dumps({'op': 'list_pins'}),
                                        },
                                    }]
                                },
                            }]
                        })
                        send_sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})
                    else:
                        send_sse({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'reasoning_content': '准备整理收尾'}}]})
                        for _ in range(40):
                            time.sleep(0.15)
                            write_chunk(b': keepalive\n\n')
                        send_sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
                write_chunk(b'data: [DONE]\n\n')
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    mock_llm_port = _free_port()
    mock_httpd = ThreadingHTTPServer(('127.0.0.1', mock_llm_port), MockOpenAiHandler)
    mock_thread = threading.Thread(target=mock_httpd.serve_forever, daemon=True)
    mock_thread.start()

    # 2. Spawn Node DSH Sidecar on ephemeral port
    dsh_port = _free_port()
    dsh_data_dir = tmp_path / 'dsh_sessions'
    py_port = _free_port()
    dsh_secret = 'test-dsh-secret-xyz'

    node_env = {
        **os.environ,
        'DSH_BRAIN_PORT': str(dsh_port),
        'DSH_BRAIN_DATA_DIR': str(dsh_data_dir),
        'DSH_BRAIN_SECRET': dsh_secret,
        'PY_GATEWAY_PORT': str(py_port),
    }
    node_proc = subprocess.Popen(
        [node_bin, str(SERVER_DIR / 'dsh_brain' / 'src' / 'server.mjs')],
        cwd=str(SERVER_DIR / 'dsh_brain'),
        env=node_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    py_data_dir = tmp_path / 'py_data'
    py_data_dir.mkdir(parents=True, exist_ok=True)
    token_val = 'test-device-token-999'

    old_env = {k: os.environ.get(k) for k in ('BRAIN_ENGINE', 'DSH_BRAIN_URL', 'DSH_BRAIN_SECRET', 'AGENT_TOKEN', 'PORT')}
    os.environ['BRAIN_ENGINE'] = 'dsh'
    os.environ['DSH_BRAIN_URL'] = f'http://127.0.0.1:{dsh_port}'
    os.environ['DSH_BRAIN_SECRET'] = dsh_secret
    os.environ['AGENT_TOKEN'] = token_val
    os.environ['PORT'] = str(py_port)

    uvicorn_server: uvicorn.Server | None = None
    uvicorn_thread: threading.Thread | None = None

    try:
        # Wait for Node DSH Sidecar /health
        deadline = time.monotonic() + 12.0
        node_ready = False
        while time.monotonic() < deadline:
            if node_proc.poll() is not None:
                out, err = node_proc.communicate(timeout=2)
                raise AssertionError(f'Node DSH sidecar exited early: code={node_proc.returncode}\nSTDOUT={out}\nSTDERR={err}')
            try:
                r = httpx.get(f'http://127.0.0.1:{dsh_port}/health', headers={'X-DSH-Secret': dsh_secret}, timeout=1.0)
                if r.status_code == 200 and r.json().get('ok'):
                    node_ready = True
                    break
            except Exception:
                time.sleep(0.15)
        assert node_ready, 'Node DSH sidecar did not become healthy in time'

        # 3. Boot real FastAPI uvicorn server
        app = create_app(data_dir=py_data_dir, scheduler=False)
        app.state.brain.py_gateway_port = py_port
        app.state.brain._vdisplay_currently_open = True
        app.state.store.update_config({
            'api_base_url': f'http://127.0.0.1:{mock_llm_port}/v1',
            'api_key': 'sk-test-dsh-mock',
            'default_model': 'deepseek-v4.1-flash',
        })
        app.state.store.save()

        published_events: list[dict] = []
        orig_publish = app.state.brain.publish

        async def spy_publish(ev):
            if isinstance(ev, dict):
                published_events.append(dict(ev))
            await orig_publish(ev)

        app.state.brain.publish = spy_publish

        uv_cfg = uvicorn.Config(app, host='127.0.0.1', port=py_port, log_level='warning')
        uvicorn_server = uvicorn.Server(uv_cfg)
        uvicorn_thread = threading.Thread(target=uvicorn_server.run, daemon=True)
        uvicorn_thread.start()

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                hr = httpx.get(f'http://127.0.0.1:{py_port}/health', timeout=1.0)
                if hr.status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)

        headers = {'Authorization': f'Bearer {token_val}'}
        with httpx.Client(base_url=f'http://127.0.0.1:{py_port}', headers=headers, timeout=30.0) as client:
            # A. /health check reports engine='dsh' and sidecar ok
            h_res = client.get('/health')
            assert h_res.status_code == 200
            h_json = h_res.json()
            assert h_json['brain_engine'] == 'dsh'
            assert h_json['dsh_sidecar']['ok'] is True
            assert str(h_json['dsh_sidecar'].get('boot_id') or '').strip() != ''

            # B. Normal /api/chat through Node DSH loop + loopback /internal/dsh/tool
            chat_res = client.post('/api/chat', json={
                'text': '帮我固定一条测试规则',
                'bot_id': 'companion',
                'source': 'pc',
            })
            assert chat_res.status_code == 200, chat_res.text
            c_json = chat_res.json()
            assert c_json['engine'] == 'dsh'
            assert '已经通过 DSH 引擎记下置顶规则啦' in c_json['text']
            assert any(tr.get('tool') == 'memory' and tr.get('status') == 'done' for tr in c_json.get('trace') or [])
            tl = c_json.get('timeline') or []
            assert [item.get('type') for item in tl] == ['text', 'tool', 'text']
            assert '先帮你在记忆库记录一下' in str(tl[0].get('text') or '')
            assert tl[1].get('tool') == 'memory' and tl[1].get('status') == 'done'
            assert '已经通过 DSH 引擎记下置顶规则啦' in str(tl[2].get('text') or '')
            assert int(c_json.get('order_seq') or 0) >= 2
            assert 'pc__companion' in app.state.brain.dsh_adapter._warm_sessions
            dsh_live_health = httpx.get(
                f'http://127.0.0.1:{dsh_port}/health',
                headers={'X-DSH-Secret': dsh_secret},
                timeout=2.0,
            ).json()
            assert 'pc__companion' in (dsh_live_health.get('warm_sessions') or [])

            # Verify memory tool actually persisted the pin in Python store
            pins = app.state.brain.memory_engine.list_pins()
            assert any('DSH 引擎集成测试通过' in str(p.get('text') or '') for p in pins)

            # Verify stream: None includes final_message handoff
            none_streams = [e for e in published_events if e.get('type') == 'stream' and e.get('stream') is None]
            assert any(isinstance(e.get('final_message'), dict) and e['final_message'].get('id') == c_json['id'] for e in none_streams)

            # Verify /api/sync?since_seq=<oseq_before> works cleanly with monotonic order_seq
            prev_oseq = int(c_json['order_seq']) - 1
            sync_res = client.get(f'/api/sync?since_seq={prev_oseq}&source=pc')
            assert sync_res.status_code == 200
            s_json = sync_res.json()
            assert any(m.get('id') == c_json['id'] for m in s_json.get('messages') or [])

            # C. Slow turn + /api/stop promotes last_step_text cleanly and NEVER sends phone_workspace_command release!
            published_events.clear()
            llm_state['mode'] = 'slow_abort'
            enq = client.post('/api/chat', json={
                'text': '开始长时间检查',
                'bot_id': 'companion',
                'source': 'phone',
                'async_ack': True,
            })
            assert enq.status_code == 200

            # Wait until tool call step 1 has executed and stream has step_text recorded
            wait_stop_dl = time.monotonic() + 8.0
            while time.monotonic() < wait_stop_dl:
                st = client.get('/api/status?source=phone').json()
                cur_st = st.get('stream') or {}
                if cur_st.get('step_text') or (st.get('execution') or {}).get('trace'):
                    break
                time.sleep(0.08)

            stop_res = client.post('/api/stop', json={'bot_id': 'companion', 'source': 'phone'})
            assert stop_res.status_code == 200
            stop_json = stop_res.json()
            committed = stop_json.get('committed_message') or {}
            assert '正在检查最后一步配置' in str(committed.get('text') or '')
            assert '（已停止）' in str(committed.get('text') or '')

            # Assert stopping chat NEVER auto-closes VirtualDisplay via phone_workspace_command release!
            assert not any(
                e.get('type') == 'phone_workspace_command' and e.get('op') == 'release'
                for e in published_events
            )
            assert app.state.brain._vdisplay_currently_open is True
    finally:
        if uvicorn_server is not None:
            uvicorn_server.should_exit = True
        if uvicorn_thread is not None:
            uvicorn_thread.join(timeout=5)
        node_proc.terminate()
        try:
            node_proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            node_proc.kill()
        mock_httpd.shutdown()
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_reply_send_image_send_file_and_workspace_media_auth(tmp_path: Path):
    """Verify reply(send_image / send_file), placeholder image filtering, and authenticated PDF serving."""
    from fastapi.testclient import TestClient
    from PIL import Image

    data_dir = tmp_path / 'data'
    ws = data_dir / 'workspace'
    (ws / 'uploads').mkdir(parents=True, exist_ok=True)
    (ws / 'cet6').mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (48, 48), 'blue').save(ws / 'uploads' / 'chart.png')
    (ws / 'cet6' / 'paper.pdf').write_bytes(b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF')

    old_tok = os.environ.get('AGENT_TOKEN')
    os.environ['AGENT_TOKEN'] = 'media-test-token'
    try:
        app = create_app(data_dir=data_dir, scheduler=False)
        brain = app.state.brain
        with TestClient(app) as client:
            brain.dsh_adapter._get_or_init_turn_acc('turn-media-1')
            brain.dsh_adapter._get_or_init_turn_acc('turn-media-2')

            async def _run_tools():
                r_img = await brain.dsh_adapter.execute_tool_from_sidecar(
                    name='reply',
                    args={'op': 'send_image', 'path': 'uploads/chart.png'},
                    bot_id='companion',
                    source='phone',
                    turn_id='turn-media-1',
                )
                r_img_pc = await brain.dsh_adapter.execute_tool_from_sidecar(
                    name='reply',
                    args={'op': 'send_image', 'path': 'uploads/chart.png'},
                    bot_id='companion',
                    source='pc',
                    turn_id='turn-media-2',
                )
                r_pdf = await brain.dsh_adapter.execute_tool_from_sidecar(
                    name='reply',
                    args={'op': 'send_file', 'path': 'cet6/paper.pdf'},
                    bot_id='companion',
                    source='phone',
                    turn_id='turn-media-1',
                )
                return r_img, r_img_pc, r_pdf

            r_img, r_img_pc, r_pdf = asyncio.run(_run_tools())
            assert r_img['ok'] is True and r_img['result']['ok'] is True
            assert str(r_img['result']['chat_image_url']).startswith('/workspace-media/uploads/')
            assert r_img_pc['ok'] is True and r_img_pc['result']['ok'] is True
            assert r_pdf['ok'] is True and r_pdf['result']['ok'] is True
            assert r_pdf['result']['chat_file']['path'] == 'cet6/paper.pdf'
            assert r_pdf['result']['chat_file']['ext'] == 'PDF'

            acc = brain.dsh_adapter._turn_acc.get('turn-media-1') or {}
            assert len(acc.get('stickers') or []) == 1
            assert len(acc.get('files') or []) == 1

            # Emit with pending vars applied should persist sticker + attachments
            async def _emit_with_acc():
                brain.pending_stickers_var.set(tuple(acc['stickers']))
                brain.pending_files_var.set(tuple(acc['files']))
                return await brain.emit('这是图片和 PDF 附件', 'chat')

            msg = asyncio.run(_emit_with_acc())
            assert str(msg.get('sticker') or '').startswith('/workspace-media/uploads/')
            assert isinstance(msg.get('attachments'), list) and msg['attachments'][0]['path'] == 'cet6/paper.pdf'

            # Illustrative placeholder ![图片](路径) must NOT become /workspace-media/路径
            cleaned, _, _, ph_stk = brain._extract_all_tags('示例写法：`![图片](/workspace-media/路径)` 或 ![图片](路径)')
            assert ph_stk == ''

            # Unauthed PDF access must stay 401; authed PDF access returns 200 application/pdf
            assert client.get('/workspace-media/cet6/paper.pdf').status_code == 401
            authed = client.get('/workspace-media/cet6/paper.pdf', headers={'Authorization': 'Bearer media-test-token'})
            assert authed.status_code == 200
            assert 'application/pdf' in authed.headers.get('content-type', '')
            assert authed.content.startswith(b'%PDF-1.4')
            # Public upload image remains accessible
            pub_img = client.get(r_img['result']['chat_image_url'])
            assert pub_img.status_code == 200
    finally:
        if old_tok is None:
            os.environ.pop('AGENT_TOKEN', None)
        else:
            os.environ['AGENT_TOKEN'] = old_tok
