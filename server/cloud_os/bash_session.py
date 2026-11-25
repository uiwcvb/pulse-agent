"""OpenHands-style Persistent Bash Session & Soft-Timeout State Machine (`server/cloud_os/bash_session.py`)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from cloud_os.tmux_terminal import TMUX_SESSION_NAME, _tmux_sock, ensure_single_tmux_terminal

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
_MAX_OUTPUT_CHARS = 16000


def truncate_middle(text: str, max_chars: int = _MAX_OUTPUT_CHARS) -> str:
    """Keep both head and tail of long outputs so trailing stack traces are preserved."""
    if not text or len(text) <= max_chars:
        return text
    head_len = max(512, (max_chars // 2) - 160)
    tail_len = max(512, max_chars - head_len - 96)
    omitted = len(text) - head_len - tail_len
    return f'{text[:head_len]}\n... [已省略中间 {omitted} 字符以保留首尾上下文] ...\n{text[-tail_len:]}'


def _clean_tty_text(raw: str, cmd_id: Optional[str] = None) -> str:
    txt = _ANSI_RE.sub('', raw or '').replace('\r\n', '\n').replace('\r', '\n')
    if cmd_id:
        begin_marker = f'__DS_BEGIN_{cmd_id}__'
        end_marker = f'__DS_END_{cmd_id}__'
        if begin_marker in txt:
            txt = txt.split(begin_marker, 1)[1]
            if end_marker in txt:
                txt = txt.split(end_marker, 1)[0]
            return txt.strip()
        if end_marker in txt:
            txt = txt.split(end_marker, 1)[0]
    lines = [
        ln for ln in txt.splitlines()
        if not any(tok in ln for tok in ('__DS_ACTIVE_ID=', '__DS_BEGIN_', '__DS_END_', '__ds_run ', 'cloud-1c1g:'))
    ]
    return '\n'.join(lines).strip()


class CloudBashSession:
    """Persistent, interruptible Bash executor backed by tmux `PROMPT_COMMAND` or detached logs."""

    def __init__(self, home: Path, workspace: Path, env_getter: Callable[[], Dict[str, str]]) -> None:
        self.home = home
        self.workspace = workspace
        self._env_getter = env_getter
        self._tmux_initialized = False
        self._active_cmd_id: Optional[str] = None
        self._active_log_path: Optional[Path] = None
        self._active_log_offset: int = 0
        self._active_proc: Optional[subprocess.Popen] = None
        self._active_proc_pgid: Optional[int] = None
        self._last_activity_at: float = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        if self._active_proc is not None and self._active_proc.poll() is None:
            return True
        return self._active_cmd_id is not None

    def _state_dir(self) -> Path:
        st = self.home / '.local' / 'state'
        st.mkdir(parents=True, exist_ok=True)
        return st

    def _cur_cwd(self) -> str:
        cwd_file = self._state_dir() / 'agent_cwd'
        if cwd_file.exists():
            with contextlib.suppress(Exception):
                val = cwd_file.read_text(encoding='utf-8').strip()
                if val:
                    return val
        return str(self.workspace)

    def _tmux_run(self, env: Dict[str, str], *args: str, timeout: float = 3.0) -> subprocess.CompletedProcess:
        tmux_env = {**env, 'SHELL': '/bin/bash'}
        return subprocess.run(['tmux', '-S', _tmux_sock(tmux_env), *args], env=tmux_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)

    def _ensure_tmux_hook(self, env: Dict[str, str]) -> Optional[Path]:
        tmux_env = {**env, 'SHELL': '/bin/bash'}
        st = self._state_dir()
        stream_log = st / 'bash_stream.log'
        if self._tmux_run(tmux_env, 'has-session', '-t', TMUX_SESSION_NAME).returncode != 0:
            self._tmux_initialized = False
        ensure_single_tmux_terminal(tmux_env, self.workspace)
        if not self._tmux_initialized:
            stream_log.touch(exist_ok=True)
            cwd_file = st / 'agent_cwd'
            ready_file = st / 'bash_hook_ready'
            ready_file.unlink(missing_ok=True)
            hook_script = st / 'init_hook.sh'
            hook_script.write_text(
                f'set -m\n'
                f'[ -f "$HOME/.bashrc" ] && source "$HOME/.bashrc" >/dev/null 2>&1 || true\n'
                f'ulimit -f 524288 2>/dev/null || true\n'
                f'__ds_run() {{\n'
                f'  export __DS_ACTIVE_ID="$1"\n'
                f'  printf \'\\n__DS_BEGIN_%s__\\n\' "$1"\n'
                f'  (\n'
                f'    trap \'__ec=$?; pwd > "{cwd_file}" 2>/dev/null || true; exit $__ec\' EXIT\n'
                f'    source "{st}/cmd_$1.sh"\n'
                f'  )\n'
                f'  local __rc=$?\n'
                f'  [ -f "{cwd_file}" ] && builtin cd "$(cat "{cwd_file}")" 2>/dev/null || true\n'
                f'  return $__rc\n'
                f'}}\n'
                f'__ds_prompt_hook() {{\n'
                f'  local __ec=$?\n'
                f'  pwd > "{cwd_file}" 2>/dev/null || true\n'
                f'  if [ -n "$__DS_ACTIVE_ID" ]; then\n'
                f'    local __cid="$__DS_ACTIVE_ID"\n'
                f'    unset __DS_ACTIVE_ID\n'
                f'    printf \'\\n__DS_END_%s__\\n\' "$__cid"\n'
                f'    rm -f "{st}/cmd_${{__cid}}.sh" 2>/dev/null || true\n'
                f'    local __cwd="$(pwd)"\n'
                f'    __cwd="${{__cwd//\\\\/\\\\\\\\}}"\n'
                f'    __cwd="${{__cwd//\\"/\\\\\\"}}"\n'
                f'    __cwd="${{__cwd//$\'\\t\'/\\\\t}}"\n'
                f'    __cwd="${{__cwd//$\'\\r\'/}}"\n'
                f'    __cwd="${{__cwd//$\'\\n\'/\\\\n}}"\n'
                f'    printf \'{{"exit_code":%d,"cwd":"%s"}}\\n\' "$__ec" "$__cwd" > "{st}/bash_done_${{__cid}}.tmp"\n'
                f'    mv -f "{st}/bash_done_${{__cid}}.tmp" "{st}/bash_done_${{__cid}}" 2>/dev/null || true\n'
                f'  fi\n'
                f'}}\n'
                f'PROMPT_COMMAND="__ds_prompt_hook"\n'
                f'touch "{ready_file}"\n',
                encoding='utf-8',
            )
            with contextlib.suppress(Exception):
                self._tmux_run(tmux_env, 'pipe-pane', '-t', TMUX_SESSION_NAME)
                self._tmux_run(tmux_env, 'pipe-pane', '-o', '-t', TMUX_SESSION_NAME, f'cat >> "{stream_log}"')
                self._tmux_run(tmux_env, 'send-keys', '-t', TMUX_SESSION_NAME, f'source "{hook_script}"', 'C-m')
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not ready_file.exists():
                time.sleep(0.05)
            has_sess = self._tmux_run(tmux_env, 'has-session', '-t', TMUX_SESSION_NAME).returncode == 0
            if not ready_file.exists() or not has_sess:
                self._tmux_initialized = False
                return None
            self._tmux_initialized = True
        return stream_log

    async def interrupt(self) -> Dict[str, object]:
        """Interrupt active foreground bash command (`C-c` / `SIGINT`) without killing detached background jobs."""
        self._last_activity_at = time.monotonic()
        env = self._env_getter()
        if shutil.which('tmux') and os.name != 'nt' and self._tmux_initialized:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._tmux_run, env, 'send-keys', '-t', TMUX_SESSION_NAME, 'C-c')
            await asyncio.sleep(0.25)
        if self._active_proc is not None:
            if getattr(self._active_proc, 'stdin', None) is not None:
                with contextlib.suppress(Exception):
                    self._active_proc.stdin.close()
            if self._active_proc.poll() is None and self._active_proc_pgid:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(self._active_proc_pgid, signal.SIGINT)
                await asyncio.sleep(0.25)
                if self._active_proc.poll() is None:
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        os.killpg(self._active_proc_pgid, signal.SIGTERM)
                    await asyncio.sleep(0.2)
                    if self._active_proc.poll() is None:
                        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                            self._active_proc.kill()
                        with contextlib.suppress(Exception):
                            await asyncio.to_thread(self._active_proc.wait, 1.5)
            elif self._active_proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    self._active_proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._active_proc.wait, 1.5)
        self._active_cmd_id = None
        self._active_proc = None
        self._active_proc_pgid = None
        return {'interrupted': True, 'cwd': self._cur_cwd()}

    def _prune_stale_state_files(self, max_age_s: float = 1800.0) -> int:
        st = self._state_dir()
        cutoff = time.time() - max_age_s
        removed = 0
        for pat in ('cmd_*.sh', 'bash_done_*', 'bash_proc_*.log'):
            for fp in st.glob(pat):
                with contextlib.suppress(OSError):
                    if fp.stat().st_mtime < cutoff:
                        fp.unlink(missing_ok=True)
                        removed += 1
        return removed

    async def cleanup_if_idle(self, max_idle_s: float = 1800.0) -> bool:
        """Close idle bash session state and prune stale logs after inactivity."""
        self._prune_stale_state_files(max_age_s=max_idle_s)
        if self.is_running or (time.monotonic() - self._last_activity_at) < max_idle_s:
            return False
        await self.close()
        return True

    async def close(self) -> None:
        """Terminate active child state and release session resources."""
        if self.is_running or self._active_proc is not None:
            await self.interrupt()
        self._prune_stale_state_files(max_age_s=0.0)
        self._tmux_initialized = False

    def _read_slice(self, log_path: Path, start_offset: int) -> tuple[str, int]:
        if not log_path.exists():
            return '', start_offset
        try:
            with log_path.open('rb') as f:
                f.seek(0, io.SEEK_END)
                total_len = f.tell()
                if total_len <= start_offset:
                    return '', start_offset
                f.seek(max(0, start_offset), io.SEEK_SET)
                chunk = f.read()
            return chunk.decode('utf-8', errors='replace'), total_len
        except Exception:
            return '', start_offset

    async def _poll_log_until(self, log_path: Path, start_offset: int, eff_timeout: int, check_done: Callable[[], Optional[Dict[str, Any]]], clean_fn: Callable[[str], str], pid: Optional[int] = None) -> Dict[str, object]:
        started = last_growth = time.monotonic()
        last_len = start_offset
        soft_idle = min(12.0, max(5.0, eff_timeout * 0.45))
        while True:
            done_meta = check_done()
            if done_meta is not None:
                await asyncio.sleep(0.05)
                raw_txt, _ = self._read_slice(log_path, start_offset)
                return {'exit_code': int(done_meta.get('exit_code', 0)), 'output': truncate_middle(clean_fn(raw_txt)), 'timed_out': False, 'running': False, 'is_input': False, 'cwd': str(done_meta.get('cwd') or self._cur_cwd())}
            now = time.monotonic()
            cur_len = log_path.stat().st_size if log_path.exists() else start_offset
            if cur_len > last_len:
                last_len, last_growth = cur_len, now
            if (now - started) >= eff_timeout or (eff_timeout > 12 and (now - started) >= 5.0 and (now - last_growth) >= soft_idle):
                raw_txt, new_off = self._read_slice(log_path, start_offset)
                self._active_log_offset = new_off
                pid_tag = f'pid={pid}, ' if pid is not None else ''
                hint = f'\n[命令仍在运行 ({pid_tag}已等待 {now - started:.1f}s)；可传 is_input=true 继续观察/输入，或调用 stop 中断]'
                res: Dict[str, object] = {'exit_code': -1, 'output': (truncate_middle(clean_fn(raw_txt)) + hint).strip(), 'timed_out': True, 'running': True, 'is_input': True, 'cwd': self._cur_cwd()}
                if pid is not None:
                    res['pid'] = pid
                return res
            await asyncio.sleep(0.1)

    async def run(self, command: str, timeout: int = 35, is_input: bool = False) -> Dict[str, object]:
        """Execute a bash command or continue/feed input to an already running session."""
        async with self._lock:
            self._last_activity_at = time.monotonic()
            cmd = (command or '').rstrip('\r\n')
            if not cmd.strip() and not is_input:
                return {'exit_code': 0, 'output': '', 'running': False, 'timed_out': False, 'cwd': self._cur_cwd()}
            env = self._env_getter()
            eff_timeout = max(2, min(120, int(timeout or 35)))
            if shutil.which('tmux') and os.name != 'nt':
                return await self._run_tmux(cmd, env, eff_timeout, is_input=is_input)
            return await self._run_subprocess_detached(cmd, eff_timeout, is_input=is_input)

    async def _run_tmux(self, cmd: str, env: Dict[str, str], eff_timeout: int, is_input: bool) -> Dict[str, object]:
        st = self._state_dir()
        stream_log = await asyncio.to_thread(self._ensure_tmux_hook, env)
        if stream_log is None:
            return await self._run_subprocess_detached(cmd, eff_timeout, is_input=is_input)
        if self._active_cmd_id and not is_input:
            await self.interrupt()
        if is_input and self._active_cmd_id:
            cmd_id = self._active_cmd_id
            done_file = st / f'bash_done_{cmd_id}'
            start_offset = self._active_log_offset
            if cmd in ('C-c', 'C-d', 'C-z'):
                await asyncio.to_thread(self._tmux_run, env, 'send-keys', '-t', TMUX_SESSION_NAME, cmd)
            elif cmd:
                await asyncio.to_thread(self._tmux_run, env, 'send-keys', '-t', TMUX_SESSION_NAME, '-l', cmd)
                await asyncio.to_thread(self._tmux_run, env, 'send-keys', '-t', TMUX_SESSION_NAME, 'C-m')
        else:
            cmd_id = uuid.uuid4().hex[:10]
            done_file = st / f'bash_done_{cmd_id}'
            done_file.unlink(missing_ok=True)
            if stream_log.exists() and stream_log.stat().st_size > 2_000_000:
                stream_log.write_bytes(b'')
            start_offset = stream_log.stat().st_size if stream_log.exists() else 0
            script_file = st / f'cmd_{cmd_id}.sh'
            script_file.write_text(f'{cmd}\n', encoding='utf-8')
            self._active_cmd_id, self._active_log_path, self._active_log_offset = cmd_id, stream_log, start_offset
            dispatch = f'__ds_run {cmd_id}'
            await asyncio.to_thread(self._tmux_run, env, 'send-keys', '-t', TMUX_SESSION_NAME, '-l', dispatch)
            await asyncio.to_thread(self._tmux_run, env, 'send-keys', '-t', TMUX_SESSION_NAME, 'C-m')

        def _check_done() -> Optional[Dict[str, Any]]:
            if not done_file.exists():
                return None
            meta: Dict[str, Any] = {}
            with contextlib.suppress(Exception):
                meta = json.loads(done_file.read_text(encoding='utf-8'))
            done_file.unlink(missing_ok=True)
            self._active_cmd_id = None
            return meta

        return await self._poll_log_until(
            stream_log,
            start_offset,
            eff_timeout,
            _check_done,
            lambda raw, cid=cmd_id: _clean_tty_text(raw, cid),
        )

    async def _run_subprocess_detached(self, cmd: str, eff_timeout: int, is_input: bool) -> Dict[str, object]:
        """Fallback detached-fd runner via `computer_shell.py` so `&` background jobs return immediately."""
        st = self._state_dir()
        if self._active_proc is not None and self._active_proc.poll() is None and not is_input:
            await self.interrupt()
        if is_input and self._active_proc is not None and self._active_log_path is not None:
            proc, log_path, start_offset = self._active_proc, self._active_log_path, self._active_log_offset
            if proc.poll() is None:
                if cmd in ('C-c', 'C-z'):
                    await self.interrupt()
                elif cmd == 'C-d':
                    if getattr(proc, 'stdin', None) is not None:
                        with contextlib.suppress(Exception):
                            proc.stdin.close()
                elif cmd:
                    if getattr(proc, 'stdin', None) is not None:
                        try:
                            proc.stdin.write((cmd + '\n').encode('utf-8'))
                            proc.stdin.flush()
                        except Exception as exc:
                            return {'exit_code': -1, 'output': f'向子进程 stdin 写入失败: {exc}', 'timed_out': False, 'running': True, 'cwd': self._cur_cwd()}
        else:
            cmd_id = uuid.uuid4().hex[:10]
            log_path = st / f'bash_proc_{cmd_id}.log'
            start_offset = 0
            shell_py = Path(__file__).resolve().parents[1] / 'computer_shell.py'
            self.workspace.mkdir(parents=True, exist_ok=True)
            log_fh = log_path.open('wb')
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(shell_py), str(self.workspace), cmd],
                    stdin=subprocess.PIPE, stdout=log_fh, stderr=subprocess.STDOUT, start_new_session=True,
                )
            finally:
                log_fh.close()
            self._active_proc = proc
            self._active_proc_pgid = proc.pid if os.name != 'nt' else None
            self._active_log_path, self._active_log_offset = log_path, 0

        def _check_proc() -> Optional[Dict[str, Any]]:
            rc = proc.poll()
            if rc is None:
                return None
            self._active_proc = None
            self._active_proc_pgid = None
            return {'exit_code': int(rc)}

        return await self._poll_log_until(log_path, start_offset, eff_timeout, _check_proc, lambda s: (s or '').strip(), pid=proc.pid)
