"""Advisory Loop Nudge Detector (`ok` | `nudge`) for Cloud/Phone/PC GUI tool calls (never hard-breaks turns)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_CANON_TOOLS = {
    'cloud_desktop': 'cloud',
    'phone_workspace': 'phone',
    'phone_macro': 'phone',
    'dsh_workspace': 'pc',
}
_GUI_INTERACTIVE_OPS = frozenset({
    'click', 'double_click', 'right_click', 'click_ref', 'type', 'type_ref',
    'find_text', 'drag', 'scroll', 'swipe', 'long_press', 'key', 'hotkey',
    'click_text', 'tap_node', 'click_node', 'move', 'hover',
    'open', 'launch', 'open_uri', 'navigate',
})
_PASSIVE_OPS = frozenset({
    'zoom', 'crop', 'magnify', 'status', 'ui_tree', 'inspect',
    'screenshot', 'capture', 'frame', 'wait', 'delay', 'sleep',
})
_VOLATILE_ARG_KEYS = frozenset({
    'request_id', 'timestamp', 'timeout', 'timeout_sec', 'duration_ms',
    'settle_ms', 'turn_id', 'bot_id', 'step_note', 'step_thought',
})
_OP_ALIASES = {'tap': 'click', 'input': 'type', 'press': 'key', 'exec': 'bash', 'shell': 'bash'}


@dataclass(frozen=True)
class StuckVerdict:
    stage: str  # 'ok' | 'nudge'
    rule: str = ''
    message: str = ''

    def format_nudge(self) -> str:
        if self.stage != 'nudge' or not self.message:
            return ''
        return f'\n\n<stuck_nudge rule="{self.rule}">\n{self.message}\n</stuck_nudge>'


def _norm_coord_xy(args: dict) -> tuple[int | None, int | None]:
    nx = args.get('nx')
    ny = args.get('ny')
    if nx is not None and ny is not None:
        try:
            nxi, nyi = int(round(float(nx))), int(round(float(ny)))
            if 0 <= nxi <= 1000 and 0 <= nyi <= 1000:
                return nxi, nyi
        except (TypeError, ValueError):
            pass
    x = args.get('x')
    y = args.get('y')
    if x is not None and y is not None:
        try:
            xf, yf = float(x), float(y)
            return max(0, min(1000, int(round(xf * 1000.0 / 1280.0)))), max(0, min(1000, int(round(yf * 1000.0 / 800.0))))
        except (TypeError, ValueError):
            pass
    return None, None


def _coord_bucket(nx: int | None, ny: int | None) -> tuple[int, int] | None:
    if nx is None or ny is None:
        return None
    return (round(nx / 25), round(ny / 25))


def _norm_error(res: dict) -> str:
    raw = str(res.get('error') or '').strip().lower()
    if not raw and res.get('ok') is False:
        raw = f"ok=false op={res.get('op', '')}"
    return re.sub(r'\b\d{4,}\b|[0-9a-f]{8,}', '<id>', raw)[:120]


def _norm_args_sig(op: str, args: dict, *, bucket_coords: bool = False) -> str:
    clean: dict[str, Any] = {}
    for k in sorted(args.keys()):
        if k in _VOLATILE_ARG_KEYS or k in ('op', 'action'):
            continue
        v = args[k]
        if bucket_coords and k in ('nx', 'ny') and v is not None:
            try:
                clean[k] = round(float(v) / 25)
                continue
            except (TypeError, ValueError):
                pass
        clean[k] = v.strip() if isinstance(v, str) else v
    try:
        return json.dumps({'op': op, **clean}, ensure_ascii=False, sort_keys=True)
    except Exception:
        return f'{op}:{sorted(clean.items())}'


def _same_neighborhood(a: dict, b: dict, tol: int = 25) -> bool:
    if a['tool'] != b['tool']:
        return False
    ra, rb = a['ref'], b['ref']
    if ra and rb and ra == rb:
        return True
    nxa, nya = a['nx'], a['ny']
    nxb, nyb = b['nx'], b['ny']
    if nxa is not None and nya is not None and nxb is not None and nyb is not None:
        if _coord_bucket(nxa, nya) == _coord_bucket(nxb, nyb):
            return True
        return abs(nxa - nxb) <= tol and abs(nya - nyb) <= tol
    return a['action_sig'] == b['action_sig']


class CloudStuckDetector:
    """Detects cloud/phone/PC GUI repetition via denoised comparisons and emits advisory nudges."""

    def __init__(self, max_history: int = 24):
        self.max_history = max_history
        self.steps: list[dict[str, Any]] = []
        self.nudge_counts: dict[str, int] = {}

    def _normalize_step(self, tool_name: str, args: dict, result: Any) -> dict[str, Any]:
        canon_tool = _CANON_TOOLS.get(tool_name, tool_name)
        safe_args = args if isinstance(args, dict) else {}
        raw_op = str(safe_args.get('op') or safe_args.get('action') or '').strip().lower()
        if raw_op in ('cua_desktop', 'desktop', 'cua'):
            nested = safe_args.get('args') if isinstance(safe_args.get('args'), dict) else {}
            raw_op = str(safe_args.get('action') or safe_args.get('tool') or nested.get('action') or nested.get('tool') or 'inspect').strip().lower()
        op = _OP_ALIASES.get(raw_op, raw_op)
        res = result if isinstance(result, dict) else {}
        nx, ny = _norm_coord_xy(safe_args)
        ref = str(safe_args.get('ref') or '').strip()
        if not ref and str(safe_args.get('target') or '').strip().startswith(('@b', '@e', '@v')):
            ref = str(safe_args.get('target')).strip()
        err_sig = _norm_error(res)
        ok = bool(not res.get('error') and res.get('ok') is not False)
        gate = res.get('auth_gate') if isinstance(res.get('auth_gate'), dict) else None
        fhash = str(
            res.get('frame_hash')
            or res.get('hash')
            or res.get('frameHash')
            or res.get('treeHash')
            or res.get('tree_hash')
            or res.get('layout_signature')
            or ''
        ).strip()
        return {
            'tool': canon_tool,
            'op': op,
            'args_sig': _norm_args_sig(op, safe_args),
            'action_sig': f"{canon_tool}:{_norm_args_sig(op, safe_args)}",
            'bucket_sig': f"{canon_tool}:{_norm_args_sig(op, safe_args, bucket_coords=True)}",
            'ok': ok,
            'err_sig': err_sig,
            'nx': nx,
            'ny': ny,
            'bucket': _coord_bucket(nx, ny),
            'ref': ref,
            'is_gui_action': canon_tool in ('cloud', 'phone', 'pc') and op in _GUI_INTERACTIVE_OPS,
            'is_passive': op in _PASSIVE_OPS,
            'ui_changed': res.get('ui_changed'),
            'frame_hash': fhash,
            'auth_gate': gate,
        }

    def observe(self, tool_name: str, args: dict, result: Any) -> StuckVerdict:
        rec = self._normalize_step(tool_name, args, result)
        self.steps.append(rec)
        if len(self.steps) > self.max_history:
            del self.steps[:-self.max_history]

        tool_lbl = rec['tool'] or 'cloud'
        ss_hint = (
            f'{tool_lbl}(op="cua_desktop", action="send_screenshot")'
            if tool_lbl == 'pc'
            else f'{tool_lbl}(op="send_screenshot")'
        )
        zoom_hint = (
            f'{tool_lbl}(op="cua_desktop", action="zoom", box=...)'
            if tool_lbl == 'pc'
            else f'{tool_lbl}(op="zoom", box=...)'
        )

        # 1. Auth gate loop (qr_login / captcha)
        if rec['auth_gate'] and rec['is_gui_action']:
            streak = 0
            for item in reversed(self.steps):
                if item['auth_gate'] and item['is_gui_action']:
                    streak += 1
                elif item['is_passive']:
                    continue
                else:
                    break
            if streak >= 2:
                kind = str(rec['auth_gate'].get('kind') or 'auth_gate')
                bbox = rec['auth_gate'].get('bbox')
                box_hint = f'box={bbox}' if bbox else 'full_page=True'
                self.nudge_counts['auth_gate_loop'] = self.nudge_counts.get('auth_gate_loop', 0) + 1
                return StuckVerdict(
                    'nudge',
                    'auth_gate_loop',
                    f'页面出现登录/验证门槛（auth_gate={kind}, {box_hint}），连续 {streak} 次操作仍未通过。可调用 `{ss_hint}` 把清晰截图发给用户请求接手，或用 `{zoom_hint}` 放大检查后再决定下一步。',
                )

        # 2. Action error streak (identical tool + normalized args failing consecutively)
        if not rec['ok']:
            err_streak = 0
            for item in reversed(self.steps):
                if not item['ok'] and item['bucket_sig'] == rec['bucket_sig']:
                    err_streak += 1
                else:
                    break
            if err_streak >= 3:
                self.nudge_counts['action_error_streak'] = self.nudge_counts.get('action_error_streak', 0) + 1
                return StuckVerdict(
                    'nudge',
                    'action_error_streak',
                    f'工具 `{tool_lbl}` 使用相近参数已连续报错 {err_streak} 次（{rec["err_sig"] or "error"}）。请结合报错调整参数、用 `{zoom_hint}` 检查界面或改换其他排查方案。',
                )

        # 3. Visual stagnation (round(v/25) bucket / ±25 norm coords or identical non-coord sig + ui_changed==False / same frame_hash)
        if rec['is_gui_action'] and (rec['ui_changed'] is False or rec['frame_hash']):
            stag = 1
            ref_hash = rec['frame_hash']
            for prev in reversed(self.steps[:-1]):
                if prev['is_passive']:
                    continue
                if not prev['is_gui_action']:
                    break
                no_change = (prev['ui_changed'] is False) or (bool(ref_hash) and prev['frame_hash'] == ref_hash)
                if no_change and _same_neighborhood(prev, rec, tol=25):
                    stag += 1
                else:
                    break
            if stag >= 3:
                self.nudge_counts['visual_stagnation'] = self.nudge_counts.get('visual_stagnation', 0) + 1
                if tool_lbl == 'cloud':
                    ref_warn = '注意 `@bN` 在动态网页中可能漂移失效，立即停止重复点击同族 ref；' if rec['ref'] else ''
                    pivot_msg = (
                        f'连续 {stag} 次在相近目标/坐标操作但云电脑画面未变化。{ref_warn}'
                        '请勿停手等用户确认，立即按优先级切换定位策略：① `cloud(op="eval_js", script="...")` 直接用 CDP/JS 操作 DOM；'
                        '② `cloud(op="zoom", box=...)` 放大后改传精确 `nx, ny` 坐标；③ `cloud(op="key", key="Tab")` 键盘切换。'
                    )
                else:
                    pivot_msg = f'连续 {stag} 次在相近坐标或同一动作上操作但画面未变化。建议用 `{zoom_hint}` 放大核实目标坐标、微调坐标/动作类型或更换入口。'
                return StuckVerdict('nudge', 'visual_stagnation', pivot_msg)

        # 4. Ping-pong loop (2-cycle A->B->A->B->A->B or 3-cycle over GUI actions, using neighborhood / round(v/25) buckets)
        gui_steps = [x for x in self.steps if x['is_gui_action']]
        if len(gui_steps) >= 6:
            tail6 = gui_steps[-6:]
            pair_a = _same_neighborhood(tail6[0], tail6[2], tol=25) and _same_neighborhood(tail6[2], tail6[4], tol=25)
            pair_b = _same_neighborhood(tail6[1], tail6[3], tol=25) and _same_neighborhood(tail6[3], tail6[5], tol=25)
            distinct_ab = not _same_neighborhood(tail6[0], tail6[1], tol=25)
            if pair_a and pair_b and distinct_ab:
                self.nudge_counts['ping_pong_loop'] = self.nudge_counts.get('ping_pong_loop', 0) + 1
                if tool_lbl == 'cloud':
                    pp_msg = '检测到最近 6 步呈现 A→B→A→B→A→B 来回切换。请立即改用 `cloud(op="eval_js", script=...)` 通过 CDP 检查页面状态/DOM，或 `zoom` 放大换路径，不要继续空转也不要因熔断停手等用户。'
                else:
                    pp_msg = f'检测到最近 6 步呈现 A→B→A→B→A→B 来回切换。建议用 `{zoom_hint}` 放大核实页面状态或换一条操作路径。'
                return StuckVerdict('nudge', 'ping_pong_loop', pp_msg)

        if len(gui_steps) >= 9:
            tail9 = gui_steps[-9:]
            c0 = _same_neighborhood(tail9[0], tail9[3], tol=25) and _same_neighborhood(tail9[3], tail9[6], tol=25)
            c1 = _same_neighborhood(tail9[1], tail9[4], tol=25) and _same_neighborhood(tail9[4], tail9[7], tol=25)
            c2 = _same_neighborhood(tail9[2], tail9[5], tol=25) and _same_neighborhood(tail9[5], tail9[8], tol=25)
            if c0 and c1 and c2 and not _same_neighborhood(tail9[0], tail9[1], tol=25):
                self.nudge_counts['ping_pong_loop'] = self.nudge_counts.get('ping_pong_loop', 0) + 1
                return StuckVerdict('nudge', 'ping_pong_loop', f'检测到 3 步周期循环（A→B→C 重复 3 轮）。建议改用 `{zoom_hint}` 放大检查或切换排查思路。')

        # 5. Perceptual frame_hash revisit loop (>= 4 identical frames in recent 10 GUI steps)
        if rec['is_gui_action'] and rec['frame_hash']:
            recent_hashes = [x['frame_hash'] for x in gui_steps[-10:] if x['frame_hash']]
            revisits = sum(1 for fh in recent_hashes if fh == rec['frame_hash'])
            if revisits >= 4:
                self.nudge_counts['state_revisit_loop'] = self.nudge_counts.get('state_revisit_loop', 0) + 1
                return StuckVerdict('nudge', 'state_revisit_loop', f'最近 10 步内已有 {revisits} 次回到同一画面状态。建议用 `{zoom_hint}` 放大核实界面或换一条方案尝试。')

        return StuckVerdict('ok')

    def observe_batch(self, items: list[tuple[str, dict, Any]]) -> StuckVerdict:
        worst = StuckVerdict('ok')
        for tool_name, args, result in items:
            v = self.observe(tool_name, args, result)
            if v.stage == 'nudge':
                worst = v
        return worst
