from pathlib import Path
import datetime, hashlib, json, re, shutil, subprocess, sys, tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from deploy_vps import PUBLIC_BASE_URL, SSH, SCP, SSH_HOST

dist = ROOT / 'dist'
dist.mkdir(exist_ok=True)
apk = dist / 'proactive-agent.apk'
src_apk = ROOT / 'android/app/build/outputs/apk/release/app-release.apk'
assert src_apk.is_file() and src_apk.stat().st_size > 0, f'APK missing or empty: {src_apk}'
shutil.copy2(src_apk, apk)
apk_bytes = apk.read_bytes()
apk_size = len(apk_bytes)
apk_sha256 = hashlib.sha256(apk_bytes).hexdigest()
assert apk_size > 0 and apk_bytes[:4] == b'PK\x03\x04', 'Invalid APK zip signature'

gradle_txt = (ROOT / 'android/app/build.gradle').read_text('utf-8')
ver_name = re.search(r"versionName\s+'([^']+)'", gradle_txt).group(1)
ver_code = int(re.search(r"versionCode\s+(\d+)", gradle_txt).group(1))
notes = (
    f"v{ver_name} (Build {ver_code}): "
    "1) 苹果级「纯享正文阅读体 + 顶部唯一过程眉标 + 左滑 1:1 线性拉出工程遥测（缓存命中/TTFT/TPS/轮次）」极简美学 UI（桌面/手机全统一，过程 0 跳动不回弹）；"
    "2) 根治 Node DSH Sidecar 中途意外中断（`python_tools_plugin` 全面告别 Node 22 undici 改走原生 `http`，增加 4 图滑动折叠防 256MB 堆 OOM，提升内存配额）；"
    "3) 全局统一 0..1000 归一化坐标系（去黑边零偏移、DOM 不混发像素、支持 Alipay/收银台二维码智能框选与截图回看确认）；"
    "4) Prompt 与 Tool Schema 瘦身降噪 + 静态前缀锁死 Prefix Cache + 原生 choices 快捷按钮回归。"
)
release_meta = {
    'version': ver_name,
    'version_name': ver_name,
    'versionName': ver_name,
    'tag_name': f'v{ver_name}',
    'version_code': ver_code,
    'versionCode': ver_code,
    'build': ver_code,
    'published_at': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
    'apk_size': apk_size,
    'sha256': apk_sha256,
    'download_url': '/proactive-agent/download/proactive-agent.apk',
    'notes': notes,
    'body': notes,
}
rel_file = dist / 'release.json'
rel_file.write_text(json.dumps(release_meta, ensure_ascii=False, indent=2), encoding='utf-8')
sums_file = dist / 'SHA256SUMS.txt'
if sums_file.exists():
    sums_file.unlink()

ssh_alive = SSH[:-1] + ['-o', 'ServerAliveInterval=15', SSH[-1]]
scp_alive = SCP + ['-o', 'ServerAliveInterval=15']

subprocess.run(ssh_alive + ['mkdir -p /opt/proactive-agent/.deploy /opt/proactive-agent/dist && rm -f /opt/proactive-agent/dist/SHA256SUMS.txt /opt/proactive-agent/.deploy/apk.part*'], check=True)

chunk_size = 4 * 1024 * 1024
part_names: list[str] = []
with tempfile.TemporaryDirectory() as td:
    td_path = Path(td)
    for idx in range(0, apk_size, chunk_size):
        p_name = f'apk.part{idx // chunk_size:02d}'
        p_file = td_path / p_name
        p_file.write_bytes(apk_bytes[idx:idx + chunk_size])
        part_names.append(p_name)
        print(f'Uploading {p_name} ({p_file.stat().st_size} bytes)...', flush=True)
        for attempt in range(3):
            res = subprocess.run(scp_alive + [str(p_file), f'{SSH_HOST}:/opt/proactive-agent/.deploy/{p_name}'])
            if res.returncode == 0:
                break
            if attempt == 2:
                raise subprocess.CalledProcessError(res.returncode, res.args)

subprocess.run(scp_alive + [str(rel_file), f'{SSH_HOST}:/opt/proactive-agent/.deploy/release.json'], check=True)
parts_joined = ' '.join(f'/opt/proactive-agent/.deploy/{n}' for n in part_names)
command = (
    f"cat {parts_joined} > /opt/proactive-agent/.deploy/proactive-agent.apk && "
    f"rm -f {parts_joined} && "
    f"test \"$(sha256sum /opt/proactive-agent/.deploy/proactive-agent.apk | cut -d' ' -f1)\" = \"{apk_sha256}\" && "
    f"test \"$(stat -c%s /opt/proactive-agent/.deploy/proactive-agent.apk)\" = \"{apk_size}\" && "
    "chmod 644 /opt/proactive-agent/.deploy/proactive-agent.apk /opt/proactive-agent/.deploy/release.json && "
    "chown proactive-agent:proactive-agent /opt/proactive-agent/.deploy/proactive-agent.apk /opt/proactive-agent/.deploy/release.json && "
    "mv /opt/proactive-agent/.deploy/proactive-agent.apk /opt/proactive-agent/dist/proactive-agent.apk && "
    "mv /opt/proactive-agent/.deploy/release.json /opt/proactive-agent/dist/release.json"
)
subprocess.run(ssh_alive + [command], check=True)

remote_rel_raw = subprocess.check_output(ssh_alive + ['curl -fsSL http://127.0.0.1:18190/download/release.json'], text=True, encoding='utf-8')
remote_rel = json.loads(remote_rel_raw)
dl_check_raw = subprocess.check_output(
    ssh_alive + ["python3 -c \"import urllib.request,json; r=urllib.request.urlopen('http://127.0.0.1:18190/download/proactive-agent.apk'); data=r.read(); print(json.dumps({'bytes':len(data),'magic_ok':data[:4]==b'PK\\x03\\x04','content_length':r.headers.get('Content-Length')}))\""],
    text=True, encoding='utf-8',
)
dl_check = json.loads(dl_check_raw)
url = f'{PUBLIC_BASE_URL}/download/proactive-agent.apk'
result = {
    'url': url,
    'version': ver_name,
    'versionCode': ver_code,
    'remote_release_versionName': remote_rel.get('versionName'),
    'local_bytes': apk_size,
    'download_bytes': dl_check['bytes'],
    'content_length': dl_check.get('content_length'),
    'magic_ok': dl_check['magic_ok'],
}
assert result['local_bytes'] == result['download_bytes'] and result['magic_ok']
assert remote_rel.get('versionName') == ver_name and remote_rel.get('versionCode') == ver_code
(ROOT / 'work').mkdir(exist_ok=True)
(ROOT / 'work/apk-download-verification.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result, indent=2, ensure_ascii=False))
