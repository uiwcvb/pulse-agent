"""
Extension Registry (`server/transport/extension_registry.py`).

Allows internal modules and third-party contributors on GitHub to register:
- `ToolPlugin` (custom agent tools exposed to the LLM)
- `SearchPlugin` (custom search providers)
- `MemoryHook` (custom pre/post memory consolidation hooks)
- `StickerPackPlugin` (optional runtime sticker packs in `workspace/plugins/stickers/<pack_id>/manifest.json`, kept outside Git)
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.interfaces import MemoryHook, SearchPlugin, ToolPlugin


class ExtensionRegistry:
    def __init__(self) -> None:
        self.tool_plugins: Dict[str, ToolPlugin] = {}
        self.search_plugins: Dict[str, SearchPlugin] = {}
        self.memory_hooks: List[MemoryHook] = []
        self.sticker_packs: Dict[str, Dict[str, Any]] = {}
        self._plugins_dir: Path | None = None

    @staticmethod
    def _safe_openai_tool_name(raw_name: str) -> str:
        import hashlib
        import re
        s = (raw_name or '').strip()
        if re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', s):
            return s
        ascii_part = re.sub(r'[^a-zA-Z0-9_-]', '', s)[:32].strip('_-')
        digest = hashlib.md5(s.encode('utf-8')).hexdigest()[:8]
        return f'custom_{ascii_part}_{digest}' if ascii_part else f'custom_tool_{digest}'

    def register_tool(self, plugin: ToolPlugin) -> None:
        name = getattr(plugin, 'name', '').strip()
        if name:
            safe_name = self._safe_openai_tool_name(name)
            self.tool_plugins[safe_name] = plugin
            if safe_name != name:
                setattr(plugin, '_display_name', name)
                setattr(plugin, 'name', safe_name)

    def register_search(self, plugin: SearchPlugin) -> None:
        name = getattr(plugin, 'name', '').strip()
        if name:
            self.search_plugins[name] = plugin

    def register_memory_hook(self, hook: MemoryHook) -> None:
        self.memory_hooks.append(hook)

    def register_sticker_pack(self, pack_id: str, name: str, stickers: Dict[str, Dict[str, str]], enabled: bool = True, manifest_path: Path | None = None) -> None:
        pid = (pack_id or '').strip()
        if pid and isinstance(stickers, dict):
            self.sticker_packs[pid] = {
                'id': pid,
                'name': name or pid,
                'enabled': bool(enabled),
                'stickers': stickers,
                'manifest_path': str(manifest_path) if manifest_path else '',
            }

    def set_sticker_pack_enabled(self, pack_id: str, enabled: bool) -> bool:
        pack = self.sticker_packs.get(pack_id)
        if not pack:
            return False
        pack['enabled'] = bool(enabled)
        mp = pack.get('manifest_path')
        if mp:
            p = Path(mp)
            if p.is_file():
                try:
                    data = json.loads(p.read_text(encoding='utf-8'))
                    data['enabled'] = bool(enabled)
                    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                except Exception:
                    pass
        return True

    def list_active_stickers(self) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        for pid, pack in self.sticker_packs.items():
            if not pack.get('enabled', True):
                continue
            stickers = pack.get('stickers') or {}
            if not isinstance(stickers, dict):
                continue
            for sk, sinfo in stickers.items():
                info = sinfo if isinstance(sinfo, dict) else {}
                fname = str(info.get('file') or f'{sk}.png').strip()
                lbl = str(info.get('label') or sk).strip()
                items.append({
                    'pack_id': pid,
                    'code': str(sk),
                    'label': lbl,
                    'file': fname,
                    'url': f'/stickers/{pid}/{fname}',
                })
        return items

    def resolve_sticker_info(self, code: str) -> Optional[Dict[str, str]]:
        raw = str(code or '').strip()
        if not raw:
            return None
        # Unwrap optional shell syntax such as <sticker:wink>, [sticker: wink], [表情: 眨眼得意], [贴纸：开心比心]
        m_wrap = re.fullmatch(
            r"""(?:<sticker\s*[:：]\s*([^>\n]+?)\s*/?>|\[(?:sticker|表情|贴纸)\s*[:：]\s*([^\]\n]+?)\]|<sticker>\s*([^<\n]+?)\s*</sticker>)""",
            raw,
            flags=re.IGNORECASE,
        )
        if m_wrap:
            raw = (m_wrap.group(1) or m_wrap.group(2) or m_wrap.group(3) or '').strip()
        raw = raw.strip().strip('"\'`').strip()
        if not raw:
            return None

        norm = raw.replace('：', ':')
        target_pack: str | None = None
        if norm.lower().startswith('/stickers/'):
            rest = norm[len('/stickers/'):].lstrip('/')
            if '/' in rest:
                p_cand, r_cand = rest.split('/', 1)
                if p_cand in self.sticker_packs:
                    target_pack = p_cand
                    norm = r_cand.strip()
        elif '/' in norm or ':' in norm:
            sep = '/' if '/' in norm else ':'
            p_cand, r_cand = norm.split(sep, 1)
            p_cand_clean = p_cand.strip()
            for pid in self.sticker_packs:
                if pid.lower() == p_cand_clean.lower():
                    target_pack = pid
                    norm = r_cand.strip()
                    break

        norm_low = norm.lower().strip()
        if not norm_low:
            return None
        norm_stem = re.sub(r'\.(?:png|webp|gif|jpe?g)$', '', norm_low, flags=re.IGNORECASE)
        norm_compact = norm_low.replace(' ', '')

        candidates: list[tuple[str, str, str, str, list[str]]] = []
        for pid, pack in self.sticker_packs.items():
            if not pack.get('enabled', True):
                continue
            if target_pack is not None and pid != target_pack:
                continue
            stickers = pack.get('stickers') or {}
            if not isinstance(stickers, dict):
                continue
            for sk, sinfo in stickers.items():
                info = sinfo if isinstance(sinfo, dict) else {}
                fname = str(info.get('file') or f'{sk}.png').strip()
                lbl = str(info.get('label') or sk).strip()
                raw_aliases = info.get('aliases')
                aliases = [str(a).strip() for a in raw_aliases if str(a).strip()] if isinstance(raw_aliases, list) else []
                candidates.append((pid, str(sk), lbl, fname, aliases))

        def _make_item(c: tuple[str, str, str, str, list[str]]) -> Dict[str, str]:
            pid, sk, lbl, fname, _ = c
            return {
                'pack_id': pid,
                'code': sk,
                'label': lbl,
                'file': fname,
                'url': f'/stickers/{pid}/{fname}',
            }

        # Tier 1: exact sticker code or exact file name / stem
        for c in candidates:
            _, sk, _, fname, _ = c
            sk_low = sk.lower()
            fn_low = fname.lower()
            fn_stem = Path(fname).stem.lower()
            if norm_low in (sk_low, fn_low) or norm_stem in (sk_low, fn_stem):
                return _make_item(c)

        # Tier 2: exact label or alias (case-insensitive)
        for c in candidates:
            _, _, lbl, _, aliases = c
            lbl_low = lbl.lower()
            if norm_low == lbl_low or norm_compact == lbl_low.replace(' ', ''):
                return _make_item(c)
            for al in aliases:
                al_low = al.lower()
                if norm_low == al_low or norm_compact == al_low.replace(' ', ''):
                    return _make_item(c)

        # Tier 3: strict prefix / substring of label or aliases ONLY for queries of length >= 2
        if len(norm_low) >= 2:
            prefix_hits: list[tuple[int, tuple[str, str, str, str, list[str]]]] = []
            sub_hits: list[tuple[int, tuple[str, str, str, str, list[str]]]] = []
            for c in candidates:
                _, _, lbl, _, aliases = c
                texts = [lbl.lower(), *(a.lower() for a in aliases)]
                matched_prefix = [len(t) for t in texts if t.startswith(norm_low)]
                if matched_prefix:
                    prefix_hits.append((min(matched_prefix), c))
                    continue
                matched_sub = [len(t) for t in texts if norm_low in t]
                if matched_sub:
                    sub_hits.append((min(matched_sub), c))
            if prefix_hits:
                prefix_hits.sort(key=lambda pair: pair[0])
                return _make_item(prefix_hits[0][1])
            if sub_hits:
                sub_hits.sort(key=lambda pair: pair[0])
                return _make_item(sub_hits[0][1])

        return None

    def resolve_sticker(self, code: str) -> str:
        info = self.resolve_sticker_info(code)
        return info['url'] if info else ''

    def sticker_prompt_hint(self) -> str:
        active_items: List[str] = []
        for s in self.list_active_stickers():
            active_items.append(f"{s['code']}={s['label']}")
        if not active_items:
            return ''
        joined = ', '.join(active_items[:16])
        return (
            f'\n【表情贴纸】：日常互动、撒娇、得意或表达情绪想配一张贴纸时，直接调用原生工具 `reply(op="sticker", code="代码")`'
            f'（可用代码：{joined}；贴纸会自动附在回复旁，严禁在正文中手写 `<sticker:...>` 或 `[sticker:...]` 标签）。'
        )

    def tool_schemas(self) -> List[Dict[str, Any]]:
        schemas: List[Dict[str, Any]] = []
        seen_names: set[str] = set()
        for name, plugin in list(self.tool_plugins.items()):
            safe_name = self._safe_openai_tool_name(name)
            if safe_name in seen_names:
                continue
            seen_names.add(safe_name)
            disp = getattr(plugin, '_display_name', '')
            desc = getattr(plugin, 'description', f'Plugin tool {name}')
            if disp and disp not in desc:
                desc = f'[{disp}] {desc}'
            schemas.append({
                'type': 'function',
                'function': {
                    'name': safe_name,
                    'description': desc,
                    'parameters': getattr(plugin, 'parameters', {'type': 'object', 'properties': {}}),
                },
            })
        return schemas

    async def execute_tool(self, name: str, args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        plugin = self.tool_plugins.get(name) or self.tool_plugins.get(self._safe_openai_tool_name(name))
        if not plugin:
            return {'error': f'Unknown tool plugin: {name}'}
        return await plugin.execute(args, context)

    def discover_plugins_dir(self, plugins_dir: Path) -> List[str]:
        """Auto-load `.py` plugin modules and `stickers/*/manifest.json` inside `plugins_dir`."""
        self._plugins_dir = plugins_dir
        loaded: List[str] = []
        if not plugins_dir.is_dir():
            return loaded
        if not hasattr(self, '_loaded_mtimes'):
            self._loaded_mtimes: Dict[str, int] = {}
        for py_file in sorted(plugins_dir.glob('*.py')):
            if py_file.name.startswith('_'):
                continue
            try:
                f_key = str(py_file.resolve())
                mtime = py_file.stat().st_mtime_ns
                if self._loaded_mtimes.get(f_key) == mtime:
                    loaded.append(py_file.stem)
                    continue
                spec = importlib.util.spec_from_file_location(f'plugins.{py_file.stem}', py_file)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    reg_fn = getattr(module, 'register', None)
                    if callable(reg_fn):
                        reg_fn(self)
                        self._loaded_mtimes[f_key] = mtime
                        loaded.append(py_file.stem)
            except Exception:
                continue
        stickers_root = plugins_dir / 'stickers'
        if stickers_root.is_dir():
            for mf in sorted(stickers_root.glob('*/manifest.json')):
                try:
                    pid_candidate = mf.parent.name
                    existing_pack = self.sticker_packs.get(pid_candidate)
                    data = json.loads(mf.read_text(encoding='utf-8'))
                    pid = str(data.get('id') or pid_candidate).strip()
                    name = str(data.get('name') or pid).strip()
                    enabled = bool(existing_pack['enabled']) if existing_pack and 'enabled' in existing_pack else bool(data.get('enabled', True))
                    stickers = data.get('stickers') or {}
                    self.register_sticker_pack(pid, name, stickers, enabled=enabled, manifest_path=mf)
                    loaded.append(f'sticker:{pid}')
                except Exception:
                    continue
        return loaded

    def summary(self) -> Dict[str, Any]:
        return {
            'tool_plugins': list(self.tool_plugins.keys()),
            'search_plugins': list(self.search_plugins.keys()),
            'memory_hooks_count': len(self.memory_hooks),
            'sticker_packs': [
                {
                    'id': p['id'],
                    'name': p['name'],
                    'enabled': p['enabled'],
                    'count': len(p.get('stickers') or {}),
                    'codes': list((p.get('stickers') or {}).keys()),
                    'stickers': [
                        {
                            'code': sk,
                            'label': str((sinfo or {}).get('label') or sk),
                            'file': str((sinfo or {}).get('file') or f'{sk}.png'),
                            'url': f"/stickers/{p['id']}/{str((sinfo or {}).get('file') or f'{sk}.png')}",
                        }
                        for sk, sinfo in (p.get('stickers') or {}).items()
                        if isinstance(sinfo, dict) or sk
                    ],
                }
                for p in self.sticker_packs.values()
            ],
        }
