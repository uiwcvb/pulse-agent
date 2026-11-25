"""Silent Background Memory, Pinned Rules & 200K Immutable-Prefix Context Engine.

Follows Letta / A-MEM Zettelkasten & Immutable Prefix Caching principles:
- Preserves full 200,000-token context capacity.
- NEVER mutates already-sent messages, images, or tool outputs mid-session (100% Prefix Cache hit rate!).
- Separates static `📌 Pinned Memories` (in `messages[0]`) from dynamic `INDEX.md` / `user_traits.md` so `vault.reindex()`
  never invalidates the upstream prompt cache prefix.
- Archives 200K compacted transcripts into both `memory_vault/timeline` and SQLite FTS5 (`context_search`).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from core.long_term_memory import LongTermMemoryEngine, format_harness_transcript
from storage import normalize_node_source, node_display_name
from vault_manager import TEMPLATES, now_iso


class MemoryEngine:
    AUTO_COMPACT_TOKENS = 100_000
    KEEP_RECENT_TOKENS = 35_000
    SLEEP_CONSOLIDATE_IDLE_SECONDS = 480
    PINNED_FILE = 'profile/pinned_memories.md'

    def get_sleep_consolidate_idle_seconds(self) -> int:
        raw = getattr(getattr(self.store, 'config', None), 'sleep_consolidate_idle_seconds', self.SLEEP_CONSOLIDATE_IDLE_SECONDS)
        try:
            return max(60, min(3600, int(raw or self.SLEEP_CONSOLIDATE_IDLE_SECONDS)))
        except Exception:
            return self.SLEEP_CONSOLIDATE_IDLE_SECONDS

    def __init__(
        self,
        store,
        vault,
        harness=None,
        last_prompt_tokens: dict[str, int] | None = None,
        context_search=None,
        last_cached_tokens: dict[str, int] | None = None,
    ):
        self.store = store
        self.vault = vault
        self.harness = harness
        self.last_prompt_tokens = last_prompt_tokens if last_prompt_tokens is not None else {}
        self.last_cached_tokens = last_cached_tokens if last_cached_tokens is not None else {}
        self.context_search = context_search
        vault_root = Path(getattr(self.vault, 'root', '.'))
        ltm_base = vault_root.parent if vault_root.name == 'memory_vault' else vault_root
        unified_db_path = Path(self.context_search.db_path) if (self.context_search and getattr(self.context_search, 'db_path', None)) else (ltm_base / 'long_term_memory.db')
        self.unified_db_path = unified_db_path
        self.long_term_memory = LongTermMemoryEngine(unified_db_path, vault=self.vault, legacy_db_path=ltm_base / 'long_term_memory.db')
        self._embedding_engine = None
        self._memory_tables = None
        self._consolidating = False
        self._suppress_vault_listener = False
        self.on_pins_changed = None
        self._last_vault_lines: dict[str, set[str]] = {}
        try:
            for init_rel in ('profile/personal_impression.md', 'profile/user_traits.md'):
                raw_init = self.vault.read(init_rel)
                self._last_vault_lines[init_rel] = {ln.strip() for ln in (raw_init or '').splitlines() if ln.strip()}
        except Exception:
            pass
        if hasattr(self.vault, 'mutation_listeners') and self._on_vault_mutation not in self.vault.mutation_listeners:
            self.vault.mutation_listeners.append(self._on_vault_mutation)
        try:
            saved_pt = self.store.state.get('last_prompt_tokens')
            if isinstance(saved_pt, dict):
                for k, v in saved_pt.items():
                    if int(v or 0) > 0 and k not in self.last_prompt_tokens:
                        self.last_prompt_tokens[str(k)] = int(v)
            saved_ct = self.store.state.get('last_cached_tokens')
            if isinstance(saved_ct, dict):
                for k, v in saved_ct.items():
                    if int(v or 0) >= 0 and k not in self.last_cached_tokens:
                        self.last_cached_tokens[str(k)] = int(v)
        except Exception:
            pass

    def _on_vault_mutation(self, rel_name: str, text: str) -> None:
        """Automatically record a tail memory delta whenever any core memory_vault file is written or edited."""
        if self._suppress_vault_listener:
            return
        norm_rel = (rel_name or '').replace('\\', '/').lstrip('/')
        if (
            not norm_rel
            or norm_rel == 'INDEX.md'
            or norm_rel.startswith(('logs/', 'config/', 'timeline/', 'notes/compact_', 'notes/ledger_', 'experiences/'))
        ):
            return
        lines = [ln.strip() for ln in (text or '').splitlines() if ln.strip()]
        if not lines:
            return
        title = lines[0].lstrip('#').strip()[:80]
        bullets = [ln for ln in lines[1:] if ln.startswith(('- ', '* ', '1.', '2.', '|')) and '---' not in ln]
        prev_set = self._last_vault_lines.get(norm_rel, set())
        diff_bullets = [ln for ln in bullets if ln not in prev_set and '（待了解）' not in ln and '(待了解)' not in ln]
        self._last_vault_lines[norm_rel] = set(lines)
        target_bullets = diff_bullets[-4:] if diff_bullets else bullets[-3:]
        snippet = ' ； '.join(b[:140] for b in target_bullets) if target_bullets else ' '.join(lines[1:4])[:240]
        summary = f"记忆文件更新 `{norm_rel}` ({title}): {snippet}".strip()
        self.record_memory_delta(summary, path=f'memory_vault/{norm_rel}', source='vault')

    def record_memory_delta(self, summary: str, path: str = '', source: str = 'all', after_msg_id: str | None = None) -> dict:
        """
        Append a concise memory update to the pending tail delta queue.
        Instead of mutating `messages[0]` (which destroys the 200K Prefix KV Cache),
        deltas are appended once at the bottom (tail) of the conversation context and locked in place!
        """
        clean = ' '.join(str(summary or '').strip().splitlines()).strip()
        if not clean:
            return {}
        deltas = self.store.state.setdefault('memory_deltas', [])
        now_ts = time.time()
        stamp = now_iso()
        if not after_msg_id:
            all_msgs = self.store.state.get('messages') or []
            if all_msgs and isinstance(all_msgs[-1], dict):
                after_msg_id = str(all_msgs[-1].get('id') or '')
        if deltas and path and deltas[-1].get('path') == path and not deltas[-1].get('consumed_by'):
            deltas[-1]['summary'] = clean[:500]
            deltas[-1]['timestamp'] = now_ts
            deltas[-1]['time_iso'] = stamp
            if after_msg_id:
                deltas[-1]['after_msg_id'] = after_msg_id
            item = deltas[-1]
        else:
            item = {
                'id': 'mdelta-' + secrets.token_hex(4),
                'timestamp': now_ts,
                'time_iso': stamp,
                'path': path,
                'summary': clean[:500],
                'source': source,
                'after_msg_id': after_msg_id or '',
                'anchors': {},
                'consumed_by': [],
            }
            deltas.append(item)
            if len(deltas) > 40:
                self.store.state['memory_deltas'] = deltas[-40:]
        try:
            self.store.save()
        except Exception:
            pass
        return item

    def record_delta(
        self,
        summary: str,
        path: str = '',
        source: str = 'all',
        kind: str = '',
        source_node: str = '',
        after_msg_id: str | None = None,
    ) -> dict:
        """Backward-compatible alias for `record_memory_delta`."""
        return self.record_memory_delta(
            summary=summary,
            path=path or (f'topology/{kind}' if kind else ''),
            source=source_node or source or 'all',
            after_msg_id=after_msg_id,
        )

    @staticmethod
    def _format_memory_delta_block(items: list[dict]) -> str:
        lines = [
            '[System Memory Delta / 最新记忆同步（以下为会话期间新增或变更的记忆与状态定论，追加在对话最底下以保持前缀 KV 缓存，优先级高于顶部旧快照）]'
        ]
        for d in items[-10:]:
            p_tag = f" [{d['path']}]" if d.get('path') else ''
            lines.append(f"- ({d.get('time_iso') or '最新'}){p_tag} {d.get('summary', '')}")
        return '\n'.join(lines)

    def consume_tail_memory_delta(self, bot_id: str = 'companion', source: str = 'phone', tail_msg_id: str = '') -> str:
        """
        Consume uninjected memory deltas for `(bot_id, source)` and format them as a Tail Memory Delta block
        to be appended at the very bottom of the current turn's context (`recent[-1]`).
        """
        src = self._normalize_source(source)
        consumer_key = f"{bot_id}:{src}"
        deltas = self.store.state.get('memory_deltas')
        if not isinstance(deltas, list) or not deltas:
            return ''
        unconsumed = []
        for d in deltas:
            if not isinstance(d, dict):
                continue
            consumed_by = d.setdefault('consumed_by', [])
            anchors = d.setdefault('anchors', {})
            if consumer_key not in consumed_by:
                unconsumed.append(d)
                consumed_by.append(consumer_key)
                if tail_msg_id:
                    anchors[consumer_key] = tail_msg_id
        if not unconsumed:
            return ''
        try:
            self.store.save()
        except Exception:
            pass
        return self._format_memory_delta_block(unconsumed)

    def inject_memory_deltas(self, recent_msgs: list[dict], bot_id: str = 'companion', source: str = 'phone') -> list[dict]:
        """
        Inject memory deltas into `recent_msgs` while strictly preserving 100% Prefix KV Cache
        AND 100% standard OpenAI API format (only `messages[0]` is `role="system"`, and `messages[-1]` is always `role="user"`):
        - Previously consumed deltas stay locked inside their anchored user message in `recent_msgs` (never shifting position).
        - Newly generated deltas are locked to the current tail user message (`recent_msgs[-1]['id']`) and appended at the very bottom (Tail) of its text!
        """
        if not recent_msgs:
            return recent_msgs
        deltas = self.store.state.get('memory_deltas')
        if not isinstance(deltas, list) or not deltas:
            return recent_msgs
        src = self._normalize_source(source)
        consumer_key = f"{bot_id}:{src}"
        tail_user_id = ''
        for m in reversed(recent_msgs):
            if m.get('role') == 'user' and m.get('id'):
                tail_user_id = str(m['id'])
                break
        if not tail_user_id:
            tail_user_id = str(recent_msgs[-1].get('id') or '')
        by_anchor: dict[str, list[dict]] = {}
        dirty = False
        for d in deltas:
            if not isinstance(d, dict):
                continue
            anchors = d.setdefault('anchors', {})
            consumed_by = d.setdefault('consumed_by', [])
            anchor_id = anchors.get(consumer_key)
            if not anchor_id:
                anchor_id = tail_user_id
                if tail_user_id:
                    anchors[consumer_key] = tail_user_id
                if consumer_key not in consumed_by:
                    consumed_by.append(consumer_key)
                dirty = True
            if anchor_id:
                by_anchor.setdefault(anchor_id, []).append(d)
        if dirty:
            try:
                self.store.save()
            except Exception:
                pass
        if not by_anchor:
            return recent_msgs
        out: list[dict] = []
        for m in recent_msgs:
            mid = str(m.get('id') or '')
            if mid and mid in by_anchor:
                block = '\n\n' + self._format_memory_delta_block(by_anchor[mid])
                c = m.get('content')
                if isinstance(c, list):
                    new_c = []
                    appended = False
                    for part in c:
                        if isinstance(part, dict) and part.get('type') == 'text' and not appended:
                            new_c.append({**part, 'text': str(part.get('text') or '') + block})
                            appended = True
                        else:
                            new_c.append(part)
                    if not appended:
                        new_c.insert(0, {'type': 'text', 'text': block.strip()})
                    out.append({**m, 'content': new_c})
                else:
                    out.append({**m, 'content': str(c or '') + block})
            else:
                out.append(m)
        return out

    def peek_recent_memory_deltas(self, limit: int = 8) -> str:
        """Read-only digest of recent memory deltas for curiosity / scheduled tasks."""
        deltas = self.store.state.get('memory_deltas')
        if not isinstance(deltas, list) or not deltas:
            return ''
        recent = [d for d in deltas if isinstance(d, dict)][-limit:]
        if not recent:
            return ''
        lines = ['【近期记忆与状态变更同步 (Memory Deltas)】']
        for d in recent:
            p_tag = f" [{d['path']}]" if d.get('path') else ''
            lines.append(f"- ({d.get('time_iso') or '最新'}){p_tag} {d.get('summary', '')}")
        return '\n'.join(lines)

    def clear_memory_deltas(self, bot_id: str | None = None, source: str | None = None) -> None:
        """Clear pending memory deltas after 200K full compaction refreshes `messages[0]`."""
        if not bot_id:
            self.store.state['memory_deltas'] = []
        else:
            src = self._normalize_source(source) if source else None
            deltas = self.store.state.get('memory_deltas')
            if isinstance(deltas, list):
                for d in deltas:
                    if not isinstance(d, dict):
                        continue
                    consumed = d.setdefault('consumed_by', [])
                    anchors = d.setdefault('anchors', {})
                    targets = [f'{bot_id}:{src}'] if src else [f'{bot_id}:phone', f'{bot_id}:phone2', f'{bot_id}:pc', f'{bot_id}:pc2']
                    for ck in targets:
                        if ck not in consumed:
                            consumed.append(ck)
                        anchors.pop(ck, None)
        try:
            self.store.save()
        except Exception:
            pass

    @staticmethod
    def estimate_tokens(text) -> int:
        from core.llm_client import LLMClient
        if not text:
            return 0
        if isinstance(text, list):
            return sum(1100 if (isinstance(p, dict) and p.get('type') == 'image_url') else MemoryEngine.estimate_tokens((p.get('text') or p.get('content') or '') if isinstance(p, dict) else str(p)) for p in text)
        return LLMClient._estimate_text_tokens(json.dumps(text, ensure_ascii=False) if isinstance(text, dict) else str(text))

    @staticmethod
    def estimate_messages_tokens(messages: list[dict]) -> int:
        from core.llm_client import LLMClient
        return LLMClient._estimate_payload_tokens(messages)

    @staticmethod
    def prune_tool_loop_messages(messages: list[dict]):
        """
        Prunes transient multimodal `image_url` blocks from earlier tool-loop steps (`messages[:-1]`),
        retaining only the newest step's visual screenshot while keeping text prefix 100% intact
        and replacing message dicts immutably without mutating stored references in-place.
        """
        if not isinstance(messages, list) or len(messages) <= 2:
            return
        for idx in range(len(messages) - 1):
            msg = messages[idx]
            if not isinstance(msg, dict):
                continue
            content = msg.get('content')
            if isinstance(content, list) and any(isinstance(p, dict) and p.get('type') == 'image_url' for p in content):
                cleaned_parts = []
                for p in content:
                    if isinstance(p, dict) and p.get('type') == 'image_url':
                        cleaned_parts.append({'type': 'text', 'text': '[历史步骤截图已省略]'})
                    else:
                        cleaned_parts.append(p)
                messages[idx] = {**msg, 'content': cleaned_parts}

    # --- Visual Memory Pinning (📌) ---
    @staticmethod
    def _normalize_source(source: str | None) -> str:
        return normalize_node_source(source)

    @classmethod
    def _parse_pin_source_and_text(cls, raw_text: str) -> tuple[str, str]:
        t = (raw_text or '').strip()
        m = re.match(r'^\[来源:\s*(PC端|手机端|Windows一|Windows二|安卓一|安卓二|pc2|pc|phone2|phone)\]\s*(.+)$', t, re.IGNORECASE)
        if m:
            raw_tag = m.group(1).strip()
            src = normalize_node_source(raw_tag)
            clean_body = m.group(2).strip()
            tag_label = '[来源: PC端]' if src == 'pc' else ('[来源: 手机端]' if src == 'phone' else f'[来源: {node_display_name(src)}]')
            return src, f'{tag_label} {clean_body}'
        return 'phone', t

    def list_pins(self) -> list[dict]:
        try:
            raw = self.vault.read(self.PINNED_FILE)
        except Exception:
            return []
        pins: list[dict] = []
        for ln in raw.splitlines():
            s = ln.strip()
            if not s or s.startswith('#'):
                continue
            m = re.match(r'^-\s*\[([a-zA-Z0-9_-]+)\]\s*(.+)$', s)
            if m:
                src, formatted_text = self._parse_pin_source_and_text(m.group(2).strip())
                pins.append({'id': m.group(1), 'text': formatted_text, 'source': src})
            elif s.startswith('- '):
                pid = 'pin-' + hashlib.sha1(s.encode('utf-8')).hexdigest()[:6]
                src, formatted_text = self._parse_pin_source_and_text(s[2:].strip())
                pins.append({'id': pid, 'text': formatted_text, 'source': src})
        return pins

    def get_pins(self) -> list[dict]:
        return self.list_pins()

    def _save_pins(self, pins: list[dict]) -> None:
        lines = ['# 📌 核心钉住记忆 (Pinned Core Memories)', '']
        for p in pins:
            lines.append(f"- [{p['id']}] {p['text']}")
        self.vault.write(self.PINNED_FILE, '\n'.join(lines) + '\n')

    def _emit_pins_changed(self, source: str | None = None) -> None:
        cb = getattr(self, 'on_pins_changed', None)
        if callable(cb):
            try:
                cb(source=source)
            except TypeError:
                try:
                    cb()
                except Exception:
                    pass
            except Exception:
                pass

    def add_pin(self, text: str, bot_id: str = 'companion', source: str = 'phone') -> dict:
        t = (text or '').strip()
        if not t:
            return {'error': '钉住内容不能为空'}
        src = self._normalize_source(source)
        tag_rx = r'^\[来源:\s*(PC端|手机端|Windows一|Windows二|安卓一|安卓二|pc2|pc|phone2|phone)\]'
        if re.match(tag_rx, t, re.IGNORECASE):
            src, formatted_text = self._parse_pin_source_and_text(t)
        else:
            tag_label = '[来源: PC端]' if src == 'pc' else ('[来源: 手机端]' if src == 'phone' else f'[来源: {node_display_name(src)}]')
            formatted_text = f'{tag_label} {t}'
        pins = self.list_pins()
        clean_target = re.sub(tag_rx + r'\s*', '', formatted_text, flags=re.IGNORECASE).strip()
        for p in pins:
            existing_clean = re.sub(tag_rx + r'\s*', '', p['text'], flags=re.IGNORECASE).strip()
            if p['text'] == formatted_text or existing_clean == clean_target:
                p['text'] = formatted_text[:600]
                p['source'] = src
                self._save_pins(pins)
                self._emit_pins_changed(source=src)
                return {'ok': True, 'pinned': True, 'id': p['id'], 'pin': p, 'pins': pins, 'source': src, 'total': len(pins)}
        new_pin = {'id': 'pin-' + secrets.token_hex(3), 'text': formatted_text[:600], 'source': src}
        pins.append(new_pin)
        self._save_pins(pins)
        self._emit_pins_changed(source=src)
        return {'ok': True, 'pinned': True, 'id': new_pin['id'], 'pin': new_pin, 'pins': pins, 'source': src, 'total': len(pins)}

    def remove_pin(self, pin_id_or_text: str) -> dict:
        q = (pin_id_or_text or '').strip()
        pins = self.list_pins()
        kept = [p for p in pins if p['id'] != q and q not in p['text']]
        removed_count = len(pins) - len(kept)
        if removed_count > 0:
            self._save_pins(kept)
            self._emit_pins_changed()
        return {'ok': removed_count > 0, 'removed': removed_count > 0, 'removed_count': removed_count, 'pins': kept, **({'error': f'未找到匹配的置顶记忆: {q}'} if removed_count == 0 else {})}

    def build_static_pinned_block(self) -> str:
        """Static block safe to include inside `messages[0]` system prompt without breaking Prefix Cache."""
        pins = self.list_pins()
        if not pins:
            return ''
        lines = ['【📌 永不遗忘的核心钉住记忆（最高优先级）】']
        for p in pins:
            lines.append(f"- [{p['id']}] {p['text']}")
        return '\n'.join(lines)

    def get_personal_impression(self) -> tuple[str, list[str]]:
        """Read `profile/personal_impression.md` and return `(full_text, missing_items)`."""
        imp_path = 'profile/personal_impression.md'
        try:
            raw_imp = self.vault.read(imp_path)
        except Exception:
            from vault_manager import TEMPLATES
            raw_imp = TEMPLATES.get(imp_path, '')
            if raw_imp:
                try:
                    self.vault.write(imp_path, raw_imp)
                except Exception:
                    pass
        missing: list[str] = []
        current_sec = ''
        for ln in raw_imp.splitlines():
            s = ln.strip()
            if s.startswith('## '):
                current_sec = s.lstrip('#').strip()
            elif s.startswith('- ') and ('（待了解）' in s or '(待了解)' in s):
                item_label = s[2:].split('：')[0].split(':')[0].strip()
                missing.append(f'{current_sec} -> {item_label}' if current_sec else item_label)
        return raw_imp, missing

    def get_embedding_engine(self):
        if self._embedding_engine is None:
            from core.embedding_engine import EmbeddingEngine
            models_dir = Path(getattr(self.store, 'root', '.')) / 'models' / 'embeddings'
            self._embedding_engine = EmbeddingEngine(models_dir, config_getter=lambda: getattr(self.store, 'config', None))
        return self._embedding_engine

    def get_memory_tables(self):
        if self._memory_tables is None:
            from core.memory_tables import MemoryTablesStore
            self._memory_tables = MemoryTablesStore(self.unified_db_path, embedding_engine=self.get_embedding_engine())
        return self._memory_tables

    def build_prompt_context(self) -> str:
        """Dynamic vault index + core_blocks + user traits + structured personal impression digest."""
        raw_idx = self.vault.read('INDEX.md')
        idx = raw_idx[:1200] if len(raw_idx) <= 1200 else (raw_idx[:750] + '\n...\n' + raw_idx[-400:])
        raw_traits = self.vault.read('profile/user_traits.md')
        if len(raw_traits) <= 2800:
            traits = raw_traits
        else:
            traits = (
                raw_traits[:1400]
                + '\n...(中间历史画像条目保存在磁盘 memory_vault/profile/user_traits.md 中未删减，可随时 read_file 查阅)...\n'
                + raw_traits[-1400:]
            )
        raw_imp, missing_imp = self.get_personal_impression()
        core_blocks_txt = ''
        try:
            core_blocks_txt = self.get_memory_tables().build_static_core_blocks_text(max_total_chars=2400)
        except Exception:
            core_blocks_txt = ''
        parts = [
            f'【记忆库目录 INDEX.md（可用 read_file("memory_vault/...") 或 memory(op="search"|"query_facts") 翻阅详情；按需技能存放在 memory_vault/skills/）】\n{idx}',
        ]
        if core_blocks_txt:
            parts.append(f'【结构化核心记忆块 (core_blocks)】\n{core_blocks_txt}')
        parts.append(f'【个人印象档案 (memory_vault/profile/personal_impression.md)】\n{raw_imp[:1100]}')
        if missing_imp:
            idle_min = max(1, round(self.get_sleep_consolidate_idle_seconds() / 60))
            parts.append(
                f'【个人印象待补全维度（记忆运转规则：1. 日常闲聊中用户透露的新信息，系统会在停歇 {idle_min} 分钟后由后台记忆引擎增量归档进 entity_facts、personal_impression.md 与 user_traits.md 并增量建向量，无需每句话都调工具；2. 若你本轮未实际调用 edit_file/write_file/memory_pin 工具，严禁在回复中谎称“已经写进档案文件了/已经填好表了”，只说明已暂记上下文；3. 若用户要求现在写入或你决定当场落盘，必须真实调用一次 edit_file("memory_vault/profile/personal_impression.md", ...) 或 memory_pin 后再说已写入档案）】\n- '
                + '\n- '.join(missing_imp[:6])
            )
        parts.append(f'【用户核心画像日志】\n{traits}')
        return '\n\n'.join(parts)

    def build_lean_context(self) -> str:
        return self.build_prompt_context()

    def get_threshold_tokens(self) -> int:
        raw = getattr(getattr(self.store, 'config', None), 'auto_compact_tokens', self.AUTO_COMPACT_TOKENS)
        return max(8_000, min(1_000_000, int(raw or self.AUTO_COMPACT_TOKENS)))

    def estimate_message_tokens(self, m: dict) -> int:
        if not isinstance(m, dict):
            return 0
        raw_txt = m.get('llm_text') if m.get('llm_text') is not None else m.get('text', '')
        tok = self.estimate_tokens(raw_txt) + 12
        steps = m.get('turn_steps')
        if isinstance(steps, list) and steps:
            for item in steps[-12:]:
                tok += self.estimate_tokens(item)
        return tok

    def _estimate_effective_sys_tokens(self, bot_id: str, src: str, fallback: int = 14800) -> int:
        base = fallback
        try:
            if self.harness is not None and hasattr(self.harness, 'static_system_instructions'):
                pinned = self.build_static_pinned_block()
                sys_txt = self.harness.static_system_instructions(pinned)
                tools_sz = sum(len(json.dumps(t, ensure_ascii=False)) for t in (self.harness.all_tools() or [])) if hasattr(self.harness, 'all_tools') else 0
                computed = self.estimate_tokens(sys_txt) + int(tools_sz * 0.45) + self.estimate_tokens(self.store.state.get('cached_vault_context') or '')
                if computed > 1000:
                    base = computed
        except Exception:
            pass
        return base

    def recalculate_tokens_after_rewind(self, bot_id: str = 'companion', source: str | None = None, sys_tokens: int | None = None) -> int:
        """Reset cached hit metrics and recompute accurate current context size after reroll/edit/variant-switch."""
        src = normalize_node_source(source)
        nb_key = f'{src}:{bot_id}'
        eff_sys = sys_tokens if (sys_tokens is not None and sys_tokens > 0) else self._estimate_effective_sys_tokens(bot_id, src)
        bot_msgs = [
            m for m in self.store.state.get('messages', [])
            if m.get('bot_id', 'companion') == bot_id
            and normalize_node_source(m.get('source')) == src
        ]
        # Keep only window from the newest compact_summary forward
        last_comp_idx = -1
        for i in range(len(bot_msgs) - 1, -1, -1):
            if bot_msgs[i].get('kind') == 'compact_summary':
                last_comp_idx = i
                break
        active_msgs = bot_msgs[last_comp_idx:] if last_comp_idx >= 0 else bot_msgs
        est = eff_sys + sum(self.estimate_message_tokens(m) for m in active_msgs)
        self.last_prompt_tokens[nb_key] = est
        self.last_cached_tokens[nb_key] = 0
        self.store.state.setdefault('last_prompt_tokens', {})[nb_key] = est
        self.store.state.setdefault('last_cached_tokens', {})[nb_key] = 0
        if src == 'phone':
            self.last_prompt_tokens[bot_id] = est
            self.last_cached_tokens[bot_id] = 0
            self.store.state.setdefault('last_prompt_tokens', {})[bot_id] = est
            self.store.state.setdefault('last_cached_tokens', {})[bot_id] = 0
        return est

    def get_context_stats(self, bot_id: str = 'companion', sys_tokens: int = 14800, source: str | None = None) -> dict:
        threshold = self.get_threshold_tokens()
        src = normalize_node_source(source) if source else None
        if src is None:
            if str(bot_id).startswith('pc_') and int(self.last_prompt_tokens.get(f'pc:{bot_id}', 0) or 0) > 0:
                src = 'pc'
            elif int(self.last_prompt_tokens.get(f'phone:{bot_id}', 0) or 0) == 0 and int(self.last_prompt_tokens.get(f'pc:{bot_id}', 0) or 0) > 0:
                src = 'pc'
        bot_msgs = [
            m for m in self.store.state.get('messages', [])
            if m.get('bot_id', 'companion') == bot_id
            and (src is None or normalize_node_source(m.get('source')) == src)
        ]
        last_comp_idx = -1
        last_comp_at = 0.0
        for i in range(len(bot_msgs) - 1, -1, -1):
            if bot_msgs[i].get('kind') == 'compact_summary':
                last_comp_idx = i
                last_comp_at = float(bot_msgs[i].get('created_at') or 0.0)
                break
        active_msgs = bot_msgs[last_comp_idx:] if last_comp_idx >= 0 else bot_msgs
        msg_tokens = sum(self.estimate_message_tokens(m) for m in active_msgs)
        eff_src = src or 'phone'
        nb_key = f'{eff_src}:{bot_id}'
        st_metrics = self.store.state.get('last_metrics') or {}
        if eff_src == 'phone':
            rep = int(self.last_prompt_tokens.get(nb_key, self.last_prompt_tokens.get(bot_id, 0)) or 0)
            cached_tokens = int(self.last_cached_tokens.get(nb_key, self.last_cached_tokens.get(bot_id, 0)) or 0)
            last_m = st_metrics.get(nb_key) or st_metrics.get(bot_id) or {}
        else:
            rep = int(self.last_prompt_tokens.get(nb_key, 0) or 0)
            cached_tokens = int(self.last_cached_tokens.get(nb_key, 0) or 0)
            last_m = st_metrics.get(nb_key) or {}
        if rep <= 0:
            for m in reversed(active_msgs):
                if m.get('role') == 'assistant' and m.get('kind') in (None, '', 'chat') and isinstance(m.get('metrics'), dict):
                    mp = int(m['metrics'].get('prompt_tokens') or 0)
                    if mp > 0:
                        rep = mp
                        cached_tokens = min(int(m['metrics'].get('cached_tokens') or 0), mp)
                        last_m = last_m or m['metrics']
                        break
        eff_sys = self._estimate_effective_sys_tokens(bot_id, eff_src, fallback=sys_tokens) if sys_tokens == 14800 else sys_tokens
        current_tokens = rep if rep > 0 else ((eff_sys + msg_tokens) if active_msgs else eff_sys)
        if cached_tokens > current_tokens:
            cached_tokens = current_tokens
        compacted_blocks = sum(1 for m in bot_msgs if m.get('kind') == 'compact_summary')
        if not last_m:
            for m in reversed(bot_msgs):
                if m.get('role') == 'assistant' and isinstance(m.get('metrics'), dict):
                    last_m = m['metrics']
                    break
        return {
            'bot_id': bot_id, 'source': eff_src, 'current_tokens': current_tokens, 'cached_tokens': cached_tokens,
            'cache_hit_ratio': round(cached_tokens / max(1, current_tokens), 4) if current_tokens > 0 else 0.0,
            'message_tokens': msg_tokens, 'reported_tokens': rep, 'threshold_tokens': threshold,
            'message_count': len(bot_msgs), 'compacted_count': compacted_blocks,
            'last_compaction_at': last_comp_at, 'last_compaction_status': 'ok' if last_comp_at > 0 else 'none',
            'usage_ratio': round(current_tokens / max(1, threshold), 4),
            'ttft_ms': int(last_m.get('ttft_ms') or 0), 'tps': float(last_m.get('tps') or 0.0),
            'completion_tokens': int(last_m.get('completion_tokens') or 0), 'total_ms': int(last_m.get('total_ms') or 0),
        }

    def get_all_bots_context_stats(self, sys_tokens: int = 14800, source: str | None = None) -> dict[str, dict]:
        bots = self.store.state.get('assistants') or []
        bot_ids = [b.get('id') for b in bots if isinstance(b, dict) and b.get('id')]
        if 'companion' not in bot_ids:
            bot_ids.insert(0, 'companion')
        return {bid: self.get_context_stats(bid, sys_tokens=sys_tokens, source=source) for bid in bot_ids}

    async def auto_compact_if_needed(
        self,
        bot_id: str,
        sys_tokens: int = 14800,
        silent_complete_fn=None,
        reported_tokens: int | None = None,
        force: bool = False,
        source: str | None = None,
        llm_stream_fn=None,
        on_stream=None,
    ) -> dict | None:
        from core.compaction_service import execute_streaming_compaction
        res = await execute_streaming_compaction(
            memory_engine=self,
            bot_id=bot_id,
            source=normalize_node_source(source),
            force=force,
            sys_tokens=sys_tokens,
            reported_tokens=reported_tokens,
            llm_stream_fn=llm_stream_fn,
            silent_complete_fn=silent_complete_fn,
            on_stream=on_stream,
        )
        if isinstance(res, dict) and res.get('compacted') and not res.get('already_compacted'):
            try:
                from core.memory_migrator import ingest_compaction_to_tables
                marker = res.get('marker') if isinstance(res.get('marker'), dict) else {}
                ingest_compaction_to_tables(
                    self,
                    bot_id=bot_id,
                    source=normalize_node_source(source),
                    summary_text=str(marker.get('text') or res.get('summary') or ''),
                    ctx_id=str(res.get('ctx_id') or ''),
                )
            except Exception:
                pass
        return res

    def apply_impression_patches(
        self, patches: list[dict] | None = None, topic_updates: list[dict] | None = None, followups: list[dict] | None = None,
    ) -> bool:
        """Section-safe incremental patcher for `profile/personal_impression.md` + SQLite sync."""
        now_ts, changed = time.time(), False
        patches = [p for p in (patches or []) if isinstance(p, dict)]
        topic_updates = [t for t in (topic_updates or []) if isinstance(t, dict)]
        followups = [f for f in (followups or []) if isinstance(f, dict)]
        raw_imp = self.vault.read('profile/personal_impression.md') or ''
        if '个人印象档案' not in raw_imp:
            raw_imp = TEMPLATES.get('profile/personal_impression.md', '# 个人印象档案\n\n## 1. 基本背景\n')
        if patches:
            lines = raw_imp.splitlines()
            for patch in patches[:12]:
                sec_hint = str(patch.get('section') or '').strip()
                item_key = str(patch.get('item_key') or patch.get('key') or '').strip()
                fact = str(patch.get('fact') or patch.get('value') or '').strip()
                if not item_key or not fact:
                    continue
                sec_indices = [i for i, ln in enumerate(lines) if ln.startswith('## ')]
                target_start, target_end = -1, len(lines)
                for idx_pos, s_idx in enumerate(sec_indices):
                    if sec_hint and (f'## {sec_hint}' in lines[s_idx] or sec_hint.lower() in lines[s_idx].lower()):
                        target_start = s_idx
                        target_end = sec_indices[idx_pos + 1] if idx_pos + 1 < len(sec_indices) else len(lines)
                        break
                if target_start < 0:
                    target_start = sec_indices[0] if sec_indices else 0
                    target_end = sec_indices[1] if len(sec_indices) > 1 else len(lines)
                norm_key, replaced = re.sub(r'[*_`]', '', item_key).strip().lower(), False
                for li in range(target_start + 1, target_end):
                    m_ln = re.match(r'^\s*[-*]\s*(?:\*\*)?([^:：\n*]+?)(?:\*\*)?\s*[:：]\s*(.*)$', lines[li])
                    if not m_ln:
                        continue
                    cur_k = m_ln.group(1).strip().lower()
                    if cur_k == norm_key or norm_key in cur_k or cur_k in norm_key:
                        old_val = m_ln.group(2).strip()
                        new_val = fact if ('(待了解)' in old_val or '（待了解）' in old_val or not old_val) else (old_val if fact in old_val else f'{old_val}；{fact}')
                        new_line = f'- **{m_ln.group(1).strip()}**：{new_val}'
                        if new_line != lines[li]:
                            lines[li] = new_line
                            changed = True
                        replaced = True
                        break
                if not replaced:
                    insert_at = target_end
                    while insert_at > target_start + 1 and not lines[insert_at - 1].strip():
                        insert_at -= 1
                    lines.insert(insert_at, f'- **{item_key}**：{fact}')
                    changed = True
            if changed:
                raw_imp = '\n'.join(lines).rstrip() + '\n'
                self.vault.write('profile/personal_impression.md', raw_imp)
        sql = getattr(self.store, 'sql', None)
        if sql is not None:
            if raw_imp and hasattr(sql, 'seed_topics_from_impression_markdown'):
                sql.seed_topics_from_impression_markdown(raw_imp, now_ts=now_ts)
            if hasattr(sql, 'upsert_curiosity_topic'):
                for tu in topic_updates[:8]:
                    lbl = str(tu.get('label') or tu.get('topic_id') or '').strip()
                    if not lbl:
                        continue
                    sql.upsert_curiosity_topic(
                        str(tu.get('topic_id') or '').strip() or None,
                        category=str(tu.get('category') or 'general').strip(), label=lbl,
                        depth=int(tu['depth']) if tu.get('depth') is not None else None,
                        known_summary=str(tu['known_summary']) if tu.get('known_summary') is not None else None,
                        open_question=str(tu['open_question']) if tu.get('open_question') is not None else None,
                        status=str(tu['status']) if tu.get('status') is not None else None, now_ts=now_ts,
                    )
                    changed = True
            if hasattr(sql, 'add_proactive_followup'):
                for fu in followups[:6]:
                    topic = str(fu.get('topic') or '').strip()
                    if topic:
                        sql.add_proactive_followup(
                            bot_id=str(fu.get('bot_id') or 'companion'), source=str(fu.get('source') or 'phone'),
                            topic=topic, context=str(fu.get('context') or ''),
                            due_at=now_ts + max(0.25, float(fu.get('due_hours') or 12.0)) * 3600.0, now_ts=now_ts,
                        )
                        changed = True
        return changed

    async def sleep_time_consolidate(self, silent_complete_fn):
        """Silently consolidate recent conversation facts into Zettelkasten atomic notes without touching any UI stream."""
        if self._consolidating:
            return
        s = self.store.state
        last_consolidated = float(s.get('last_sleep_consolidate', 0.0))
        last_user = float(s.get('last_user', 0.0))
        if last_user <= last_consolidated or (time.time() - last_user) < self.get_sleep_consolidate_idle_seconds():
            return
        candidates = [
            m for m in s.get('messages', [])
            if float(m.get('created_at', 0)) > last_consolidated
            and m.get('role') in ('user', 'assistant')
            and m.get('kind') not in ('error', 'compact_summary', 'notification', 'bot_message', 'bot_reply')
            and not m.get('is_sub_turn')
        ]
        # Keep `curiosity` assistant messages ONLY when followed by a user reply
        new_msgs: list[dict] = []
        seen_user_after = False
        for m in reversed(candidates):
            if m.get('role') == 'user':
                seen_user_after = True
                new_msgs.append(m)
            elif m.get('kind') == 'curiosity':
                if seen_user_after:
                    new_msgs.append(m)
            else:
                new_msgs.append(m)
        new_msgs.reverse()
        if len(new_msgs) < 2:
            return
        self._consolidating = True
        try:
            digest = format_harness_transcript(
                new_msgs[-14:],
                max_total_chars=24_000,
                max_msg_chars=1_800,
                max_tool_chars=1_000,
            )
            existing_traits = self.vault.read('profile/user_traits.md')[-1800:]
            raw_imp, missing_imp = self.get_personal_impression()
            imp_sections = [ln.strip() for ln in raw_imp.splitlines() if ln.startswith('## ')]
            raw_pins = self.vault.read(self.PINNED_FILE)
            prompt = (
                '你是后台静默记忆整理器（Hindsight 经验记忆 + Zettelkasten 原子记忆引擎）。\n'
                '请检查以下最近对话（含工具执行与检索结果）是否有值得长期记住的客观事实或可复用方案：\n'
                '1. 【已完成事项与关键状态变更】（如某退订/待办已办结）；\n'
                '2. 【用户个人背景与偏好】（含主动对话与日常聊天中确认的细节）；\n'
                '3. 【长久经验与已查明方法 `experiences`】（例如从 Linux.do/网页/代码/命令行里找到的可行办法、命令、URL 与踩坑教训）。\n'
                '⚠️ 严禁把日常打情骂俏、亲密互动或角色扮演记录为“边界/底线/拒绝”规则！若无新客观事实、有效方案或待跟进承诺，严格只输出 `NONE`。\n'
                '若有更新，严格输出 JSON 对象：\n'
                '{"new_traits": ["..."], "completed_or_updated_pins": [{"match": "...", "replacement": "..."}], '
                '"impression_patches": [{"section": "1", "item_key": "条目名", "fact": "新事实"}], '
                '"curiosity_topic_updates": [{"category": "章节", "label": "条目名", "depth": 1, "known_summary": "...", "open_question": "...", "status": "open"}], '
                '"followups_to_add": [{"topic": "跟进事项", "context": "...", "due_hours": 12}], '
                '"experiences": [{"topic": "短主题键", "kind": "experience|fact|decision", "outcome": "success|failed|partial", "l0": "一句话触发条件与结论", "l1": "具体方法/步骤/命令/URL细节", "urls": ["https://..."]}], '
                '"note_slug": "short-slug", "note_title": "标题", "note_markdown": "精简原子卡片正文"}\n\n'
                f'【当前置顶记忆】\n{raw_pins[:1600]}\n\n'
                f'【个人印象现有章节与待了解项】\n章节：{", ".join(imp_sections)}\n待了解：{", ".join(missing_imp[:12])}\n\n'
                f'【已有画像尾部（勿重复）】\n{existing_traits}\n\n【近期新增对话与工具实录】\n{digest}'
            )
            res_msg = await silent_complete_fn([{'role': 'user', 'content': prompt}])
            raw = (res_msg.get('content') or '').strip()
            s['last_sleep_consolidate'] = time.time()
            if not raw or re.fullmatch(r'[`"\'\s]*NONE[.`"\'\s。！]*', raw, flags=re.IGNORECASE):
                self.store.save()
                return
            m_json = re.search(r'\{.*\}', raw, flags=re.DOTALL)
            if not m_json:
                self.store.save()
                return
            try:
                data = json.loads(m_json.group(0))
            except Exception:
                self.store.save()
                return
            changed = False
            delta_parts: list[str] = []
            prev_suppress = self._suppress_vault_listener
            self._suppress_vault_listener = True
            tables = self.get_memory_tables()
            try:
                for tr in (data.get('new_traits') or [])[:4]:
                    if isinstance(tr, str) and tr.strip() and tr.strip() not in existing_traits:
                        clean_tr = tr.strip()
                        if hasattr(self, 'long_term_memory') and self.long_term_memory is not None:
                            self.long_term_memory.retain(
                                l0_summary=clean_tr[:180], l1_overview=clean_tr, kind='fact',
                                bot_id='all', source='phone', outcome='success', supersede_topic=False,
                            )
                        tables.upsert_fact_reconciled(
                            entity='主人', attr=f'特征_{hashlib.sha1(clean_tr.encode()).hexdigest()[:6]}',
                            value=clean_tr, ttl_type='stateful', source_ref='sleep_consolidate', bot_id='all',
                        )
                        self.vault.append('profile/user_traits.md', f'- {now_iso()}（静默整理）{clean_tr}')
                        delta_parts.append(clean_tr)
                        changed = True
                pin_updates = data.get('completed_or_updated_pins') or []
                if isinstance(pin_updates, list) and pin_updates:
                    pins = self.list_pins()
                    pins_modified = False
                    for pu in pin_updates[:4]:
                        if not isinstance(pu, dict):
                            continue
                        match_kw = str(pu.get('match') or pu.get('old_keyword') or '').strip()
                        repl_txt = str(pu.get('replacement') or pu.get('new_text') or '').strip()
                        if not match_kw or not repl_txt:
                            continue
                        matched_any = False
                        for p in pins:
                            if match_kw in str(p.get('text') or ''):
                                p_src = normalize_node_source(p.get('source'))
                                src_tag = '[来源: PC端]' if p_src == 'pc' else ('[来源: 手机端]' if p_src == 'phone' else f'[来源: {node_display_name(p_src)}]')
                                if not repl_txt.startswith('[来源:'):
                                    p['text'] = f"{src_tag} {repl_txt}"[:500]
                                else:
                                    p['text'] = repl_txt[:500]
                                matched_any = True
                                pins_modified = True
                        if not matched_any and match_kw in raw_pins:
                            pins.append({
                                'id': 'pin-' + secrets.token_hex(3),
                                'text': f"[来源: 手机端] {repl_txt}"[:500] if not repl_txt.startswith('[来源:') else repl_txt[:500],
                                'source': 'phone',
                            })
                            pins_modified = True
                        delta_parts.append(f"状态变更确认：{repl_txt}")
                    if pins_modified:
                        self._save_pins(pins[-30:])
                        changed = True
                patches = data.get('impression_patches') or []
                topic_upds = data.get('curiosity_topic_updates') or []
                followups = data.get('followups_to_add') or []
                if self.apply_impression_patches(patches=patches, topic_updates=topic_upds, followups=followups):
                    for pt in (patches if isinstance(patches, list) else [])[:4]:
                        if isinstance(pt, dict) and pt.get('fact'):
                            ik = str(pt.get('item_key') or pt.get('key') or '个人印象').strip()
                            fv = str(pt.get('fact') or '').strip()
                            tables.upsert_fact_reconciled(entity='主人', attr=ik[:80], value=fv, ttl_type='permanent', source_ref='sleep_consolidate', bot_id='all')
                            delta_parts.append(f"印象更新({ik})：{fv[:80]}")
                    changed = True
                slug = re.sub(r'[^a-zA-Z0-9_-]', '', str(data.get('note_slug') or ''))[:40].lower()
                md_body = str(data.get('note_markdown') or '').strip()
                if slug and md_body:
                    self.vault.write(f'notes/{slug}.md', md_body + f'\n\n> 更新时间：{now_iso()}\n')
                    changed = True
                exps = data.get('experiences') or []
                if isinstance(exps, list) and exps and hasattr(self, 'long_term_memory'):
                    for ex in exps[:6]:
                        if not isinstance(ex, dict):
                            continue
                        l0 = str(ex.get('l0') or ex.get('summary') or '').strip()
                        l1 = str(ex.get('l1') or ex.get('detail') or ex.get('overview') or '').strip()
                        if not l0 and not l1:
                            continue
                        urls = ex.get('urls') if isinstance(ex.get('urls'), list) else []
                        res_ret = self.long_term_memory.retain(
                            l0_summary=l0, l1_overview=l1, topic=str(ex.get('topic') or ''),
                            kind=str(ex.get('kind') or 'experience'), bot_id='companion', source='phone',
                            source_urls=[str(u) for u in urls if u], outcome=str(ex.get('outcome') or 'success'), importance=1.2,
                        )
                        tables.upsert_fact_reconciled(
                            entity=str(ex.get('topic') or '经验方案').strip()[:80], attr=str(ex.get('kind') or 'experience')[:40],
                            value=(f'{l0} — {l1}' if l0 and l1 and l0 not in l1 else (l1 or l0)),
                            ttl_type='project', search_cues=' '.join([str(u) for u in urls if u][:2]),
                            source_ref='sleep_consolidate', bot_id='companion',
                        )
                        if res_ret.get('ok'):
                            delta_parts.append(f"长久经验沉淀：{l0[:60]}")
                            changed = True
                if changed:
                    self.vault.reindex()
            finally:
                self._suppress_vault_listener = prev_suppress
            if changed:
                # Note: NEVER overwrite s['cached_vault_context'] here because mutating messages[0] invalidates the 200K Prefix KV Cache!
                if delta_parts:
                    self.record_memory_delta(' ； '.join(delta_parts[:4]), path='memory_vault/profile/user_traits.md', source='sleep_consolidate')
            s['last_sleep_consolidate'] = time.time()
            self.store.save()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("sleep_time_consolidate error: %s", e)
        finally:
            self._consolidating = False
