"""Linux Landlock sandbox for the model's Bash tool (`server/sandbox.py`).

Confines filesystem writes to the workspace while permitting outbound HTTP/HTTPS
requests and read-only system libraries/certs. Dynamically probes the host kernel's
Landlock ABI (1..6) and degrades gracefully when Landlock is unsupported.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path
from computer_security import apply_landlock

try:
    import resource
except ImportError:  # pragma: no cover - Windows/non-POSIX hosts
    resource = None  # type: ignore[assignment]

_FORBIDDEN_ROOTS = frozenset({
    '/', '/bin', '/boot', '/dev', '/etc', '/home', '/lib', '/lib64',
    '/opt', '/proc', '/root', '/run', '/sbin', '/srv', '/sys', '/tmp', '/usr', '/var',
})


def detect_landlock_abi() -> int:
    """Probe the running Linux kernel for the supported Landlock ABI version (0 if unavailable)."""
    if sys.platform != 'linux':
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        ctypes.set_errno(0)
        abi = int(libc.syscall(444, 0, 0, 1))
        if abi >= 1:
            return abi
        return 0
    except Exception:
        return 0


def validate_sandbox_root(raw_root: str) -> str:
    """Validate and canonicalize sandbox root to prevent path traversal or host-root escapes."""
    if not raw_root or '\x00' in raw_root:
        raise ValueError('Invalid sandbox root path')
    parts = Path(raw_root).parts
    if '..' in parts:
        raise ValueError('Path traversal ("..") is not allowed in sandbox root')
    real_root = os.path.realpath(raw_root)
    if not real_root or real_root in _FORBIDDEN_ROOTS:
        raise ValueError(f'Unsafe sandbox root path: {real_root}')
    if not os.path.isdir(real_root):
        raise ValueError(f'Sandbox root does not exist or is not a directory: {real_root}')
    return real_root


def restrict(root: str) -> int:
    """
    Apply Landlock restrictions for `root` using the highest compatible ABI (1..6).
    Returns the applied ABI version, or 0 if Landlock is unavailable on this kernel.
    """
    real_root = validate_sandbox_root(root)
    abi = detect_landlock_abi()
    if abi <= 0:
        return 0
    eff_abi = min(abi, 6)
    ro = [
        '/usr', '/bin', '/lib', '/lib64', '/sbin',
        '/opt/proactive-agent/venv',
        '/etc/alternatives', '/etc/ssl', '/etc/ca-certificates', '/etc/pki',
        '/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/localtime',
        '/etc/debian_version', '/etc/os-release', '/etc/fonts', '/proc',
    ]
    try:
        apply_landlock(ro, [real_root], ('/dev/null', '/dev/urandom'), min_abi=eff_abi)
        return eff_abi
    except OSError as exc:
        if exc.errno in (errno.ENOSYS, errno.EOPNOTSUPP):
            return 0
        raise


if __name__ == '__main__':
    try:
        if len(sys.argv) < 3:
            raise ValueError('Usage: sandbox.py <workspace_root> <command>')
        root = validate_sandbox_root(sys.argv[1])
        if resource is None:
            raise RuntimeError('POSIX resource limits require Linux/POSIX')
        resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        os.chdir(root)
        restrict(root)
        py_user_base = os.environ.get('PYTHONUSERBASE') or os.path.join(root, '.python_packages')
        py_user_bin = os.path.join(py_user_base, 'bin')
        venv_bin = '/opt/proactive-agent/venv/bin'
        path_parts = [p for p in (py_user_bin, venv_bin, '/usr/local/bin', '/usr/bin', '/bin') if os.path.isdir(p)]
        base_path = ':'.join(path_parts) if path_parts else '/usr/local/bin:/usr/bin:/bin'
        env = {
            'PATH': base_path,
            'HOME': root,
            'TMPDIR': root,
            'LANG': os.environ.get('LANG', 'C.UTF-8'),
            'LC_ALL': os.environ.get('LC_ALL', 'C.UTF-8'),
            'TZ': os.environ.get('TZ', 'Asia/Shanghai'),
            'PYTHONUSERBASE': py_user_base,
            'PIP_USER': os.environ.get('PIP_USER', '1'),
        }
        for k in ('API_BASE_URL', 'API_KEY', 'DEFAULT_MODEL', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY'):
            if os.environ.get(k):
                env[k] = os.environ[k]
        os.execve('/bin/bash', ['bash', '--noprofile', '--norc', '-c', sys.argv[2]], env)
    except Exception as e:
        print(f'SANDBOX_REFUSED: {e}', file=sys.stderr)
        sys.exit(126)
