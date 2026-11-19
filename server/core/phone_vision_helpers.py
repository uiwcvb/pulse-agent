"""Pure-vision & diff helpers for PhoneControllerMixin (`server/core/phone_vision_helpers.py`)."""
from __future__ import annotations

import base64
import io
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from core.phone_ui_formatter import parse_phone_nodes
from core.vfs_router import enrich_phone_frame_contract
from core.vision_engine import (
    annotate_tap_crosshair,
    compute_frame_diff_pct,
    compute_perceptual_frame_hash,
    draw_vision_ruler_overlay,
)


def validate_norm_box_xyxy(args: dict) -> tuple[list[float] | None, str | None]:
    """Validate optional `box`/`zoom_box`/`crop_box` strictly as `[xmin, ymin, xmax, ymax]` in 0..1000."""
    raw = None
    for k in ('box', 'zoom_box', 'crop_box'):
        if args.get(k) is not None:
            raw = args.get(k)
            break
    if raw is None:
        return None, None
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None, f'无效的裁剪框 {raw}：必须为 [xmin, ymin, xmax, ymax] 4 个 0..1000 数值'
    try:
        x1, y1, x2, y2 = float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])
    except (ValueError, TypeError):
        return None, f'无效的裁剪框 {raw}：坐标必须为数值'
    if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000):
        return None, f'无效的裁剪框 {raw}：要求满足 0 <= xmin < xmax <= 1000 且 0 <= ymin < ymax <= 1000'
    return [x1, y1, x2, y2], None


def build_phone_swipe_args(
    args: dict,
    cur_w: int,
    cur_h: int,
    orig_op: str,
) -> tuple[str, dict, str, str | None]:
    """Normalize `scroll`/`swipe` with strict 0..1000 validation and `dx`/`dy`/`amount`/`pages` support."""
    raw_dir = str(args.get('direction') or '').strip().lower()
    if raw_dir and raw_dir not in ('up', 'down', 'left', 'right'):
        return orig_op, args, '', f'无效的滚动方向 direction="{raw_dir}"，仅支持 up / down / left / right'
    if orig_op == 'scroll' and (args.get('target') or args.get('text') or args.get('label')):
        return 'scroll_to', {**args, 'op': 'scroll_to'}, '', None

    for k in ('nx', 'ny', 'nx2', 'ny2'):
        if args.get(k) is not None:
            try:
                v = float(args[k])
            except (ValueError, TypeError):
                return orig_op, args, '', f'无效坐标 {k}={args[k]}：必须为 0..1000 数值'
            if v < 0 or v > 1000:
                return orig_op, args, '', f'无效坐标 {k}={args[k]}：必须在 0..1000 归一化范围内'

    has_explicit_coords = any(
        args.get(k) is not None for k in ('x1', 'start_x', 'nx', 'dx', 'dy')
    ) or bool(args.get('grid'))
    pages_raw = args.get('pages') if args.get('pages') is not None else args.get('amount')
    span_scale = 1.0
    if pages_raw is not None:
        try:
            span_scale = max(0.35, min(1.4, float(pages_raw) * 0.62 if args.get('pages') is not None else float(pages_raw) / 500.0))
        except (ValueError, TypeError):
            span_scale = 1.0

    dur_s = max(180, min(1200, int(args.get('duration_ms') or 300)))
    if not has_explicit_coords:
        direction = raw_dir or 'down'
        delta_y = int(cur_h * 0.31 * span_scale)
        delta_x = int(cur_w * 0.30 * span_scale)
        cy, cx = cur_h // 2, cur_w // 2
        if direction == 'up':
            sx1, sy1, sx2, sy2 = cx, max(20, cy - delta_y), cx, min(cur_h - 20, cy + delta_y)
        elif direction == 'left':
            sx1, sy1, sx2, sy2 = min(cur_w - 20, cx + delta_x), cy, max(20, cx - delta_x), cy
        elif direction == 'right':
            sx1, sy1, sx2, sy2 = max(20, cx - delta_x), cy, min(cur_w - 20, cx + delta_x), cy
        else:
            sx1, sy1, sx2, sy2 = cx, min(cur_h - 20, cy + delta_y), cx, max(20, cy - delta_y)
        return 'swipe', {**args, 'op': 'swipe', 'x1': sx1, 'y1': sy1, 'x2': sx2, 'y2': sy2, 'duration_ms': dur_s}, direction, None

    sx1 = args.get('x1') if args.get('x1') is not None else (args.get('start_x') if args.get('start_x') is not None else args.get('x', cur_w // 2))
    sy1 = args.get('y1') if args.get('y1') is not None else (args.get('start_y') if args.get('start_y') is not None else args.get('y', int(cur_h * 0.75)))
    if args.get('nx') is not None and args.get('ny') is not None:
        sx1 = int(round(float(args['nx']) * cur_w / 1000.0))
        sy1 = int(round(float(args['ny']) * cur_h / 1000.0))

    sx2 = args.get('x2') if args.get('x2') is not None else args.get('end_x')
    sy2 = args.get('y2') if args.get('y2') is not None else args.get('end_y')
    if args.get('nx2') is not None and args.get('ny2') is not None:
        sx2 = int(round(float(args['nx2']) * cur_w / 1000.0))
        sy2 = int(round(float(args['ny2']) * cur_h / 1000.0))
    elif sx2 is None and sy2 is None and (args.get('dx') is not None or args.get('dy') is not None):
        dx_px = int(round(float(args.get('dx') or 0) * cur_w / 1000.0))
        dy_px = int(round(float(args.get('dy') or 0) * cur_h / 1000.0))
        sx2 = max(0, min(cur_w - 1, int(sx1) + dx_px))
        sy2 = max(0, min(cur_h - 1, int(sy1) + dy_px))
    if sx2 is None:
        sx2 = cur_w // 2
    if sy2 is None:
        sy2 = int(cur_h * 0.25)
    return 'swipe', {**args, 'op': 'swipe', 'x1': int(sx1), 'y1': int(sy1), 'x2': int(sx2), 'y2': int(sy2), 'duration_ms': dur_s}, raw_dir, None


async def run_phone_macro_action(
    args: dict,
    workspace_root: Path,
    exec_phone_workspace_fn: Callable[[dict], Awaitable[dict]],
) -> dict:
    """Manage and execute reusable phone action macros under `.config/phone_macros.json`."""
    action = str(args.get('action') or 'list').strip().lower()
    cfg_dir = workspace_root / '.config'
    cfg_dir.mkdir(parents=True, exist_ok=True)
    macro_file = cfg_dir / 'phone_macros.json'
    macros: dict = {}
    if macro_file.exists():
        try:
            macros = json.loads(macro_file.read_text('utf-8'))
        except Exception:
            macros = {}
    _op_map = {
        'launch': 'open', 'tap_node': 'tap', 'click_node': 'tap', 'tap_and_type': 'type',
        'type_node': 'type', 'input_node': 'type', 'text': 'type', 'key': 'press',
        'swipe': 'scroll', 'ui_tree': 'inspect', 'screenshot': 'inspect', 'capture': 'inspect',
        'delay': 'wait', 'sleep': 'wait',
    }
    _valid_ops = {
        'open', 'open_uri', 'inspect', 'tap', 'type', 'scroll', 'scroll_to', 'press',
        'long_press', 'wait', 'send_screenshot', 'pull_file', 'push_file', 'list_files',
        'notifications', 'device_info', 'shell', 'status', 'apps', 'release', 'ime_policy',
    }
    if action == 'list':
        return {'ok': True, 'macros': macros}
    name = str(args.get('name') or '').strip()
    if not name:
        return {'ok': False, 'error': '请提供宏名称 name'}
    if action == 'save':
        steps = args.get('steps') if isinstance(args.get('steps'), list) else []
        if not steps:
            return {'ok': False, 'error': 'steps 不能为空'}
        norm_steps = []
        for idx_st, st in enumerate(steps[:30], 1):
            if not isinstance(st, dict):
                return {'ok': False, 'error': f'第 {idx_st} 步格式错误：必须为 JSON 对象'}
            raw_sop = str(st.get('op') or '').strip().lower()
            sop = _op_map.get(raw_sop, raw_sop)
            if not sop or sop not in _valid_ops:
                return {'ok': False, 'error': f'第 {idx_st} 步包含不支持的 op="{raw_sop}"'}
            norm_steps.append({**st, 'op': sop})
        macros[name] = {'name': name, 'description': str(args.get('description') or ''), 'steps': norm_steps, 'updated_at': time.time()}
        macro_file.write_text(json.dumps(macros, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'ok': True, 'saved': True, 'name': name, 'steps_count': len(norm_steps)}
    if action == 'delete':
        existed = name in macros
        macros.pop(name, None)
        macro_file.write_text(json.dumps(macros, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'ok': existed, 'deleted': existed, 'existed': existed, 'name': name, **({'error': f'未找到手机动作宏 "{name}"'} if not existed else {})}
    if action == 'run':
        item = macros.get(name)
        if not item:
            return {'ok': False, 'error': f'未找到手机动作宏 "{name}"'}
        raw_steps = item.get('steps', [])
        tpl_vars = args.get('vars') if isinstance(args.get('vars'), dict) else {}
        if tpl_vars:
            steps_str = json.dumps(raw_steps, ensure_ascii=False)
            for k, v in tpl_vars.items():
                steps_str = steps_str.replace(f'{{{{{k}}}}}', str(v))
            try:
                raw_steps = json.loads(steps_str)
            except Exception:
                pass
        return await exec_phone_workspace_fn({'op': 'batch', 'steps': raw_steps})
    return {'ok': False, 'error': f'未知 phone macro action: {action}'}


async def finalize_phone_vision_and_diff(
    res: dict,
    node_st: Any,
    orig_requested_op: str,
    op: str,
    args: dict,
    pkg: str,
    node_count: int | None,
    cur_hash_now: str,
    eff_disp_id: int,
    tapped_xy: tuple[int, int] | None,
    before_tree_hash: str,
    before_pkg_state: str,
    before_nodes_state: int | None,
    scroll_dir: str,
    workspace_root: Path,
    raw_phone_rpc_fn: Callable[[str, dict], Awaitable[dict]],
    prepare_chat_screenshot_fn: Callable[..., str],
) -> dict:
    """Attach isolated per-node state, perceptual frame_hash, truthful diff/ui_changed, and clean 0..1000 vision frame."""
    w = int(res.get('width') or node_st.last_size[0] or 1080)
    h = int(res.get('height') or node_st.last_size[1] or 2400)
    if w > 100 and h > 100:
        node_st.last_size = (w, h)
    if pkg:
        node_st.last_pkg = pkg
    if node_count is not None:
        node_st.last_node_count = int(node_count)
        res['node_count'] = int(node_count)

    cached_nodes = parse_phone_nodes(res, node_st.last_size)
    if cached_nodes:
        node_st.last_nodes = cached_nodes
    elif int(node_count or 0) == 0:
        node_st.last_nodes = []
    raw_nodes_list = res.pop('nodes', None)
    if 'clickable_count' not in res:
        src_nodes = raw_nodes_list if isinstance(raw_nodes_list, list) else cached_nodes
        res['clickable_count'] = sum(1 for n in src_nodes if isinstance(n, dict) and n.get('clickable'))

    node_st.last_tree_hash = cur_hash_now
    enrich_phone_frame_contract(res, node_st, eff_disp_id, node_count)
    if tapped_xy is not None:
        node_st.last_tap = tapped_xy

    inc_img_arg = args.get('include_image')
    img_mode = str(args.get('image_mode') or '').strip().lower()
    gui_vision_ops = (
        'launch', 'open_uri', 'ui_tree', 'tap', 'tap_node', 'click_node',
        'tap_and_type', 'swipe', 'text', 'type_node', 'key', 'long_press',
        'capture', 'screenshot', 'send_screenshot',
    )
    if bool(args.get('_skip_vision')) or inc_img_arg is False or img_mode == 'none' or res.get('error') or res.get('ok') is False:
        want_image = False
    elif op in gui_vision_ops or bool(args.get('send_to_chat')) or inc_img_arg is True or img_mode in ('thumb', 'full'):
        want_image = True
        if not img_mode:
            img_mode = 'thumb'
    else:
        want_image = False

    b64_img = ''
    raw_jpg = b''
    if not want_image:
        res.pop('image', None)
        res.pop('_image_b64', None)
        res.pop('_multimodal_b64', None)
    elif res.get('image'):
        b64_img = str(res.pop('image')).split(',')[-1]
        raw_jpg = base64.b64decode(b64_img)
        res['saved_screenshot'] = 'phone-latest.jpg'
    else:
        snap = await raw_phone_rpc_fn('preview', {'op': 'preview', 'since': -1})
        if isinstance(snap, dict) and snap.get('image'):
            b64_img = str(snap['image']).split(',')[-1]
            w = int(snap.get('width') or w)
            h = int(snap.get('height') or h)
            if w > 100 and h > 100:
                node_st.last_size = (w, h)
            raw_jpg = base64.b64decode(b64_img)
            res['saved_screenshot'] = 'phone-latest.jpg'

    visual_changed = bool(res.get('visual_changed', False))
    if raw_jpg:
        prev_jpg = getattr(node_st, 'prev_raw_jpg', None)
        diff_pct = compute_frame_diff_pct(prev_jpg, raw_jpg)
        node_st.prev_raw_jpg = raw_jpg
        res['frame_hash'] = compute_perceptual_frame_hash(raw_jpg)
        res['frame_diff_pct'] = diff_pct
        if prev_jpg is not None:
            visual_changed = bool(diff_pct >= 0.35)
    elif cur_hash_now:
        res['frame_hash'] = cur_hash_now

    if 'change_summary' in res or op in ('launch', 'open_uri', 'tap', 'tap_node', 'click_node', 'tap_and_type', 'swipe', 'text', 'type_node', 'key', 'long_press'):
        raw_cs = str(res.get('change_summary') or '')
        m_bp = re.search(r'包名\s+([^\s]+)\s+->', raw_cs)
        m_bn = re.search(r'节点数\s+([^\s]+)\s+->', raw_cs)
        b_pkg = m_bp.group(1) if m_bp else (before_pkg_state or 'none')
        b_cnt = m_bn.group(1) if m_bn else (str(before_nodes_state) if before_nodes_state is not None else '?')
        final_pkg_str = pkg or 'unknown'
        final_cnt_int = int(node_count or 0)
        tree_changed = bool(
            (before_tree_hash != cur_hash_now and (before_tree_hash or cur_hash_now))
            or (b_pkg not in ('', 'none', final_pkg_str))
            or (str(b_cnt) not in ('?', str(final_cnt_int)))
        )
        ui_changed_final = tree_changed or visual_changed
        res['ui_changed'] = ui_changed_final
        res['change_summary'] = (
            f"{'✅ 界面已变化: ' if ui_changed_final else '⚠️ 界面未变: '}"
            f"包名 {b_pkg or 'none'} -> {final_pkg_str}, 节点数 {b_cnt} -> {final_cnt_int}"
            + (' (自绘画面帧已更新)' if (visual_changed and not tree_changed) else '')
        )

    if want_image and raw_jpg:
        (workspace_root / 'phone-clean.jpg').write_bytes(raw_jpg)
        if args.get('send_to_chat'):
            box_xyxy, _ = validate_norm_box_xyxy(args)
            res['chat_image_url'] = prepare_chat_screenshot_fn(raw_jpg, crop_box=box_xyxy, prefix='phone-shot')
        work_jpg = annotate_tap_crosshair(raw_jpg, tapped_xy[0], tapped_xy[1], w, h) if tapped_xy is not None else raw_jpg
        annotated_jpg, _, _ = draw_vision_ruler_overlay(work_jpg, a11y_summary='', screen_w=w, screen_h=h, pad_px=0, draw_badges=False)
        (workspace_root / 'phone-latest.jpg').write_bytes(annotated_jpg)
        if img_mode != 'full' and not args.get('send_to_chat'):
            try:
                from PIL import Image as _Image
                im = _Image.open(io.BytesIO(annotated_jpg)).convert('RGB')
                iw, ih = im.size
                if max(iw, ih) > 1080:
                    scale = 1080.0 / float(max(iw, ih))
                    im = im.resize((max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))), _Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format='JPEG', quality=88)
                annotated_jpg = buf.getvalue()
            except Exception:
                pass
        b64_img = base64.b64encode(annotated_jpg).decode('ascii')

    if want_image and b64_img:
        change_line = f"{res.get('change_summary')}\n" if res.get('change_summary') else ''
        res['_multimodal_b64'] = b64_img
        res['_multimodal_mime'] = 'image/jpeg'
        res['_multimodal_caption'] = (
            f'【手机虚拟屏实时画面 (op={orig_requested_op}, 包名={pkg or "unknown"}, 节点数={int(node_count or 0)})】\n'
            f'{change_line}'
            '坐标系已零偏移对齐画面四边 0..1000（左上 0,0 至右下 1000,1000），直接读取边缘刻度传 nx, ny (0..1000) 调用 phone(op="tap"|"type")；小字用 zoom(box=[nx1,ny1,nx2,ny2]) 放大。'
        )
        res['vision_direct'] = True
        res['image_mode'] = img_mode or 'thumb'
    if tapped_xy is not None:
        tx, ty = tapped_xy
        nx_t = int(round(tx * 1000.0 / max(1, w)))
        ny_t = int(round(ty * 1000.0 / max(1, h)))
        res['tap_calibration'] = f'🎯 已点击 TAP({nx_t},{ny_t})'
    if scroll_dir:
        res['direction'] = scroll_dir
    if orig_requested_op:
        if res.get('op') and res.get('op') != orig_requested_op:
            res['op_internal'] = res['op']
        res['op'] = orig_requested_op
    return res
