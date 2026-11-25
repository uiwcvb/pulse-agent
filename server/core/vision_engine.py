"""Multimodal Vision & Screen Grounding Engine.

All models in this system are treated as native multimodal vision models.
Never guess "text model vs vision model" and never forward images to OCR or secondary vision LLMs.
Instead:
1. Draw clean outer-edge calibrated 0..1000 normalized coordinate rulers via PIL (~15ms), with zero interior badge clutter by default.
2. Pass images directly to the active model via standard OpenAI/FastAPI `image_url` multimodal content blocks.
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import io
import re
from pathlib import Path


def detect_image_mime(raw_bytes: bytes | None, fallback: str = 'image/jpeg') -> str:
    """Detect real image MIME strictly from magic bytes (never trust file extension)."""
    if not raw_bytes:
        return fallback
    b = bytes(raw_bytes[:16])
    if b.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if b.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if b.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if len(b) >= 12 and b[:4] == b'RIFF' and b[8:12] == b'WEBP':
        return 'image/webp'
    if b.startswith(b'BM'):
        return 'image/bmp'
    return fallback


def annotate_tap_crosshair(img_bytes: bytes, tap_x: int, tap_y: int, width: int, height: int) -> bytes:
    """Draw a high-contrast red/white crosshair at (tap_x, tap_y) on the screenshot."""
    try:
        from PIL import Image, ImageDraw
        im = Image.open(io.BytesIO(img_bytes)).convert('RGB')
        iw, ih = im.size
        sx = int(round(tap_x * iw / max(1, width)))
        sy = int(round(tap_y * ih / max(1, height)))
        draw = ImageDraw.Draw(im)
        r_outer = max(14, min(iw, ih) // 28)
        r_inner = max(5, r_outer // 3)
        line_len = int(r_outer * 1.65)
        for w_stroke, col in ((5, (255, 255, 255)), (3, (255, 30, 30))):
            draw.ellipse((sx - r_outer, sy - r_outer, sx + r_outer, sy + r_outer), outline=col, width=w_stroke)
            draw.ellipse((sx - r_inner, sy - r_inner, sx + r_inner, sy + r_inner), outline=col, width=w_stroke)
            draw.line((sx - line_len, sy, sx + line_len, sy), fill=col, width=w_stroke)
            draw.line((sx, sy - line_len, sx, sy + line_len), fill=col, width=w_stroke)
        nx = int(round(tap_x * 1000.0 / max(1, width)))
        ny = int(round(tap_y * 1000.0 / max(1, height)))
        tag = f'TAP({nx},{ny})'
        tx = min(max(6, sx + r_outer + 6), max(6, iw - 130))
        ty = max(6, sy - r_outer - 22)
        draw.rectangle((tx - 3, ty - 2, tx + len(tag) * 7 + 6, ty + 15), fill=(220, 20, 20), outline=(255, 255, 255), width=1)
        draw.text((tx, ty), tag, fill=(255, 255, 255))
        buf = io.BytesIO()
        im.save(buf, format='JPEG', quality=88)
        return buf.getvalue()
    except Exception:
        return img_bytes


def _draw_outer_edge_ticks_rgba(
    canvas,
    x_ticks: list[tuple[int, str]],
    y_ticks: list[tuple[int, str]],
):
    """Draw light semi-transparent normalized coordinate ticks strictly within outer 14px edge."""
    from PIL import Image, ImageDraw

    cw, ch = canvas.size
    base = canvas.convert('RGBA')
    overlay = Image.new('RGBA', (cw, ch), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    # Subtle 200-step 0..1000 hairline grid so VLM reads coordinates accurately at a glance without math
    for px, lbl in x_ticks:
        if lbl in ('200', '400', '600', '800'):
            px_g = max(0, min(cw - 1, int(px)))
            for yy in range(16, max(16, ch - 16), 14):
                odraw.line((px_g, yy, px_g, min(ch - 16, yy + 4)), fill=(110, 225, 255, 34), width=1)
    for py, lbl in y_ticks:
        if lbl in ('200', '400', '600', '800'):
            py_g = max(0, min(ch - 1, int(py)))
            for xx in range(16, max(16, cw - 16), 14):
                odraw.line((xx, py_g, min(cw - 16, xx + 4), py_g), fill=(110, 225, 255, 34), width=1)

    for px, lbl in x_ticks:
        px_c = max(0, min(cw - 1, int(px)))
        tw = max(10, len(lbl) * 6 + 6)
        bx1 = max(0, min(cw - tw, px_c - tw // 2))
        odraw.line((px_c, 0, px_c, 4), fill=(110, 225, 255, 190), width=1)
        odraw.line((px_c, max(0, ch - 5), px_c, ch - 1), fill=(110, 225, 255, 190), width=1)
        odraw.rectangle((bx1, 0, min(cw - 1, bx1 + tw), 13), fill=(15, 20, 28, 130))
        odraw.text((bx1 + 3, 1), lbl, fill=(255, 232, 130, 210))
        odraw.rectangle((bx1, max(0, ch - 14), min(cw - 1, bx1 + tw), ch - 1), fill=(15, 20, 28, 130))
        odraw.text((bx1 + 3, max(0, ch - 13)), lbl, fill=(255, 232, 130, 210))

    for py, lbl in y_ticks:
        py_c = max(0, min(ch - 1, int(py)))
        odraw.line((0, py_c, 4, py_c), fill=(110, 225, 255, 190), width=1)
        odraw.line((max(0, cw - 5), py_c, cw - 1, py_c), fill=(110, 225, 255, 190), width=1)
        th = max(14, len(lbl) * 6 + 6)
        tile = Image.new('RGBA', (th, 13), (15, 20, 28, 130))
        ImageDraw.Draw(tile).text((3, 1), lbl, fill=(145, 240, 255, 210))
        rot = tile.rotate(90, expand=True)
        by1 = max(14, min(max(14, ch - 14 - rot.height), py_c - rot.height // 2))
        overlay.alpha_composite(rot, (0, by1))
        overlay.alpha_composite(rot, (max(0, cw - 13), by1))

    return Image.alpha_composite(base, overlay).convert('RGB')


def draw_vision_ruler_overlay(
    img_bytes: bytes,
    a11y_summary: str = '',
    screen_w: int = 0,
    screen_h: int = 0,
    pad_px: int = 0,
    draw_badges: bool = False,
) -> tuple[bytes, int, int]:
    """
    Draws clean calibrated 0..1000 coordinate rulers with zero interior badge clutter by default:
    - When `pad_px == 0` (default): subtle semi-transparent 0..1000 ticks strictly inside outer 14px edge.
    - When `pad_px > 0`: outer border (e.g. 28px) with 0..1000 ruler labels around the frame.
    - `draw_badges` defaults to False so UI content is never occluded by @eN/@bN boxes.
    Returns (annotated_jpeg_bytes, inner_img_w, inner_img_h).
    """
    iw, ih = 0, 0
    try:
        from PIL import Image, ImageDraw
        im = Image.open(io.BytesIO(img_bytes)).convert('RGB')
        iw, ih = im.size
        pad = max(0, int(pad_px))
        if pad > 0:
            canvas = Image.new('RGB', (iw + pad * 2, ih + pad * 2), (13, 17, 24))
            canvas.paste(im, (pad, pad))
            draw = ImageDraw.Draw(canvas)
            draw.rectangle((pad - 1, pad - 1, pad + iw, pad + ih), outline=(38, 120, 155), width=1)
            for step in range(100, 1000, 100):
                y = pad + int(round(step * ih / 1000.0))
                x = pad + int(round(step * iw / 1000.0))
                if pad >= 20:
                    ylbl = f'{step}'
                    draw.line((pad - 5, y, pad - 1, y), fill=(0, 215, 255), width=1)
                    draw.text((2, max(pad, min(pad + ih - 11, y - 5))), ylbl, fill=(130, 240, 255))
                    draw.line((pad + iw, y, pad + iw + 4, y), fill=(0, 215, 255), width=1)
                    draw.text((pad + iw + 6, max(pad, min(pad + ih - 11, y - 5))), ylbl, fill=(130, 240, 255))
                    xlbl = f'X{step}' if step in (200, 500, 800) else str(step)
                    tw = len(xlbl) * 6
                    draw.line((x, pad - 5, x, pad - 1), fill=(255, 225, 95), width=1)
                    draw.text((max(pad, min(pad + iw - tw, x - tw // 2)), 7), xlbl, fill=(255, 230, 110))
                    draw.line((x, pad + ih, x, pad + ih + 4), fill=(255, 225, 95), width=1)
                    draw.text((max(pad, min(pad + iw - tw, x - tw // 2)), pad + ih + 8), xlbl, fill=(255, 230, 110))
        else:
            x_ticks = [(int(round(step * iw / 1000.0)), str(step)) for step in (0, 200, 400, 600, 800, 1000)]
            y_ticks = [(int(round(step * ih / 1000.0)), str(step)) for step in (0, 200, 400, 600, 800, 1000)]
            canvas = _draw_outer_edge_ticks_rgba(im, x_ticks, y_ticks)
            draw = ImageDraw.Draw(canvas)

        if draw_badges and a11y_summary and screen_w > 0 and screen_h > 0:
            for m_e in re.finditer(r'(@[eb]\d+)\s+\[[^\]]+\].*?center=\((\d+),(\d+)\)', a11y_summary):
                eref = m_e.group(1)
                ecx, ecy = int(m_e.group(2)), int(m_e.group(3))
                px = pad + int(round(ecx * iw / max(1, screen_w)))
                py = pad + int(round(ecy * ih / max(1, screen_h)))
                draw.ellipse((px - 5, py - 5, px + 5, py + 5), fill=(255, 180, 0), outline=(0, 0, 0), width=1)
                bx = min(max(pad + 4, px + 6), max(pad + 4, pad + iw - len(eref) * 6 - 10))
                by = min(max(pad + 2, py - 8), max(pad + 2, pad + ih - 15))
                draw.rectangle((bx - 2, by - 1, bx + len(eref) * 6 + 4, by + 12), fill=(120, 30, 180), outline=(255, 220, 80), width=1)
                draw.text((bx, by), eref, fill=(255, 255, 255))

        buf = io.BytesIO()
        canvas.save(buf, format='JPEG', quality=88)
        return buf.getvalue(), iw, ih
    except Exception:
        return img_bytes, iw, ih


def compute_perceptual_frame_hash(raw_bytes: bytes | None) -> str:
    """Compute an 8x8 dHash + mean-luma bucket perceptual hash resilient to clock/caret blink."""
    if not raw_bytes:
        return ''
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw_bytes)).convert('L')
        iw, ih = im.size
        # Mask out tiny bottom-right clock/status corner on desktop-sized frames so minute ticks don't churn hashes
        if iw >= 400 and ih >= 300:
            small = im.resize((16, 16), Image.Resampling.BILINEAR)
            s_px = small.load()
            fill_v = s_px[12, 14]
            for yy in (14, 15):
                for xx in (13, 14, 15):
                    s_px[xx, yy] = fill_v
            grid = small.resize((9, 8), Image.Resampling.BILINEAR)
        else:
            grid = im.resize((9, 8), Image.Resampling.BILINEAR)
        px = grid.load()
        bits = 0
        total_luma = 0
        for yy in range(8):
            for xx in range(8):
                left = px[xx, yy]
                right = px[xx + 1, yy]
                bits = (bits << 1) | (1 if left > right else 0)
                total_luma += left
        mean_bucket = min(15, max(0, (total_luma // 64) // 16))
        return f'{bits:016x}:{mean_bucket:x}'
    except Exception:
        return hashlib.md5(raw_bytes).hexdigest()[:16]


class VisionEngine:
    """Direct multimodal passthrough engine (no secondary LLM or OCR calls)."""

    def __init__(self, store=None, llm_or_get_assistant=None, fetch_models_fn=None):
        self.store = store
        self.last_vision_nodes: list[dict] = []

    @staticmethod
    def is_vision_capable_model(model_id: str = '') -> bool:
        """All models configured in this system are multimodal vision models."""
        return True

    async def read_image_file_with_vision(self, file_path: Path, prompt: str = '') -> dict:
        if not file_path.exists():
            return {'ok': False, 'error': f'图片文件不存在: {file_path.name}'}

        def _load_image_meta() -> tuple[bytes, int, int, str, str]:
            raw = file_path.read_bytes()
            if not raw:
                return b'', 0, 0, '', ''
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as im:
                iw, ih = im.size
                img_fmt = str(im.format or file_path.suffix.lstrip('.').upper() or 'IMAGE')
                img_mode = str(im.mode or 'RGB')
            return raw, iw, ih, img_fmt, img_mode

        try:
            raw_bytes, iw, ih, img_fmt, img_mode = await asyncio.to_thread(_load_image_meta)
        except Exception as exc:
            return {'ok': False, 'error': f'读取或解析图片文件失败 ({file_path.name}): {exc}'}
        if not raw_bytes:
            return {'ok': False, 'error': f'图片文件为空 (0 bytes): {file_path.name}'}

        sha16 = hashlib.sha256(raw_bytes).hexdigest()[:16]
        ext = file_path.suffix.lower()
        ext_fallback = {
            '.png': 'image/png',
            '.webp': 'image/webp',
            '.gif': 'image/gif',
            '.bmp': 'image/bmp',
        }.get(ext, 'image/jpeg')
        mime = detect_image_mime(raw_bytes, fallback=ext_fallback)
        b64 = base64.b64encode(raw_bytes).decode('ascii')
        if not b64:
            return {'ok': False, 'error': f'图片 Base64 编码失败 ({file_path.name})'}
        cap_parts = [f'【图片文件 `{file_path.name}` ({iw}x{ih} {img_fmt}, sha256={sha16}) 实时画面】']
        if prompt:
            cap_parts.append(f'（提示：{prompt}）')
        return {
            'ok': True,
            'image_file': file_path.name,
            'size_bytes': len(raw_bytes),
            'width': iw,
            'height': ih,
            'format': img_fmt,
            'mode': img_mode,
            'sha256': sha16,
            'multimodal_b64_len': len(b64),
            '_keep_in_turn': False,
            '_multimodal_b64': b64,
            '_multimodal_mime': mime,
            '_multimodal_caption': ''.join(cap_parts),
        }

    @staticmethod
    def build_user_multimodal_content(raw_text: str, resolve_path_fn) -> str | list[dict]:
        """Attach uploaded images directly as standard OpenAI `image_url` blocks without secondary OCR."""
        matches = list(dict.fromkeys(re.findall(
            r'((?:uploads/|[A-Za-z0-9_.-]+\.)[^\s\n"\'<>]+?\.(?:png|jpg|jpeg|webp|gif|bmp))',
            raw_text,
            flags=re.IGNORECASE,
        )))
        if not matches:
            return raw_text
        valid_files: list[tuple[str, str, str]] = []
        for rel in matches[:4]:
            clean_rel = rel.removeprefix('workspace/').strip()
            try:
                p = resolve_path_fn(clean_rel)
                if p.exists() and p.is_file() and p.stat().st_size <= 10_000_000:
                    raw_file_bytes = p.read_bytes()
                    ext = p.suffix.lower()
                    ext_fallback = {'.png': 'image/png', '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp'}.get(ext, 'image/jpeg')
                    mime = detect_image_mime(raw_file_bytes, fallback=ext_fallback)
                    b64 = base64.b64encode(raw_file_bytes).decode('ascii')
                    valid_files.append((clean_rel, mime, b64))
            except Exception:
                continue
        if not valid_files:
            return raw_text
        parts: list[dict] = [{'type': 'text', 'text': raw_text}]
        for _, mime, b64 in valid_files:
            parts.append({'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}})
        return parts


def compute_frame_diff_pct(prev_bytes: bytes | None, curr_bytes: bytes | None) -> float:
    """Estimate changed pixel percentage [0..100] between two screenshots using Pillow .load()."""
    if not curr_bytes:
        return 0.0
    if not prev_bytes:
        return 100.0
    try:
        from PIL import Image
        im1 = Image.open(io.BytesIO(prev_bytes)).convert('RGB').resize((96, 96))
        im2 = Image.open(io.BytesIO(curr_bytes)).convert('RGB').resize((96, 96))
        p1 = im1.load()
        p2 = im2.load()
        changed = 0
        total = 96 * 96
        for yy in range(96):
            for xx in range(96):
                a, b = p1[xx, yy], p2[xx, yy]
                if abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2]) > 24:
                    changed += 1
        return round((changed * 100.0) / total, 2)
    except Exception:
        return 100.0


def crop_zoom_region(
    raw_bytes: bytes,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    norm_1000: bool = True,
) -> tuple[bytes, dict]:
    """Crop region from clean image, upscale cleanly, and overlay TRUE global 0..1000 edge ticks."""
    from PIL import Image
    im = Image.open(io.BytesIO(raw_bytes)).convert('RGB')
    iw, ih = im.size
    use_norm = bool(norm_1000 and max(abs(x1), abs(y1), abs(x2), abs(y2)) <= 1000.0)
    if use_norm:
        px1 = int(round(x1 * iw / 1000.0))
        py1 = int(round(y1 * ih / 1000.0))
        px2 = int(round(x2 * iw / 1000.0))
        py2 = int(round(y2 * ih / 1000.0))
    else:
        px1, py1, px2, py2 = int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))
    lx, rx = sorted((max(0, min(iw, px1)), max(0, min(iw, px2))))
    ty, by = sorted((max(0, min(ih, py1)), max(0, min(ih, py2))))
    if rx - lx < 64:
        cx = (lx + rx) // 2
        lx = max(0, min(iw - min(iw, 64), cx - 32))
        rx = min(iw, lx + 64)
    if by - ty < 64:
        cy = (ty + by) // 2
        ty = max(0, min(ih - min(ih, 64), cy - 32))
        by = min(ih, ty + 64)
    cropped = im.crop((lx, ty, max(lx + 1, rx), max(ty + 1, by)))
    cw, ch = cropped.size
    scale = min(2.5, max(1.0, 1280.0 / max(1, cw, ch)))
    scale = max(scale, 2.0) if max(cw, ch) <= 720 else scale
    if scale > 1.01:
        cropped = cropped.resize((max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))), Image.Resampling.LANCZOS)
    zw, zh = cropped.size
    nx1_g = lx * 1000.0 / max(1, iw)
    ny1_g = ty * 1000.0 / max(1, ih)
    nx2_g = rx * 1000.0 / max(1, iw)
    ny2_g = by * 1000.0 / max(1, ih)
    x_ticks = [(int(round(f * zw)), str(int(round(nx1_g + f * (nx2_g - nx1_g))))) for f in (0.0, 0.25, 0.5, 0.75, 1.0)]
    y_ticks = [(int(round(f * zh)), str(int(round(ny1_g + f * (ny2_g - ny1_g))))) for f in (0.0, 0.25, 0.5, 0.75, 1.0)]
    annotated_im = _draw_outer_edge_ticks_rgba(cropped, x_ticks, y_ticks)
    out_buf = io.BytesIO()
    annotated_im.save(out_buf, format='JPEG', quality=90)
    return out_buf.getvalue(), {
        'crop_px': [lx, ty, rx, by],
        'norm_box': [int(round(nx1_g)), int(round(ny1_g)), int(round(nx2_g)), int(round(ny2_g))],
        'scale': round(float(scale), 2),
        'orig_size': [iw, ih],
        'zoom_size': [zw, zh],
    }


def _coerce_box_4(box) -> list[float] | None:
    if isinstance(box, dict):
        if all(k in box for k in ('x1', 'y1', 'x2', 'y2')):
            box = [box['x1'], box['y1'], box['x2'], box['y2']]
        elif all(k in box for k in ('nx1', 'ny1', 'nx2', 'ny2')):
            box = [box['nx1'], box['ny1'], box['nx2'], box['ny2']]
        elif all(k in box for k in ('left', 'top', 'right', 'bottom')):
            box = [box['left'], box['top'], box['right'], box['bottom']]
    elif isinstance(box, str) and box.strip():
        import json
        s = box.strip()
        try:
            box = json.loads(s) if s.startswith('[') else [float(x) for x in re.split(r'[\s,]+', s) if x]
        except Exception:
            return None
    if isinstance(box, (list, tuple)) and len(box) == 4:
        try:
            return [float(box[0]), float(box[1]), float(box[2]), float(box[3])]
        except (TypeError, ValueError):
            return None
    return None


def zoom_region_from_args(raw_bytes: bytes, args: dict) -> tuple[bytes, dict]:
    """Parse flexible bbox/center arguments (`box`, `zoom_box`, `x1/y1/x2/y2`, `x/y/radius`) and zoom."""
    raw_box = None
    for key in ('box', 'zoom_box', 'crop_box', 'region'):
        if args.get(key) is not None:
            raw_box = args.get(key)
            break
    parsed_box = _coerce_box_4(raw_box)
    radius = float(args.get('radius') or args.get('r') or 140)
    if parsed_box is not None:
        x1, y1, x2, y2 = parsed_box
    elif args.get('x1') is not None and args.get('y1') is not None and args.get('x2') is not None and args.get('y2') is not None:
        x1, y1, x2, y2 = float(args['x1']), float(args['y1']), float(args['x2']), float(args['y2'])
    else:
        cx = float(args.get('nx') if args.get('nx') is not None else (args.get('x') if args.get('x') is not None else 500))
        cy = float(args.get('ny') if args.get('ny') is not None else (args.get('y') if args.get('y') is not None else 500))
        x1, y1, x2, y2 = max(0.0, cx - radius), max(0.0, cy - radius), min(1000.0, cx + radius), min(1000.0, cy + radius)
    norm_flag = bool(args.get('norm_1000', True))
    if args.get('nx') is None and args.get('x') is not None and float(args['x']) > 1000:
        norm_flag = False
    return crop_zoom_region(raw_bytes, x1, y1, x2, y2, norm_1000=norm_flag)


def enrich_cloud_desktop_vision(
    res: dict,
    op: str,
    workspace_root: Path,
    state_box: dict,
    tap_xy: tuple[int, int] | None = None,
) -> dict:
    """Save clean shot, attach zero-offset Q88 JPEG, UI diff metric, frame_hash, and window list."""
    if not isinstance(res, dict):
        return res
    b64_raw = str(res.get('image') or '').strip()
    clean_res = {k: v for k, v in res.items() if k not in ('image', 'frame_b64', 'screenshot')}
    if isinstance(clean_res.get('dom_elements'), list):
        clean_res['dom_elements'] = [
            {k: v for k, v in el.items() if k not in ('x', 'y')} if isinstance(el, dict) else el
            for el in clean_res['dom_elements']
        ]
    raw_wins = list(res.get('x11_windows') or res.get('windows') or [])
    dedup_wins: list[dict] = []
    seen_win_keys: dict[tuple[str, str], int] = {}
    for w_it in raw_wins:
        if not isinstance(w_it, dict):
            continue
        w_copy = dict(w_it)
        w_key = (str(w_copy.get('title') or '').strip().lower(), str(w_copy.get('wm_class') or '').strip().lower())
        if not w_copy.get('focused') and w_key[0] and w_key in seen_win_keys:
            idx = seen_win_keys[w_key]
            dedup_wins[idx]['duplicate_count'] = int(dedup_wins[idx].get('duplicate_count') or 1) + 1
            continue
        if not w_copy.get('focused') and w_key[0]:
            seen_win_keys[w_key] = len(dedup_wins)
        dedup_wins.append(w_copy)
        if len(dedup_wins) >= 16:
            break
    clean_res['x11_windows'] = dedup_wins
    clean_res.pop('windows', None)
    if not b64_raw:
        return clean_res
    try:
        raw_bytes = base64.b64decode(b64_raw.split(',')[-1])
        clean_res['frame_hash'] = compute_perceptual_frame_hash(raw_bytes)
        uploads_dir = workspace_root / 'uploads'
        try:
            (workspace_root / 'desktop-clean.jpg').write_bytes(raw_bytes)
            uploads_dir.mkdir(parents=True, exist_ok=True)
            (uploads_dir / 'desktop-clean.jpg').write_bytes(raw_bytes)
        except Exception:
            pass
        prev_bytes = state_box.get('prev_raw')
        diff_pct = compute_frame_diff_pct(prev_bytes, raw_bytes)
        state_box['prev_raw'] = raw_bytes
        clean_res['frame_diff_pct'] = diff_pct
        clean_res['ui_changed'] = True if prev_bytes is None else bool(diff_pct >= 0.35)
        w = int(res.get('width') or 1280)
        h = int(res.get('height') or 800)
        work_bytes = annotate_tap_crosshair(raw_bytes, tap_xy[0], tap_xy[1], w, h) if tap_xy else raw_bytes
        annotated_bytes, _, _ = draw_vision_ruler_overlay(
            work_bytes,
            a11y_summary='',
            screen_w=w,
            screen_h=h,
            pad_px=0,
            draw_badges=False,
        )
        try:
            (workspace_root / 'desktop-latest.jpg').write_bytes(annotated_bytes)
            uploads_dir.mkdir(parents=True, exist_ok=True)
            (uploads_dir / 'desktop-latest.jpg').write_bytes(annotated_bytes)
        except Exception:
            pass
        clean_res['_multimodal_b64'] = base64.b64encode(annotated_bytes).decode('ascii')
        clean_res['_multimodal_mime'] = 'image/jpeg'
        clean_res['saved_to'] = 'desktop-latest.jpg'
    except Exception:
        pass
    return clean_res


def prune_historical_images_batch(
    messages: list[dict],
    max_images: int = 8,
    evict_batch: int = 4,
) -> int:
    """Evict oldest historical `image_url` blocks in batches (`batch=4` when > `max_images=8`) to preserve KV-cache prefix."""
    if not isinstance(messages, list) or len(messages) < 2:
        return 0
    positions: list[tuple[int, int]] = []
    for mi in range(len(messages) - 1):
        msg = messages[mi]
        content = msg.get('content') if isinstance(msg, dict) else None
        if isinstance(content, list):
            for bi, blk in enumerate(content):
                if isinstance(blk, dict) and blk.get('type') == 'image_url':
                    positions.append((mi, bi))
    total = len(positions)
    if total <= max_images:
        return 0
    to_drop_count = min(max(1, evict_batch), total)
    drop_by_msg: dict[int, set[int]] = {}
    for mi, bi in positions[:to_drop_count]:
        drop_by_msg.setdefault(mi, set()).add(bi)
    for mi, drop_indices in drop_by_msg.items():
        msg = messages[mi]
        new_blocks = [
            {'type': 'text', 'text': '[历史步骤截图已省略]'} if bi in drop_indices else blk
            for bi, blk in enumerate(msg['content'])
        ]
        messages[mi] = {**msg, 'content': new_blocks}
    return to_drop_count

