"""Narrow host-only deployment. No Docker, network, proxy or firewall operations."""
from pathlib import Path
import argparse,json,os,subprocess,tarfile,tempfile
ROOT=Path(__file__).resolve().parents[1]
def _load_deploy_cfg() -> dict:
    cfg_path = ROOT / 'private' / 'deploy.json'
    if cfg_path.is_file():
        try:
            return json.loads(cfg_path.read_text('utf-8'))
        except Exception:
            return {}
    return {}

_DEPLOY_CFG = _load_deploy_cfg()
SSH_HOST = os.environ.get('PULSE_SSH_HOST') or str(_DEPLOY_CFG.get('ssh_host') or '').strip() or 'your-vps-host'
PUBLIC_BASE_URL = (os.environ.get('PULSE_PUBLIC_BASE_URL') or str(_DEPLOY_CFG.get('public_base_url') or '').strip() or 'http://127.0.0.1:18190').rstrip('/')
SSH_CONFIG = os.environ.get('VPS_SSH_CONFIG', '')
JUMP_HOST = os.environ.get('VPS_JUMP_HOST', '')
_BASE_SSH_OPTS = (['-F', SSH_CONFIG] if SSH_CONFIG else []) + (['-J', JUMP_HOST] if JUMP_HOST else []) + ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=30', '-o', 'ConnectionAttempts=2']
SSH = ['ssh', *_BASE_SSH_OPTS, SSH_HOST]
SCP = ['scp', *_BASE_SSH_OPTS]

REMOTE=r'''set -eu
BASE=/opt/proactive-agent
install -d -m 755 "$BASE" "$BASE/server" "$BASE/server/dsh_brain" "$BASE/server/dsh_brain/src" "$BASE/dist"
install -d -m 700 "$BASE/bootstrap"
tar -xzf "$BASE/.deploy/incoming.tar.gz" -C "$BASE"
chmod -R a+rX "$BASE/server"
if [ -f "$BASE/dist/proactive-agent.apk" ]; then chmod 644 "$BASE/dist/proactive-agent.apk"; fi
if ! id proactive-agent >/dev/null 2>&1; then useradd --system --home-dir "$BASE/data" --shell /usr/sbin/nologin proactive-agent; fi
install -d -m 700 -o proactive-agent -g proactive-agent "$BASE/data" "$BASE/data/dsh_sessions"
if [ -d /opt/open-deepseek-bot/cloud-brain/node_modules/@deepseek-ai ]; then
  ln -sfn /opt/open-deepseek-bot/cloud-brain/node_modules "$BASE/server/dsh_brain/node_modules"
elif [ ! -d "$BASE/server/dsh_brain/node_modules/@deepseek-ai" ]; then
  echo 'Missing @deepseek-ai/dsh-* node_modules' >&2
  exit 1
fi
for secret in transport-key.pem device-token; do
  if [ ! -f "$BASE/data/$secret" ]; then install -m 600 -o proactive-agent -g proactive-agent "$BASE/bootstrap/$secret" "$BASE/data/$secret"; fi
done
rm -f "$BASE/bootstrap/transport-key.pem" "$BASE/bootstrap/device-token"
if [ ! -x "$BASE/venv/bin/python" ]; then python3 -m venv "$BASE/venv"; fi
"$BASE/venv/bin/python" -m pip install --disable-pip-version-check -r "$BASE/server/requirements.txt" > "$BASE/install.log" 2>&1
cat > /etc/systemd/system/proactive-dsh-brain.service <<'DSH_UNIT'
[Unit]
Description=Proactive Agent Official DSH Node Brain Sidecar
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=proactive-agent
Group=proactive-agent
WorkingDirectory=/opt/proactive-agent/server/dsh_brain
Environment=DSH_BRAIN_HOST=127.0.0.1
Environment=DSH_BRAIN_PORT=18199
Environment=DSH_BRAIN_DATA_DIR=/opt/proactive-agent/data/dsh_sessions
Environment=PY_GATEWAY_PORT=18190
ExecStart=/usr/local/bin/node --max-old-space-size=640 /opt/proactive-agent/server/dsh_brain/src/server.mjs
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/proactive-agent/data
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
MemoryMax=896M
CPUQuota=100%
TasksMax=64

[Install]
WantedBy=multi-user.target
DSH_UNIT
cat > /etc/systemd/system/proactive-agent.service <<'UNIT'
[Unit]
Description=Proactive Personal Agent (isolated host venv)
After=network-online.target proactive-dsh-brain.service
Wants=network-online.target proactive-dsh-brain.service

[Service]
Type=simple
User=proactive-agent
Group=proactive-agent
WorkingDirectory=/opt/proactive-agent/server
Environment=AGENT_DATA_DIR=/opt/proactive-agent/data
Environment=AGENT_APK=/opt/proactive-agent/dist/proactive-agent.apk
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=MALLOC_ARENA_MAX=2
Environment=BRAIN_ENGINE=dsh
Environment=DSH_BRAIN_URL=http://127.0.0.1:18199
ExecStart=/opt/proactive-agent/venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 18190 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1 --root-path /proactive-agent --no-access-log
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/proactive-agent/data
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
MemoryMax=1280M
CPUQuota=150%
TasksMax=256

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/nginx/snippets/proactive-agent.conf <<'NGINX'
# Owned only by Proactive Agent. Included inside the existing default :80 server.
location = /proactive-agent { return 301 /proactive-agent/; }
location ^~ /proactive-agent/ {
    proxy_pass http://127.0.0.1:18190/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 180s;
    proxy_send_timeout 180s;
    proxy_buffering off;
    client_max_body_size 6m;
    access_log off;
}
NGINX
python3 - <<'PY'
from pathlib import Path
import re,json
default=Path('/etc/nginx/sites-available/default')
candidates=[p for p in Path('/etc/nginx/conf.d').glob('*.conf')
            if re.search(r'(?m)^\s*listen\s+80\s*;',p.read_text())
            and re.search(r'(?m)^\s*server_name\s+216\.23\.82\.196\s*;',p.read_text())]
if len(candidates)>1: raise SystemExit('Ambiguous IP virtual host; refusing routing changes')
target=candidates[0] if candidates else default
text=target.read_text()
line='    include /etc/nginx/snippets/proactive-agent.conf;\n'
rollback={}
if 'include /etc/nginx/snippets/proactive-agent.conf;' not in text:
    matches=list(re.finditer(r'(?m)^\s*server\s*\{',text))
    if len(matches)!=1: raise SystemExit('Expected one unambiguous HTTP server block')
    backup=Path('/opt/proactive-agent/nginx-'+target.name+'.before')
    if not backup.exists(): backup.write_text(text)
    rollback[str(target)]=text
    point=matches[0].end()
    target.write_text(text[:point]+'\n'+line+text[point:])
    print('Added only independent proactive-agent location include to the IP HTTP virtual host')
Path('/opt/proactive-agent/.deploy/nginx-rollback.json').write_text(json.dumps(rollback))
PY
if ! nginx -t; then
  python3 -c 'import json; from pathlib import Path; [(Path(p).write_text(t)) for p,t in json.loads(Path("/opt/proactive-agent/.deploy/nginx-rollback.json").read_text()).items()]'
  echo 'nginx validation failed; this deployment changes restored' >&2
  exit 1
fi
systemctl daemon-reload
systemctl enable --now proactive-dsh-brain.service
systemctl restart proactive-dsh-brain.service
for i in $(seq 1 15); do
  curl -fsS http://127.0.0.1:18199/health >/dev/null 2>&1 && break
  sleep 1
done
systemctl enable --now proactive-agent.service
systemctl restart proactive-agent.service
if [ -d "$BASE/cloud-computer/server" ]; then
  for cf in computer_models.py computer_desktop.py computer_browser.py; do
    if [ -f "$BASE/server/$cf" ]; then cp -f "$BASE/server/$cf" "$BASE/cloud-computer/server/$cf"; fi
  done
  chown proactive-desktop:proactive-desktop "$BASE/cloud-computer/server"/computer_*.py 2>/dev/null || true
  systemctl restart proactive-desktop.service 2>/dev/null || true
fi
systemctl reload nginx.service
for i in $(seq 1 15); do
  curl -fsS http://127.0.0.1:18190/health >/dev/null 2>&1 && break
  sleep 1
done
systemctl is-active proactive-dsh-brain.service
systemctl is-active proactive-agent.service
curl -fsS http://127.0.0.1:18190/health
'''

def run(args,**kw):return subprocess.run(args,check=True,**kw)
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--server-only',action='store_true');args=parser.parse_args()
    work=ROOT/'work';work.mkdir(exist_ok=True)
    archive=work/'deploy.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for file in sorted((ROOT/'server').rglob('*.py')):
            rel = file.relative_to(ROOT/'server').as_posix()
            if not rel.startswith(('tests/', '.venv/')) and '/.venv/' not in rel and '__pycache__' not in rel:
                tar.add(file,arcname='server/'+rel)
        tar.add(ROOT/'server/dsh_brain/package.json',arcname='server/dsh_brain/package.json')
        for mjs in sorted((ROOT/'server/dsh_brain/src').glob('*.mjs')):
            tar.add(mjs,arcname='server/dsh_brain/src/'+mjs.name)
        tar.add(ROOT/'server/requirements.txt',arcname='server/requirements.txt')
        tar.add(ROOT/'private/transport-key.pem',arcname='bootstrap/transport-key.pem')
        tar.add(ROOT/'private/device-token',arcname='bootstrap/device-token')
        if not args.server_only:tar.add(ROOT/'dist/proactive-agent.apk',arcname='dist/proactive-agent.apk')
    script=work/'deploy-remote.sh';script.write_text(REMOTE,encoding='utf-8',newline='\n')
    run(SSH+['install -d -m 755 /opt/proactive-agent; install -d -m 700 /opt/proactive-agent/.deploy; rm -f /opt/proactive-agent/incoming.tar.gz'])
    run(SCP+[str(archive),f'{SSH_HOST}:/opt/proactive-agent/.deploy/incoming.tar.gz'])
    run(SCP+[str(script),f'{SSH_HOST}:/opt/proactive-agent/deploy-remote.sh'])
    run(SSH+['bash /opt/proactive-agent/deploy-remote.sh'])
    # The archive contains bootstrap credentials; remove it after successful installation.
    run(SSH+['rm -f /opt/proactive-agent/.deploy/incoming.tar.gz'])
    print('Deployed host venv service and isolated nginx path. Protected services were not changed.')

if __name__=='__main__':main()
