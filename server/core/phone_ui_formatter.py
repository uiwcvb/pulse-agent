"""
[Module Responsibility]
Pure Phone VirtualDisplay coordinate & accessibility UI tree node helpers (`server/core/phone_ui_formatter.py`).
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable


def resolve_phone_grid_or_norm(args: dict, width: int, height: int) -> tuple[int, int] | None:
    """Convert grid (`A1..F10`) or normalized 0..1000 (`nx`, `ny`) into pixel coordinates `(px, py)`."""
    grid_raw = str(args.get('grid') or '').strip().upper()
    if grid_raw and len(grid_raw) >= 2 and grid_raw[0].isalpha() and grid_raw[1:].replace('.', '', 1).isdigit():
        col_idx = max(0, min(5, ord(grid_raw[0]) - ord('A')))
        row_val = max(0.0, min(9.9, float(grid_raw[1:]) - 1.0))
        return (
            max(0, min(width - 1, int(((col_idx + 0.5) / 6.0) * width))),
            max(0, min(height - 1, int(((row_val + 0.5) / 10.0) * height))),
        )
    nx, ny = args.get('nx'), args.get('ny')
    if nx is not None and ny is not None:
        try:
            nx_f, ny_f = float(nx), float(ny)
            if nx_f < 0 or nx_f > 1000 or ny_f < 0 or ny_f > 1000:
                args['_coord_error'] = f'无效坐标 (nx={nx}, ny={ny})：必须在 0..1000 归一化范围内'
                return None
            return (
                max(0, min(width - 1, int(round(nx_f * width / 1000.0)))),
                max(0, min(height - 1, int(round(ny_f * height / 1000.0)))),
            )
        except (ValueError, TypeError):
            args['_coord_error'] = f'无效坐标 (nx={nx}, ny={ny})：必须为 0..1000 数值'
            return None
    return None


def parse_phone_nodes(res_dict: dict, fallback_size: tuple[int, int] = (1080, 2400)) -> list[dict]:
    """Parse structured `nodes` and textual `ui_tree` lines into normalized node records."""
    w = int(res_dict.get('width') or fallback_size[0] or 1080)
    h = int(res_dict.get('height') or fallback_size[1] or 2400)
    by_ref: dict[str, dict] = {}
    raw_list = res_dict.get('nodes')
    if isinstance(raw_list, list):
        for idx, item in enumerate(raw_list, 1):
            if not isinstance(item, dict):
                continue
            ref_s = str(item.get('ref') or f'@e{idx}').strip()
            if not ref_s.startswith('@'):
                ref_s = '@' + ref_s
            cx = item.get('x') if item.get('x') is not None else item.get('center_x')
            cy = item.get('y') if item.get('y') is not None else item.get('center_y')
            nx_v, ny_v = item.get('nx'), item.get('ny')
            if (cx is None or cy is None) and nx_v is not None and ny_v is not None:
                cx = int(round(float(nx_v) * w / 1000.0))
                cy = int(round(float(ny_v) * h / 1000.0))
            by_ref[ref_s.lower()] = {
                'ref': ref_s,
                'role': str(item.get('role') or item.get('className') or item.get('class') or '').split('.')[-1],
                'text': str(item.get('text') or item.get('label') or '').strip(),
                'desc': str(item.get('desc') or item.get('contentDescription') or '').strip(),
                'hint': str(item.get('hint') or item.get('hintText') or '').strip(),
                'id': str(item.get('id') or item.get('view_id') or item.get('resourceId') or '').strip(),
                'clickable': bool(item.get('clickable', False)),
                'editable': bool(item.get('editable', False)),
                'x': int(float(cx)) if cx is not None else None,
                'y': int(float(cy)) if cy is not None else None,
            }
    tree_txt = str(res_dict.get('ui_tree') or res_dict.get('tree') or res_dict.get('summary') or '')
    for line in tree_txt.splitlines():
        m_ref = re.match(r'^\s*(@[ev]\d+)\b(.*)$', line.strip(), flags=re.IGNORECASE)
        if not m_ref:
            continue
        ref_s, rest = m_ref.group(1), m_ref.group(2)
        cur = by_ref.get(
            ref_s.lower(),
            {'ref': ref_s, 'role': '', 'text': '', 'desc': '', 'hint': '', 'id': '', 'clickable': False, 'editable': False, 'x': None, 'y': None},
        )
        m_role = re.search(r'\[([A-Za-z0-9_.$]+)\]', rest)
        if m_role and not cur['role']:
            cur['role'] = m_role.group(1).split('.')[-1]
        m_id = re.search(r'\bid=([^\s"\[\]]+)', rest)
        if m_id and not cur['id']:
            cur['id'] = m_id.group(1).strip()
        m_desc = re.search(r'\bdesc="([^"]*)"', rest)
        if m_desc and not cur['desc']:
            cur['desc'] = m_desc.group(1).strip()
        m_hint = re.search(r'\bhint="([^"]*)"', rest)
        if m_hint and not cur['hint']:
            cur['hint'] = m_hint.group(1).strip()
        rest_no_kv = re.sub(r'\b(?:id|desc|hint)="[^"]*"', '', rest)
        m_txt = re.search(r'"([^"]+)"', rest_no_kv)
        if m_txt and not cur['text']:
            cur['text'] = m_txt.group(1).strip()
        if re.search(r'\bclick\b', rest, flags=re.IGNORECASE):
            cur['clickable'] = True
        if re.search(r'\bedit\b', rest, flags=re.IGNORECASE) or cur['role'] in ('EditText', 'AutoCompleteTextView', 'SearchBox'):
            cur['editable'] = True
        if cur['x'] is None or cur['y'] is None:
            m_norm = re.search(r'norm=\(\s*(\d+)\s*,\s*(\d+)\s*\)', rest)
            if m_norm:
                cur['x'] = int(round(float(m_norm.group(1)) * w / 1000.0))
                cur['y'] = int(round(float(m_norm.group(2)) * h / 1000.0))
            else:
                m_b = re.search(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', rest)
                if m_b:
                    cur['x'] = (int(m_b.group(1)) + int(m_b.group(3))) // 2
                    cur['y'] = (int(m_b.group(2)) + int(m_b.group(4))) // 2
        by_ref[ref_s.lower()] = cur
    return list(by_ref.values())


def match_phone_node(
    nodes: list[dict],
    target_q: str = '',
    ref_q: str = '',
    id_q: str = '',
    for_type: bool = False,
) -> dict | None:
    if not nodes:
        return None
    r_norm, t_norm, i_norm = str(ref_q or '').strip(), str(target_q or '').strip(), str(id_q or '').strip()
    for cand in (r_norm, t_norm, i_norm):
        if cand and re.fullmatch(r'@?[ev]\d+', cand, flags=re.IGNORECASE):
            r_norm = cand if cand.startswith('@') else ('@' + cand)
            if t_norm == cand:
                t_norm = ''
            if i_norm == cand:
                i_norm = ''
            break
    if r_norm:
        for n in nodes:
            if str(n.get('ref') or '').lower() == r_norm.lower():
                return n
    queries = [q for q in (i_norm, t_norm) if q]
    if not queries:
        return None
    best_node, best_score = None, 0
    for n in nodes:
        nid = str(n.get('id') or '').strip()
        nid_short = nid.split('/')[-1] if '/' in nid else nid
        ntxt = str(n.get('text') or '')
        ndesc = str(n.get('desc') or '')
        nhint = str(n.get('hint') or '')
        nrole = str(n.get('role') or '')
        is_edit_like = bool(
            n.get('editable')
            or nrole in ('EditText', 'AutoCompleteTextView', 'SearchBox')
        )
        if for_type and not is_edit_like and not n.get('clickable'):
            continue
        score = 0
        for q in queries:
            ql = q.lower()
            if nid and (nid.lower() == ql or nid_short.lower() == ql):
                score = max(score, 1200)
            elif any(f and f.lower() == ql for f in (ntxt, nhint, ndesc)):
                score = max(score, 1000)
            elif nid and ql in nid.lower():
                score = max(score, 650)
            elif any(f and ql in f.lower() for f in (ntxt, nhint, ndesc)):
                score = max(score, 500)
        if score > 0 and for_type and is_edit_like:
            score += 350
        if score > best_score:
            best_score, best_node = score, n
    return best_node


def format_available_nodes_hint(nodes: list[dict], max_items: int = 10) -> str:
    if not nodes:
        return '当前页面无无障碍节点，请直接根据实时画面 0..1000 标尺传 nx, ny 视觉坐标操作（或调用 phone(op="inspect") 刷新）'
    parts = []
    for n in nodes[:max_items]:
        lbl = n.get('text') or n.get('hint') or n.get('desc') or ''
        id_s = f" id={n['id'].split('/')[-1]}" if n.get('id') else ''
        flags = ','.join(k for k, v in (('click', n.get('clickable')), ('edit', n.get('editable'))) if v)
        parts.append(f"{n.get('ref')} [{n.get('role') or 'View'}] \"{lbl}\"{id_s}{' ' + flags if flags else ''}")
    return '当前可用节点: ' + '; '.join(parts)


async def exec_phone_shell(
    args: dict[str, Any],
    node_st: Any,
    raw_phone_rpc_fn: Callable[..., Awaitable[dict]],
) -> dict[str, Any]:
    """Execute guarded adb-equivalent shell on the virtual-display Android node."""
    cmd_str = str(args.get('command') or args.get('text') or args.get('target') or args.get('goal') or '').strip()
    if not cmd_str:
        return {'ok': False, 'error': 'phone(op="shell") 需要提供 command 参数'}
    if re.search(r'\b(reboot|poweroff|shutdown|rm\s+-rf\s+/data|mkfs|dd\s+if=)\b', cmd_str, flags=re.IGNORECASE):
        return {'ok': False, 'error': '安全保护已拦截高危系统关机/格式化指令'}
    if re.search(
        r'\bcmd\s+uimode\b|\bservice\s+call\s+uimode\b|\bsettings\s+put\s+(?:system|secure|global)\s+(?:ui_night_mode|screen_brightness|screen_off_timeout|font_scale|user_rotation|accelerometer_rotation|default_input_method|enabled_input_methods)\b',
        cmd_str,
        flags=re.IGNORECASE,
    ):
        return {'ok': False, 'error': '安全沙箱已拦截系统全局设置修改指令'}
    if re.search(r'\bmonkey\b|(?:--display|-d)\s+0\b', cmd_str, flags=re.IGNORECASE):
        return {'ok': False, 'error': '安全沙箱已拦截指向主屏幕 (Display 0) 或 monkey 的指令，请使用 phone(op="open"|"tap") 在独立虚拟屏内操作'}
    cur_disp_env = int(node_st.display_id or 0)
    rpc_cmd_str = f'export DISPLAY_ID={cur_disp_env}; {cmd_str}' if (cur_disp_env > 0 and 'DISPLAY_ID=' not in cmd_str) else cmd_str
    raw_sh = await raw_phone_rpc_fn('shell', {'op': 'shell', 'command': rpc_cmd_str, **({'source': node_st.source} if args.get('source') else {})}, timeout=35.0)
    if not isinstance(raw_sh, dict):
        return {'ok': False, 'op': 'shell', 'error': '手机端 shell 返回无效响应'}
    is_ok = bool(raw_sh.get('ok', 'error' not in raw_sh)) and not bool(raw_sh.get('error'))
    raw_ec = raw_sh.get('exit_code')
    eff_ec = -1 if (not is_ok and (raw_ec is None or int(raw_ec) == 0)) else int(raw_ec if raw_ec is not None else (0 if is_ok else -1))
    out_text = str(raw_sh.get('output') if 'output' in raw_sh else raw_sh.get('stdout', ''))
    if 'screencap' in cmd_str.lower() and ('status: -2' in out_text.lower() or eff_ec != 0):
        out_text = (out_text + '\n[提示：独立虚拟屏启用了 FLAG_SECURE 保护；请改用 phone(op="inspect") 或 phone(op="send_screenshot") 获取画面。]').strip()
    clean_sh: dict[str, Any] = {'ok': is_ok, 'op': 'shell', 'output': out_text, 'exit_code': eff_ec}
    if raw_sh.get('cwd'):
        clean_sh['cwd'] = str(raw_sh['cwd'])
    if raw_sh.get('error'):
        err_s = re.sub(r'(Root\s*工具进程握手未完成)\s*:\s*ok\b', r'\1', str(raw_sh['error'])).strip()
        clean_sh['error'] = err_s
        if '握手未完成' in err_s:
            clean_sh['root_state'] = 'handshake_failed'
            clean_sh['hint'] = 'Root 守护进程握手未完成：请重启手机端 App 或检查 su/Zygisk 授权'
    if raw_sh.get('stderr'):
        clean_sh['stderr'] = str(raw_sh['stderr'])
    return clean_sh


async def exec_phone_pull_file(
    args: dict[str, Any],
    node_st: Any,
    raw_phone_rpc_fn: Callable[..., Awaitable[dict]],
) -> dict[str, Any]:
    """Pull a file (or resolve the newest download/media file) from Android node."""
    file_p = str(args.get('path') or args.get('target') or args.get('text') or 'latest').strip()
    src_dest = str(args.get('source') or 'workspace').strip().lower()
    if src_dest not in ('workspace', 'computer'):
        src_dest = 'workspace'
    src_extra = {'source': node_st.source} if args.get('source') in ('phone', 'phone2', '安卓一', '安卓二') else {}
    is_latest_query = file_p.lower() in ('latest', '', 'newest', 'latest_download')
    if is_latest_query:
        resolved_latest = ''
        for candidate_dir in ('/sdcard/Download/DeepSeek Claw', '/sdcard/Download', '/sdcard/Pictures/Screenshots', '/sdcard/DCIM/Camera'):
            lf = await raw_phone_rpc_fn('list_files', {'op': 'list_files', 'path': candidate_dir, 'limit': 15, **src_extra}, timeout=12.0)
            items = (lf.get('files') or lf.get('entries') or []) if isinstance(lf, dict) else []
            file_items = [it for it in items if isinstance(it, dict) and not it.get('is_dir') and not it.get('isDirectory') and (it.get('path') or it.get('name'))]
            if file_items:
                picked = file_items[0]
                resolved_latest = str(picked.get('path') or f"{candidate_dir}/{picked.get('name')}")
                break
        if not resolved_latest:
            sh_cmd = (
                "for d in '/sdcard/Download' '/sdcard/Pictures' '/sdcard/DCIM' '/sdcard/Documents'; do "
                "[ -d \"$d\" ] && find \"$d\" -maxdepth 3 -type f 2>/dev/null; "
                "done | while IFS= read -r f; do "
                "ts=$(stat -c '%Y' \"$f\" 2>/dev/null || echo 0); printf '%s\\t%s\\n' \"$ts\" \"$f\"; "
                "done | sort -nr | head -n 1 | cut -f2-"
            )
            sh_res = await raw_phone_rpc_fn('shell', {'op': 'shell', 'command': sh_cmd, **src_extra}, timeout=15.0)
            cand_line = str((sh_res or {}).get('output') or '').strip().splitlines()
            if cand_line and cand_line[0].strip().startswith('/'):
                resolved_latest = cand_line[0].strip()
        if not resolved_latest:
            direct_pull = await raw_phone_rpc_fn('pull_file', {'op': 'pull_file', 'path': 'latest', 'source': src_dest}, timeout=45.0)
            if isinstance(direct_pull, dict) and direct_pull.get('ok') and (direct_pull.get('file') or direct_pull.get('saved_path') or direct_pull.get('url')):
                direct_pull['resolved_from'] = 'latest'
                return direct_pull
            return {
                'ok': False, 'op': 'pull_file', 'resolved_mode': 'latest_scan',
                'error': '已扫描手机常用下载与相册目录，当前未找到可拉取的普通文件，请确认路径或传入具体 path。',
            }
        file_p = resolved_latest
    res_pull = await raw_phone_rpc_fn('pull_file', {'op': 'pull_file', 'path': file_p, 'source': src_dest}, timeout=45.0)
    if isinstance(res_pull, dict) and is_latest_query:
        res_pull['resolved_from'] = 'latest'
        res_pull['resolved_phone_path'] = file_p
    return res_pull


async def exec_phone_tap_and_type(
    args: dict[str, Any],
    node_st: Any,
    orig_requested_op: str,
    ref_arg: str,
    target_only_arg: str,
    id_arg: str,
    before_pkg_state: str,
    raw_phone_rpc_fn: Callable[..., Awaitable[dict]],
    parse_nodes_fn: Callable[..., list[dict]],
) -> tuple[dict | None, dict | None, int | None, int | None]:
    """Tap target input field (explicit coords or node), input text, optionally submit, with honest failure handling."""
    content_txt = str(args.get('text') or '')
    tx = int(args['x']) if args.get('x') is not None else None
    ty = int(args['y']) if args.get('y') is not None else None
    rpc_src = {'source': node_st.source} if args.get('source') else {}

    if (tx is None or ty is None) and (ref_arg or target_only_arg or id_arg):
        if not node_st.last_nodes:
            pre_nodes_r = await raw_phone_rpc_fn('ui_tree', {'op': 'ui_tree', 'refresh': True, **rpc_src}, timeout=8.0)
            if isinstance(pre_nodes_r, dict):
                parse_nodes_fn(pre_nodes_r, node_st=node_st)
        matched_n = match_phone_node(node_st.last_nodes, target_q=target_only_arg, ref_q=ref_arg, id_q=id_arg, for_type=True)
        if matched_n:
            if not ref_arg and matched_n.get('ref'):
                ref_arg = str(matched_n['ref'])
            if not id_arg and matched_n.get('id'):
                id_arg = str(matched_n['id'])
            if tx is None and matched_n.get('x') is not None and matched_n.get('y') is not None:
                tx, ty = int(matched_n['x']), int(matched_n['y'])
        elif not id_arg and target_only_arg and re.fullmatch(r'[a-zA-Z0-9_.:/-]+', target_only_arg) and ('_' in target_only_arg or ':id/' in target_only_arg):
            id_arg = target_only_arg

    if tx is not None and ty is not None:
        tap_r = await raw_phone_rpc_fn('tap', {'op': 'tap', 'x': tx, 'y': ty, **rpc_src})
    elif ref_arg or target_only_arg or id_arg:
        tap_r = await raw_phone_rpc_fn('tap_node', {'op': 'tap_node', 'ref': ref_arg, 'text': target_only_arg, 'id': id_arg, **rpc_src})
        if isinstance(tap_r, dict) and not tap_r.get('error') and tap_r.get('ok') is not False:
            if tap_r.get('x') is not None and tap_r.get('y') is not None:
                tx, ty = int(float(tap_r['x'])), int(float(tap_r['y']))
    else:
        return {
            'ok': False, 'op': orig_requested_op, 'typed': False,
            'error': 'phone(op="type") 必须提供输入框的 nx, ny (0..1000 视觉坐标) 或有效的 target',
        }, None, tx, ty

    if not isinstance(tap_r, dict) or tap_r.get('error') or tap_r.get('ok') is False:
        err_msg = str((tap_r or {}).get('error') or '点击目标输入框失败')
        return {**(tap_r if isinstance(tap_r, dict) else {}), 'ok': False, 'op': orig_requested_op, 'typed': False, 'error': err_msg}, None, tx, ty

    tap_pkg = str(tap_r.get('package') or before_pkg_state or '')
    tap_cnt = int(tap_r.get('node_count') or tap_r.get('nodeCount') or 0)
    tap_hash = str(tap_r.get('treeHash') or tap_r.get('tree_hash') or '')
    steps_executed = [{'step': 1, 'op': 'tap', 'ref': ref_arg, 'x': tx, 'y': ty, 'package': tap_pkg, 'node_count': tap_cnt, 'treeHash': tap_hash}]
    await asyncio.sleep(0.32)

    clear_flag = bool(args.get('clear', False))
    if content_txt or clear_flag:
        type_r = await raw_phone_rpc_fn('text', {'op': 'text', 'text': content_txt, 'clear': clear_flag, **rpc_src})
        if (not isinstance(type_r, dict) or type_r.get('error') or type_r.get('ok') is False) and (ref_arg or id_arg or target_only_arg):
            alt_type = await raw_phone_rpc_fn('type_node', {'op': 'type_node', 'ref': ref_arg, 'id': id_arg, 'target': target_only_arg, 'text': content_txt, 'clear': clear_flag, **rpc_src})
            if isinstance(alt_type, dict) and not alt_type.get('error') and alt_type.get('ok') is not False:
                type_r = alt_type
    else:
        type_r = {'ok': True}

    if not isinstance(type_r, dict) or type_r.get('error') or type_r.get('ok') is False:
        err_msg = str((type_r or {}).get('error') or '向输入框写入文本失败')
        return {**(type_r if isinstance(type_r, dict) else {}), 'ok': False, 'op': orig_requested_op, 'typed': False, 'x': tx, 'y': ty, 'error': err_msg}, None, tx, ty

    pkg_before_submit = str(type_r.get('package') or tap_pkg or before_pkg_state or '')
    type_cnt = int(type_r.get('node_count') or type_r.get('nodeCount') or tap_cnt)
    type_hash = str(type_r.get('treeHash') or type_r.get('tree_hash') or tap_hash)
    steps_executed.append({'step': 2, 'op': 'text', 'text': content_txt, 'package': pkg_before_submit, 'node_count': type_cnt, 'treeHash': type_hash})
    pkg_after_submit = pkg_before_submit
    if args.get('submit'):
        await asyncio.sleep(0.22)
        key_r = await raw_phone_rpc_fn('key', {'op': 'key', 'key': 'enter', **rpc_src})
        if not isinstance(key_r, dict) or key_r.get('error') or key_r.get('ok') is False:
            return {
                **(key_r if isinstance(key_r, dict) else {}),
                'ok': False, 'op': orig_requested_op, 'typed': bool(content_txt), 'submitted': False,
                'error': str((key_r or {}).get('error') or '提交按键 (enter) 失败'),
            }, None, tx, ty
        pkg_after_submit = str(key_r.get('package') or pkg_before_submit)
        key_cnt = int(key_r.get('node_count') or key_r.get('nodeCount') or type_cnt)
        key_hash = str(key_r.get('treeHash') or key_r.get('tree_hash') or type_hash)
        steps_executed.append({'step': 3, 'op': 'key', 'key': 'enter', 'package': pkg_after_submit, 'node_count': key_cnt, 'treeHash': key_hash})
        if key_r.get('ui_tree') or key_r.get('tree') or key_r.get('summary'):
            type_r = key_r

    _stale_tap_keys = ('error', 'image', '_image_b64', 'ui_tree', 'tree', 'summary', 'treeHash', 'tree_hash', 'package', 'node_count', 'nodeCount', 'count', 'change_summary', 'ui_changed', 'layout_signature')
    clean_tap = {k: v for k, v in tap_r.items() if k not in _stale_tap_keys}
    clean_type = {k: v for k, v in type_r.items() if k not in ('error', 'image', '_image_b64')}
    res = {
        **clean_tap, **clean_type, 'ok': True, 'op': 'tap_and_type', 'ref': ref_arg, 'id': id_arg,
        'x': tx, 'y': ty, 'typed': bool(content_txt), 'submitted': bool(args.get('submit')), 'steps_executed': steps_executed,
    }
    if args.get('submit') and pkg_before_submit and pkg_after_submit and pkg_after_submit != pkg_before_submit:
        res['submit_triggered_navigation'] = True
        res['package_before_submit'] = pkg_before_submit
        res['package_after_submit'] = pkg_after_submit
    return None, res, tx, ty
