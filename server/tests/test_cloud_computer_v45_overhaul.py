import base64
import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from cloud_os import agent_browser, bash_session
from computer_desktop import CloudDesktop
from computer_files import UploadChunk, UploadSimple, UploadStart, WorkspaceFiles
from computer_models import HEIGHT, WIDTH, InputAction
from core.cloud_stuck_detector import CloudStuckDetector
from core.observation_masker import GUI_UI_TOOLS, extract_ephemeral_ui_from_result
from core.tool_schemas import TOOLS
from core.vision_engine import draw_vision_ruler_overlay, zoom_region_from_args
from main import create_app


def _make_solid_jpeg(w: int = WIDTH, h: int = HEIGHT, color: tuple[int, int, int] = (40, 44, 52)) -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', (w, h), color).save(buf, format='JPEG', quality=92)
    return buf.getvalue()


def test_apk_download_bypasses_gzip_and_supports_range(tmp_path: Path, monkeypatch):
    apk_bytes = b'PK\x03\x04' + (b'PROACTIVE_AGENT_PAYLOAD_' * 200)
    apk_path = tmp_path / 'proactive-agent.apk'
    apk_path.write_bytes(apk_bytes)
    monkeypatch.setenv('AGENT_APK', str(apk_path))

    app = create_app(data_dir=tmp_path / 'data', scheduler=False)
    with TestClient(app) as client:
        r = client.get('/download/proactive-agent.apk', headers={'Accept-Encoding': 'gzip'})
        assert r.status_code == 200
        assert 'content-encoding' not in {k.lower(): v for k, v in r.headers.items()}
        assert int(r.headers['content-length']) == len(apk_bytes)
        assert r.headers.get('accept-ranges') == 'bytes'
        cc = r.headers.get('cache-control', '')
        assert 'no-cache' in cc and 'no-transform' in cc
        assert r.headers.get('content-type', '').startswith('application/vnd.android.package-archive')
        assert r.content == apk_bytes

        pr = client.get(
            '/download/proactive-agent.apk',
            headers={'Accept-Encoding': 'gzip', 'Range': 'bytes=0-15'},
        )
        assert pr.status_code == 206
        assert pr.content == apk_bytes[:16]
        assert pr.headers.get('content-range') == f'bytes 0-15/{len(apk_bytes)}'


def test_workspace_files_zero_sha256_and_size_validation(tmp_path: Path):
    ws = tmp_path / 'ws'
    ws.mkdir()
    lib = WorkspaceFiles(ws, tmp_path / 'transfers')

    payload = b'zero-sha256-chunked-binary'
    st = lib.start(UploadStart(name='chunked.bin', size=len(payload), sha256=''))
    lib.chunk(st['id'], UploadChunk(offset=0, data=base64.b64encode(payload).decode()))
    fin = lib.finish(st['id'])
    assert fin['path'] == 'workspace/uploads/chunked.bin' and fin['bytes'] == len(payload)

    simple = lib.put_simple(
        UploadSimple(
            name='simple.bin',
            data=base64.b64encode(b'hello-v45').decode(),
            sha256='placeholder-not-checked',
        )
    )
    assert simple['path'] == 'workspace/uploads/simple.bin' and simple['bytes'] == 9

    bad = lib.start(UploadStart(name='bad.bin', size=20, sha256=''))
    lib.chunk(bad['id'], UploadChunk(offset=0, data=base64.b64encode(b'12345').decode()))
    with pytest.raises(ValueError):
        lib.finish(bad['id'])


@pytest.mark.parametrize('tool_name', ['cloud', 'phone', 'pc'])
def test_observation_masker_recognizes_gui_tools_and_preserves_refs(tool_name: str):
    assert tool_name in GUI_UI_TOOLS
    dom_elements = [
        {'ref': f'@b{i}', 'tag': 'button', 'label': f'Action Button {i}', 'x': 100 + i, 'y': 200}
        for i in range(1, 35)
    ]
    raw_res = {
        'ok': True,
        'op': 'click',
        'title': 'Test App',
        'ui_changed': True,
        'dom_elements': dom_elements,
        'page_text': 'Header ' + ('content ' * 160),
    }
    receipt_str, tail = extract_ephemeral_ui_from_result(tool_name, raw_res)
    receipt = json.loads(receipt_str)
    assert len(receipt_str) <= 8000
    assert 'Header content' in receipt.get('page_text', '')
    assert receipt['dom_elements_count'] == 34
    assert tail is not None and f'<ephemeral_ui_state tool="{tool_name}"' in tail
    assert '@b1' in tail and '@b34' in tail


def test_tool_schema_cloud_allows_new_v45_parameters():
    cloud_tool = next(t for t in TOOLS if t['function']['name'] == 'cloud')
    params = cloud_tool['function']['parameters']
    assert params['additionalProperties'] is False
    props = params['properties']
    expected = {'ref', 'box', 'zoom_box', 'path', 'clear', 'submit', 'dx', 'dy', 'is_input', 'click'}
    assert expected.issubset(props.keys())
    assert props['box']['type'] == 'array'
    assert props['zoom_box']['type'] == 'array'


def test_vision_engine_strict_1280x800_no_center_cyan_grid_and_zoom():
    base_color = (40, 44, 52)
    raw = _make_solid_jpeg(WIDTH, HEIGHT, base_color)
    annotated, iw, ih = draw_vision_ruler_overlay(
        raw,
        a11y_summary='@b1 [button] label=Login center=(180,180)',
        screen_w=WIDTH,
        screen_h=HEIGHT,
    )
    assert (iw, ih) == (WIDTH, HEIGHT)
    im = Image.open(io.BytesIO(annotated)).convert('RGB')
    assert im.size == (1280, 800)

    # Interior coordinates away from outer 14px ticks and @b1 badge must not have cyan gridlines/dots
    for px, py in ((320, 200), (640, 400), (960, 400), (640, 600)):
        r, g, b = im.getpixel((px, py))
        assert max(abs(r - base_color[0]), abs(g - base_color[1]), abs(b - base_color[2])) < 12

    z1, m1 = zoom_region_from_args(raw, {'box': [200, 100, 600, 500]})
    z2, m2 = zoom_region_from_args(raw, {'zoom_box': [250, 200, 750, 600]})
    assert m1['norm_box'] == [200, 100, 600, 500]
    assert m2['norm_box'] == [250, 200, 750, 600]
    zim1 = Image.open(io.BytesIO(z1))
    zim2 = Image.open(io.BytesIO(z2))
    assert zim1.size == tuple(m1['zoom_size']) and zim1.size[0] >= 512
    assert zim2.size == tuple(m2['zoom_size']) and zim2.size[0] >= 512


def test_cloud_stuck_detector_error_neighborhood_pingpong_and_auth_gate():
    # 1. Repeated identical action errors: nudge at 3 and remains advisory nudge at 4 (never hard 'stuck')
    det_err = CloudStuckDetector()
    err_res = {'ok': False, 'op': 'click', 'error': 'element not found'}
    assert det_err.observe('cloud', {'op': 'click', 'nx': 100, 'ny': 100}, err_res).stage == 'ok'
    assert det_err.observe('cloud', {'op': 'click', 'nx': 100, 'ny': 100}, err_res).stage == 'ok'
    v3 = det_err.observe('cloud', {'op': 'click', 'nx': 100, 'ny': 100}, err_res)
    assert v3.stage == 'nudge' and v3.rule == 'action_error_streak'
    v4 = det_err.observe('cloud', {'op': 'click', 'nx': 100, 'ny': 100}, err_res)
    assert v4.stage == 'nudge' and v4.rule == 'action_error_streak' and v4.stage != 'stuck'

    # 2. +/-15px neighborhood unchanged streak: nudge on 3rd and remains advisory nudge on 4th (never 'stuck')
    det_vis = CloudStuckDetector()
    unchanged = {'ok': True, 'ui_changed': False, 'frame_hash': 'samehash01'}
    assert det_vis.observe('cloud', {'op': 'click', 'nx': 500, 'ny': 500}, unchanged).stage == 'ok'
    assert det_vis.observe('cloud', {'op': 'click', 'nx': 510, 'ny': 492}, unchanged).stage == 'ok'
    nv = det_vis.observe('cloud', {'op': 'click', 'nx': 502, 'ny': 505}, unchanged)
    assert nv.stage == 'nudge' and nv.rule == 'visual_stagnation'
    sv = det_vis.observe('cloud', {'op': 'click', 'nx': 508, 'ny': 497}, unchanged)
    assert sv.stage == 'nudge' and sv.rule == 'visual_stagnation' and sv.stage != 'stuck'

    # 3. ABAB ping-pong loop: A->B->A->B->A->B nudges at 6, and stays advisory nudge on subsequent cycles
    det_pp = CloudStuckDetector()
    a_args = {'op': 'click', 'nx': 120, 'ny': 120}
    b_args = {'op': 'click', 'nx': 880, 'ny': 880}
    a_res = {'ok': True, 'ui_changed': True, 'frame_hash': 'hash_a'}
    b_res = {'ok': True, 'ui_changed': True, 'frame_hash': 'hash_b'}
    verdicts = []
    for step_idx in range(8):
        if step_idx % 2 == 0:
            verdicts.append(det_pp.observe('cloud', a_args, a_res))
        else:
            verdicts.append(det_pp.observe('cloud', b_args, b_res))
    assert verdicts[5].stage == 'nudge' and verdicts[5].rule == 'ping_pong_loop'
    assert verdicts[6].stage == 'nudge' and verdicts[6].rule == 'ping_pong_loop'
    assert all(v.stage != 'stuck' for v in verdicts)

    # 4. auth_gate QR guidance: emits advisory nudge with send_screenshot hint (never 'stuck')
    det_qr = CloudStuckDetector()
    qr_res = {'ok': True, 'auth_gate': {'kind': 'qr_login', 'bbox': [350, 200, 650, 500]}}
    assert det_qr.observe('cloud', {'op': 'click', 'nx': 500, 'ny': 350}, qr_res).stage == 'ok'
    q2 = det_qr.observe('cloud', {'op': 'click', 'nx': 505, 'ny': 355}, qr_res)
    assert q2.stage == 'nudge' and q2.rule == 'auth_gate_loop' and 'send_screenshot' in q2.message
    q3 = det_qr.observe('cloud', {'op': 'click', 'nx': 505, 'ny': 355}, qr_res)
    assert q3.stage == 'nudge' and q3.rule == 'auth_gate_loop' and q3.stage != 'stuck'


def test_agent_browser_proxy_split_args_and_tencent_direct(tmp_path: Path):
    assert '*.tencent.com' in agent_browser.PROXY_BYPASS_LIST
    assert agent_browser.is_domestic_direct('https://cloud.tencent.com/login') is True
    assert agent_browser.is_domestic_direct('https://docs.qq.com/sheet/123') is True
    assert agent_browser.is_domestic_direct('https://example.org') is False

    launched_cmds: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        launched_cmds.append(list(cmd))
        return types.SimpleNamespace(pid=12345)

    env = {'HOME': str(tmp_path), 'DISPLAY': ':91'}
    fake_os = types.SimpleNamespace(name='posix', environ=agent_browser.os.environ)
    with (
        patch.object(agent_browser, 'os', fake_os),
        patch.object(agent_browser, 'find_chromium_executable', return_value='/usr/bin/chromium'),
        patch.object(agent_browser, '_warp_proxy_url', return_value='http://127.0.0.1:40001'),
        patch.object(agent_browser, '_wait_for_browser_window', side_effect=[False, False, True, False, False, True]),
        patch.object(agent_browser, '_cdp_get_targets', return_value=[]),
        patch.object(agent_browser.time, 'sleep'),
        patch.object(
            agent_browser,
            'extract_live_cdp_dom_snapshot',
            return_value={'title': 'Mock', 'url': 'https://example.org', 'page_text': 'ok', 'elements': []},
        ),
        patch.object(agent_browser.subprocess, 'Popen', side_effect=fake_popen),
    ):
        agent_browser.launch_modern_browser('https://example.org', env, tmp_path)
        agent_browser.launch_modern_browser('https://cloud.tencent.com', env, tmp_path)

    ext_cmd, cn_cmd = launched_cmds[0], launched_cmds[1]
    assert '--proxy-server=http://127.0.0.1:40001' in ext_cmd
    assert f'--proxy-bypass-list={agent_browser.PROXY_BYPASS_LIST}' in ext_cmd
    assert not any(arg.startswith('--proxy-server=') and '--proxy-bypass-list=' in arg for arg in ext_cmd)
    assert not any(arg.startswith('--proxy-server=') for arg in cn_cmd)


@pytest.mark.asyncio
async def test_desktop_input_action_takeover_handback_and_adler32_stride(tmp_path: Path, monkeypatch):
    act = InputAction(kind='drag', x=100, y=100, dx=50, dy=-25, click=True, clear=True, submit=True, is_input=True)
    assert (act.dx, act.dy) == (50, -25)
    assert act.click and act.clear and act.submit and act.is_input

    monkeypatch.setenv('HOME', str(tmp_path))
    desktop = CloudDesktop()
    desktop.files.mkdir(parents=True, exist_ok=True)
    desktop.workspace.mkdir(parents=True, exist_ok=True)
    desktop.started = True

    st_takeover = await desktop.takeover()
    assert st_takeover['control'] == 'user' and st_takeover['user_takeover_locked'] is True
    desktop._auto_takeover_for_interactive()
    assert desktop.control == 'user' and desktop.user_takeover_locked is True

    st_handback = await desktop.handback()
    assert st_handback['control'] == 'agent' and st_handback['user_takeover_locked'] is False
    desktop._auto_takeover_for_interactive()
    assert desktop.control == 'agent'

    # 1280x800 BGRA raw buffer (stride 5120 bytes): paint a small pure-red text block change
    buf = bytearray([20, 20, 20, 255] * (WIDTH * HEIGHT))
    shot_obj = types.SimpleNamespace(size=(WIDTH, HEIGHT), bgra=bytes(buf), raw=bytes(buf))

    class FakeMss:
        def __init__(self, display=':91'):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def grab(self, _rect):
            return shot_obj

    fake_mss_mod = types.ModuleType('mss')
    fake_mss_mod.mss = FakeMss
    monkeypatch.setitem(sys.modules, 'mss', fake_mss_mod)
    monkeypatch.setattr(desktop, '_active_windows_summary', lambda: 'Desktop')
    monkeypatch.setattr(desktop, '_query_pointer_xy', lambda: None)
    monkeypatch.setattr('shutil.which', lambda _name: None)

    f1 = desktop.frame(quality=88)
    assert f1['unchanged'] is False
    assert desktop.frame(quality=88, since_hash=f1['hash'])['unchanged'] is True

    # Draw a 24x12 pure red block (BGRX -> B=0, G=0, R=255, X=255) that raw_bytes[::97] detects
    for yy in range(200, 212):
        for xx in range(300, 324):
            off = (yy * WIDTH + xx) * 4
            buf[off:off + 4] = b'\x00\x00\xff\xff'
    shot_obj = types.SimpleNamespace(size=(WIDTH, HEIGHT), bgra=bytes(buf), raw=bytes(buf))

    f2 = desktop.frame(quality=88, since_hash=f1['hash'])
    assert f2['unchanged'] is False
    assert f2['hash'] != f1['hash']


@pytest.mark.asyncio
async def test_bash_session_sentinel_cleaning_and_tmux_fallback(tmp_path: Path, monkeypatch):
    # 1. Sentinel extraction keeps legitimate lines containing `cmd_` and strips shell chrome
    cid = 'ab12cd34ef'
    raw_tty = (
        '\x1b[32mcloud-1c1g:~$\x1b[0m __ds_run ab12cd34ef\r\n'
        '__DS_BEGIN_ab12cd34ef__\r\n'
        'cmd_output_line=hello\r\n'
        'DETACHED_OK\r\n'
        '__DS_END_ab12cd34ef__\r\n'
        'cloud-1c1g:~$ '
    )
    cleaned = bash_session._clean_tty_text(raw_tty, cid)
    assert cleaned == 'cmd_output_line=hello\nDETACHED_OK'

    # Fallback cleaning without begin marker still strips internal control tokens
    inc_cleaned = bash_session._clean_tty_text('line1\n__ds_run x\nline2\ncloud-1c1g:~$ ', None)
    assert inc_cleaned == 'line1\nline2'

    # 2. When _ensure_tmux_hook returns None on POSIX, run() cleanly falls back to _run_subprocess_detached
    home = tmp_path / 'home'
    ws = home / 'workspace'
    ws.mkdir(parents=True)
    sess = bash_session.CloudBashSession(home, ws, lambda: {'HOME': str(home), 'DISPLAY': ':91'})

    monkeypatch.setattr(sess, '_ensure_tmux_hook', lambda _env: None)
    monkeypatch.setattr(bash_session.shutil, 'which', lambda name: '/usr/bin/tmux' if name == 'tmux' else None)
    monkeypatch.setattr(bash_session, 'os', types.SimpleNamespace(name='posix'))

    class FakeProc:
        pid = 4242

        def poll(self):
            return 0

    def fake_popen(args, stdout=None, **_kwargs):
        if stdout is not None:
            stdout.write(b'FALLBACK_DETACHED_OK\n')
            stdout.flush()
        return FakeProc()

    monkeypatch.setattr(bash_session.subprocess, 'Popen', fake_popen)

    res = await sess.run('echo FALLBACK_DETACHED_OK', timeout=10)
    assert res['exit_code'] == 0
    assert res['running'] is False
    assert res['timed_out'] is False
    assert 'FALLBACK_DETACHED_OK' in str(res['output'])
