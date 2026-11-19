"""
[Module Responsibility]
Conversational one-shot Cloud Computer (`proactive-desktop.service`) automated deployer over SSH
with Ed25519 key enrollment, swap provisioning, Cloud OS tarball upload, Playwright Chromium setup,
`ssh_tunnel` live hot-reload, and initial screenshot verification (`server/core/cloud_computer_deployer.py`).
"""
from __future__ import annotations
import base64, io, os, re, secrets, tarfile
from pathlib import Path
from typing import Any
import asyncssh

_CLOUD_OS_FILES = (
    'computer-openbox.xml',
    'computer_api.py',
    'computer_browser.py',
    'computer_desktop.py',
    'computer_files.py',
    'computer_models.py',
    'computer_requirements.txt',
    'computer_runtime.py',
    'computer_security.py',
    'computer_shell.py',
    'sandbox.py',
)

_HOST_RE = re.compile(r'^[A-Za-z0-9._:\-\[\]]+$')
_USER_RE = re.compile(r'^[A-Za-z0-9._\-]+$')


def load_or_create_ed25519_key(data_dir: Path) -> tuple[str, str]:
    """Return `(private_pem, public_openssh)` for `cloud_computer_ed25519` stored inside `data/`."""
    data_dir.mkdir(parents=True, exist_ok=True)
    key_path = data_dir / 'cloud_computer_ed25519'
    pub_path = data_dir / 'cloud_computer_ed25519.pub'
    if key_path.exists():
        priv_pem = key_path.read_text('utf-8').strip()
        if pub_path.exists():
            pub_str = pub_path.read_text('utf-8').strip()
        else:
            key_obj = asyncssh.import_private_key(priv_pem)
            pub_str = key_obj.export_public_key('openssh').decode('utf-8').strip()
            pub_path.write_text(pub_str + '\n', encoding='utf-8')
        return priv_pem, pub_str

    key_obj = asyncssh.generate_private_key('ssh-ed25519')
    priv_pem = key_obj.export_private_key('openssh').decode('utf-8').strip()
    pub_str = key_obj.export_public_key('openssh').decode('utf-8').strip()
    key_path.write_text(priv_pem + '\n', encoding='utf-8')
    pub_path.write_text(pub_str + '\n', encoding='utf-8')
    if os.name == 'posix':
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
    return priv_pem, pub_str


def build_cloud_os_tarball(server_dir: Path | None = None) -> bytes:
    """Package Cloud OS scripts, `core/search_engine.py`, and `cloud_os/` assets into `.tar.gz`."""
    root = server_dir or Path(__file__).resolve().parents[1]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for fname in _CLOUD_OS_FILES:
            fp = root / fname
            if fp.is_file():
                tar.add(fp, arcname=fname)
        for core_rel in ('core/__init__.py', 'core/search_engine.py'):
            cfp = root / core_rel
            if cfp.is_file():
                tar.add(cfp, arcname=core_rel)
        cloud_dir = root / 'cloud_os'
        if cloud_dir.is_dir():
            for child in sorted(cloud_dir.rglob('*')):
                if not child.is_file():
                    continue
                if '__pycache__' in child.parts or child.suffix == '.pyc':
                    continue
                rel = child.relative_to(root).as_posix()
                tar.add(child, arcname=rel)
    return buf.getvalue()


async def _emit_stage(publish: Any, stage: str, detail: str) -> None:
    if callable(publish):
        try:
            await publish({'type': 'cloud_desktop_deploy', 'stage': stage, 'detail': detail})
        except Exception:
            pass


async def _run_checked(conn: asyncssh.SSHClientConnection, cmd: str, check: bool = True, timeout: int = 240) -> tuple[int, str]:
    res = await conn.run(cmd, check=False, timeout=timeout)
    out = ((res.stdout or '') + '\n' + (res.stderr or '')).strip()
    rc = int(res.exit_status or 0)
    if check and rc != 0:
        raise RuntimeError(f'远程命令失败 (exit={rc}): {out[-600:]}')
    return rc, out


def _systemd_unit_text() -> str:
    return (
        '[Unit]\n'
        'Description=Proactive Agent Isolated Cloud Desktop Service\n'
        'After=network.target\n\n'
        '[Service]\n'
        'Type=simple\n'
        'User=proactive-desktop\n'
        'Group=proactive-desktop\n'
        'RuntimeDirectory=proactive-desktop\n'
        'RuntimeDirectoryMode=0750\n'
        'WorkingDirectory=/opt/proactive-agent/cloud-computer\n'
        'Environment=HOME=/opt/proactive-agent/cloud-computer/desktop-home\n'
        'Environment=USER=proactive-desktop\n'
        'Environment=XDG_RUNTIME_DIR=/run/proactive-desktop\n'
        'Environment=LANG=C.UTF-8\n'
        'Environment=LC_ALL=C.UTF-8\n'
        'Environment=PYTHONPATH=/opt/proactive-agent/cloud-computer\n'
        'Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/proactive-agent/browser-runtime\n'
        'Environment=COMPUTER_XAUTH_EXECUTABLE=/usr/bin/xauth\n'
        'Environment=COMPUTER_HOST=127.0.0.1\n'
        'Environment=COMPUTER_PORT=18193\n'
        'Environment=COMPUTER_TOKEN_FILE=/opt/proactive-agent/data/computer-token\n'
        'MemoryMax=2560M\n'
        'CPUQuota=200%\n'
        'TasksMax=2048\n'
        'ExecStart=/opt/proactive-agent/desktop-venv/bin/python /opt/proactive-agent/cloud-computer/computer_runtime.py\n'
        'Restart=always\n'
        'RestartSec=3\n\n'
        '[Install]\n'
        'WantedBy=multi-user.target\n'
    )


def _save_first_screenshot(workspace_dir: Path | None, frame_data: dict[str, Any]) -> str:
    if workspace_dir is None or not isinstance(frame_data, dict):
        return ''
    b64 = str(frame_data.get('data') or frame_data.get('image') or '').strip()
    if not b64:
        return ''
    if ',' in b64 and b64.startswith('data:'):
        b64 = b64.split(',', 1)[1]
    try:
        raw = base64.b64decode(b64)
        out_dir = Path(workspace_dir) / 'uploads'
        out_dir.mkdir(parents=True, exist_ok=True)
        fmt = str(frame_data.get('format') or 'jpeg').lower()
        ext = 'png' if fmt == 'png' else 'jpg'
        out_path = out_dir / f'cloud_computer_ready.{ext}'
        out_path.write_bytes(raw)
        return f'uploads/{out_path.name}'
    except Exception:
        return ''


async def deploy_cloud_computer_over_ssh(
    store: Any,
    computer_coordinator: Any,
    publish: Any,
    args: dict[str, Any],
    workspace_dir: Path | None = None,
) -> dict[str, Any]:
    """Connect to a user's Linux VPS over SSH, deploy Cloud OS, and hot-reload an `ssh_tunnel` client."""
    action = str(args.get('action') or args.get('op') or 'deploy').strip().lower()
    cfg = store.config

    if action == 'status':
        st = await computer_coordinator.client.status(force=True) if (computer_coordinator and getattr(computer_coordinator, 'client', None)) else {}
        return {
            'ok': True,
            'configured': bool(cfg.cloud_computer_host or cfg.cloud_computer_url),
            'mode': cfg.cloud_computer_mode,
            'host': cfg.cloud_computer_host,
            'port': cfg.cloud_computer_port,
            'user': cfg.cloud_computer_user,
            'status': cfg.cloud_computer_status,
            'desktop': st,
        }

    if action in ('disconnect', 'remove', 'clear'):
        store.update_config(
            {
                'cloud_computer_mode': '',
                'cloud_computer_host': '',
                'cloud_computer_port': 22,
                'cloud_computer_user': '',
                'cloud_computer_password': '',
                'cloud_computer_ssh_key': '',
                'cloud_computer_url': '',
                'cloud_computer_token': '',
                'cloud_computer_status': 'disconnected',
            },
            source_tag='tool:deploy_cloud_computer',
        )
        if computer_coordinator and getattr(computer_coordinator, 'client', None):
            await computer_coordinator.client.reload_config()
        await _emit_stage(publish, 'disconnected', '已断开共享云电脑隧道配置')
        return {'ok': True, 'disconnected': True}

    host = str(args.get('host') or args.get('ip') or '').strip()
    username = str(args.get('username') or args.get('user') or 'root').strip()
    password = str(args.get('password') or '').strip()
    user_ssh_key = str(args.get('ssh_key') or args.get('private_key') or '').strip()
    force_reinstall = bool(args.get('force_reinstall', False))
    try:
        port = int(args.get('port') or 22)
    except Exception:
        return {'ok': False, 'error': 'SSH 端口必须是 1~65535 的整数'}

    if not host or not _HOST_RE.match(host):
        return {'ok': False, 'error': '请提供有效的 VPS IP 或域名 (host)'}
    if not (1 <= port <= 65535):
        return {'ok': False, 'error': 'SSH 端口必须在 1~65535 之间'}
    if not username or not _USER_RE.match(username):
        return {'ok': False, 'error': '请提供有效的 SSH 用户名 (username)'}
    if not password and not user_ssh_key:
        return {'ok': False, 'error': '请提供 SSH 密码或私钥，才能自动连上 VPS 部署云电脑'}

    data_dir = Path(getattr(store, 'data_dir', None) or store.config_path.parent)
    priv_pem, pub_str = load_or_create_ed25519_key(data_dir)
    client_keys: list[Any] = []
    if user_ssh_key:
        try:
            client_keys.append(asyncssh.import_private_key(user_ssh_key))
        except Exception as exc:
            return {'ok': False, 'error': f'提供的 SSH 私钥格式无法解析: {exc}'}
    try:
        client_keys.append(asyncssh.import_private_key(priv_pem))
    except Exception:
        pass

    sudo = '' if username == 'root' else 'sudo '
    tar_bytes = build_cloud_os_tarball()
    remote_token = secrets.token_hex(24)

    await _emit_stage(publish, 'connecting', f'正在通过 SSH 连接 {username}@{host}:{port}…')
    try:
        conn_ctx = await asyncssh.connect(
            host,
            port=port,
            username=username,
            password=password or None,
            client_keys=client_keys or None,
            known_hosts=None,
            connect_timeout=20,
        )
    except Exception as exc:
        await _emit_stage(publish, 'failed', f'SSH 连接失败: {exc}')
        return {'ok': False, 'error': f'SSH 连接 {username}@{host}:{port} 失败：{exc}'}

    key_installed = False
    async with conn_ctx as conn:
        try:
            # 1. Install generated Ed25519 public key so future tunnel reconnects stay passwordless
            await _emit_stage(publish, 'ssh_key', '写入持久化 Ed25519 密钥与系统探测…')
            pub_b64 = base64.b64encode(pub_str.encode('utf-8')).decode('ascii')
            rc_key, _ = await _run_checked(
                conn,
                f'mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && '
                f'PUB=$(echo {pub_b64} | base64 -d) && '
                f'(grep -qxF "$PUB" ~/.ssh/authorized_keys || echo "$PUB" >> ~/.ssh/authorized_keys)',
                check=False,
                timeout=30,
            )
            key_installed = rc_key == 0

            # 2. Ensure adequate swap on small-memory VPS (<1900 MB RAM)
            await _emit_stage(publish, 'swap', '检查内存与 Swap 空间…')
            await _run_checked(
                conn,
                f'{sudo}sh -c "'
                'MEM=$(free -m | awk \'/^Mem:/{print $2}\'); '
                'SWP=$(swapon --show --noheadings | wc -l); '
                'if [ "${MEM:-0}" -lt 1900 ] && [ "${SWP:-0}" -eq 0 ] && [ ! -f /swapfile ]; then '
                '(fallocate -l 1536M /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=1536 status=none) && '
                'chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile && '
                '(grep -q \'/swapfile\' /etc/fstab || echo \'/swapfile none swap sw 0 0\' >> /etc/fstab); '
                'fi"',
                check=False,
                timeout=90,
            )

            # 3. Install Xvfb, Openbox, fonts, tmux, xauth/xclip/x11-utils and Python venv prerequisites
            await _emit_stage(publish, 'packages', '安装 Xvfb / Openbox / X11 工具链与中文字体…')
            force_flag = '1' if force_reinstall else '0'
            await _run_checked(
                conn,
                f'{sudo}sh -c "'
                f'if [ "{force_flag}" = "1" ] || ! command -v Xvfb >/dev/null 2>&1 || ! command -v openbox >/dev/null 2>&1 || ! command -v tmux >/dev/null 2>&1 || ! command -v xclip >/dev/null 2>&1 || ! command -v xprop >/dev/null 2>&1 || ! command -v xauth >/dev/null 2>&1; then '
                'export DEBIAN_FRONTEND=noninteractive && '
                'apt-get update -y -qq && '
                'apt-get install -y -qq python3 python3-venv python3-pip xvfb openbox xdotool scrot imagemagick dbus-x11 xauth xclip x11-utils fonts-noto-cjk tmux curl ca-certificates; '
                'fi"',
                check=True,
                timeout=300,
            )

            # 4. Upload Cloud OS bundle, ensure `proactive-desktop` user & unpack into /opt/proactive-agent/cloud-computer
            await _emit_stage(publish, 'upload', '上传云桌面核心包与配置受控账号 proactive-desktop…')
            async with conn.start_sftp_client() as sftp:
                async with sftp.open('/tmp/proactive-cloud-os.tar.gz', 'wb') as rf:
                    await rf.write(tar_bytes)
            await _run_checked(
                conn,
                f'{sudo}sh -c "'
                'mkdir -p /opt/proactive-agent/cloud-computer/desktop-home/workspace /opt/proactive-agent/cloud-computer/desktop-home/Downloads /opt/proactive-agent/data /opt/proactive-agent/browser-runtime /run/proactive-desktop && '
                '(id -u proactive-desktop >/dev/null 2>&1 || useradd -r -M -d /opt/proactive-agent/cloud-computer/desktop-home -s /bin/bash proactive-desktop) && '
                'tar -xzf /tmp/proactive-cloud-os.tar.gz -C /opt/proactive-agent/cloud-computer && '
                'rm -f /tmp/proactive-cloud-os.tar.gz && '
                'chmod 755 /opt/proactive-agent /opt/proactive-agent/data && '
                'chmod -R a+rX /opt/proactive-agent/cloud-computer && '
                'chown -R proactive-desktop:proactive-desktop /opt/proactive-agent/cloud-computer/desktop-home /run/proactive-desktop && '
                'chmod 0750 /opt/proactive-agent/cloud-computer/desktop-home /run/proactive-desktop"',
                check=True,
                timeout=60,
            )

            # 5. Create desktop-venv & install dependencies + Playwright Chromium
            await _emit_stage(publish, 'venv', '配置桌面运行环境与 Playwright Chromium…')
            await _run_checked(
                conn,
                f'{sudo}sh -c "'
                'if [ ! -x /opt/proactive-agent/desktop-venv/bin/python ]; then '
                'python3 -m venv /opt/proactive-agent/desktop-venv; '
                'fi && '
                '/opt/proactive-agent/desktop-venv/bin/pip install -q --upgrade pip && '
                '/opt/proactive-agent/desktop-venv/bin/pip install -q -r /opt/proactive-agent/cloud-computer/computer_requirements.txt && '
                'export PLAYWRIGHT_BROWSERS_PATH=/opt/proactive-agent/browser-runtime && '
                '(/opt/proactive-agent/desktop-venv/bin/python -m playwright install chromium --with-deps || '
                '/opt/proactive-agent/desktop-venv/bin/python -m playwright install chromium || true) && '
                'chmod -R a+rX /opt/proactive-agent/desktop-venv /opt/proactive-agent/browser-runtime"',
                check=True,
                timeout=420,
            )

            # 6. Write token (owned by proactive-desktop), systemd unit, and start service
            await _emit_stage(publish, 'service', '启动 127.0.0.1:18193 隔离云桌面 systemd 服务…')
            tok_b64 = base64.b64encode(remote_token.encode('utf-8')).decode('ascii')
            unit_b64 = base64.b64encode(_systemd_unit_text().encode('utf-8')).decode('ascii')
            await _run_checked(
                conn,
                f'{sudo}sh -c "'
                f'echo {tok_b64} | base64 -d > /opt/proactive-agent/data/computer-token && '
                'chown proactive-desktop:proactive-desktop /opt/proactive-agent/data/computer-token && '
                'chmod 600 /opt/proactive-agent/data/computer-token && '
                f'echo {unit_b64} | base64 -d > /etc/systemd/system/proactive-desktop.service && '
                'systemctl daemon-reload && '
                'systemctl enable proactive-desktop.service && '
                'systemctl restart proactive-desktop.service"',
                check=True,
                timeout=90,
            )

            # 7. Wait briefly for remote uvicorn to accept local authenticated /status requests
            await _run_checked(
                conn,
                f'for i in 1 2 3 4 5 6 7 8 9 10; do '
                f'curl -sf -H "Authorization: Bearer {remote_token}" http://127.0.0.1:18193/status >/dev/null 2>&1 && exit 0; '
                f'sleep 1; done; exit 1',
                check=True,
                timeout=30,
            )
        except Exception as exc:
            await _emit_stage(publish, 'failed', f'部署过程出错: {exc}')
            return {'ok': False, 'error': str(exc)}

    await _emit_stage(publish, 'tunnel', '建立加密 SSH 本地端口转发并热重载客户端…')
    saved_key = priv_pem if key_installed else (user_ssh_key or priv_pem)
    store.update_config(
        {
            'cloud_computer_mode': 'ssh_tunnel',
            'cloud_computer_host': host,
            'cloud_computer_port': port,
            'cloud_computer_user': username,
            'cloud_computer_password': '' if key_installed else password,
            'cloud_computer_ssh_key': saved_key,
            'cloud_computer_token': remote_token,
            'cloud_computer_status': 'ready',
            'onboarding_completed': True,
        },
        source_tag='tool:deploy_cloud_computer',
    )

    desktop_status: dict[str, Any] = {}
    screenshot_path = ''
    if computer_coordinator and getattr(computer_coordinator, 'client', None):
        desktop_status = await computer_coordinator.client.reload_config()
        try:
            frame_res = await computer_coordinator.client.call('GET', '/frame', query={'quality': 72})
            screenshot_path = _save_first_screenshot(workspace_dir, frame_res)
        except Exception:
            screenshot_path = ''

    await _emit_stage(publish, 'ready', f'云电脑已在 {host} 上线并通过加密 SSH 隧道挂载！')
    return {
        'ok': True,
        'mode': 'ssh_tunnel',
        'host': host,
        'port': port,
        'user': username,
        'key_auth_installed': key_installed,
        'status': 'ready',
        'screenshot_path': screenshot_path,
        'desktop_status': desktop_status,
    }
