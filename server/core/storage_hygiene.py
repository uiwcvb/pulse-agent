"""Unified storage lifecycle & garbage collection helpers."""
from __future__ import annotations

import contextlib
import hashlib
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from storage import Store


def run_storage_hygiene(
    store: Store,
    vault=None,
    context_search=None,
    workspace_dir: Path | None = None,
    force_vacuum: bool = False,
    upload_ttl_days: float = 7.0,
    max_uploads_bytes: int = 20_000_000,
) -> dict:
    """
    Unified Storage Lifecycle & Garbage Collection Engine:
    - Scrubs state bloat (expired timers, deadlines, old notifications, stale pending items)
      without mutating active LLM message prefixes.
    - Checkpoints SQLite WAL via `store.sql.checkpoint_wal('TRUNCATE')`.
    - Prunes memory_vault/.history snapshots, FTS5 context_index.db, workspace transfers/logs/uploads,
      0-byte orphan DB files, and idle browser caches.
    """
    now_ts = time.time()
    report: dict = {
        'timestamp': now_ts,
        'freed_bytes_total': 0,
        'files_removed': 0,
        'vault': {},
        'sqlite': {},
        'workspace': {},
        'browser_cache_freed_bytes': 0,
    }

    # 1. Scrub state bloat & checkpoint SQLite WAL
    try:
        before_state_sz = store.state_path.stat().st_size if store.state_path.exists() else 0
        store._scrub_state_bloat(drop_caches=True)
        store.save()
        if hasattr(store, 'sql'):
            with contextlib.suppress(Exception):
                if hasattr(store.sql, 'checkpoint_wal'):
                    store.sql.checkpoint_wal('TRUNCATE')
                else:
                    store.sql.close()
            report['sqlite_state'] = store.sql.stats()
        after_state_sz = store.state_path.stat().st_size if store.state_path.exists() else 0
        report['state_freed_bytes'] = max(0, before_state_sz - after_state_sz)
        report['freed_bytes_total'] += report['state_freed_bytes']
    except Exception:
        pass

    # 2. Prune Vault .history snapshots & logs
    if vault is not None and hasattr(vault, 'prune_storage'):
        try:
            v_res = vault.prune_storage()
            report['vault'] = v_res
            report['freed_bytes_total'] += int(v_res.get('freed_bytes') or 0)
            report['files_removed'] += int(v_res.get('removed_snapshots') or 0)
        except Exception:
            pass

    # 3. Prune & optimize SQLite FTS5 context_index.db
    if context_search is not None and hasattr(context_search, 'prune_and_optimize'):
        try:
            s_res = context_search.prune_and_optimize(force_vacuum=force_vacuum)
            report['sqlite'] = s_res
            report['freed_bytes_total'] += int(s_res.get('freed_bytes') or 0)
        except Exception:
            pass

    # 4. Clean 0-byte orphan DB files in data root
    data_root = store.root
    for empty_db_name in ('agent.db', 'context_index.db', 'context_search.db', 'state.sqlite3'):
        cand = data_root / empty_db_name
        try:
            if cand.is_file() and cand.stat().st_size == 0:
                cand.unlink(missing_ok=True)
                report['files_removed'] += 1
        except Exception:
            pass

    # 5. Clean workspace transfers, .bg_logs, and uploads
    ws = workspace_dir or store.workspace_dir
    ws_freed = 0
    ws_removed = 0

    for trans_dir in (data_root / '.transfers', ws / '.transfers'):
        if trans_dir.exists() and trans_dir.is_dir():
            for part in trans_dir.glob('*.partial'):
                try:
                    st = part.stat()
                    if (now_ts - st.st_mtime) > 21600:
                        part.unlink(missing_ok=True)
                        ws_freed += st.st_size
                        ws_removed += 1
                except Exception:
                    pass

    bg_dir = ws / '.bg_logs'
    if bg_dir.exists() and bg_dir.is_dir():
        bg_logs = [f for f in bg_dir.glob('*.log') if f.is_file() and not f.is_symlink()]
        bg_logs.sort(key=lambda p: p.stat().st_mtime)
        cutoff_bg = now_ts - 3 * 86400.0
        for idx, lg in enumerate(bg_logs):
            try:
                st = lg.stat()
                if st.st_mtime < cutoff_bg or idx < len(bg_logs) - 20 or st.st_size == 0:
                    lg.unlink(missing_ok=True)
                    ws_freed += st.st_size
                    ws_removed += 1
                elif st.st_size > 262_144:
                    raw_b = lg.read_bytes()[-131_072:]
                    lg.write_bytes(raw_b)
                    ws_freed += (st.st_size - len(raw_b))
            except Exception:
                pass

    up_dir = ws / 'uploads'
    if up_dir.exists() and up_dir.is_dir():
        referenced_names: set[str] = set()
        for m in store.state.get('messages', []):
            if isinstance(m, dict):
                txt_m = str(m.get('text') or '')
                if 'uploads/' in txt_m or '/workspace-media/' in txt_m:
                    for f_cand in up_dir.iterdir():
                        if f_cand.name in txt_m:
                            referenced_names.add(f_cand.name)
        up_files = [f for f in up_dir.iterdir() if f.is_file() and not f.is_symlink()]
        up_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        seen_hashes: set[str] = set()
        kept_uploads: list[tuple[Path, int, float]] = []
        cutoff_up = now_ts - upload_ttl_days * 86400.0
        for uf in up_files:
            try:
                st = uf.stat()
                if st.st_mtime < cutoff_up and uf.name not in referenced_names:
                    uf.unlink(missing_ok=True)
                    ws_freed += st.st_size
                    ws_removed += 1
                    continue
                if st.st_size <= 10_000_000:
                    h = hashlib.sha256(uf.read_bytes()).hexdigest()
                    if h in seen_hashes and uf.name not in referenced_names:
                        uf.unlink(missing_ok=True)
                        ws_freed += st.st_size
                        ws_removed += 1
                        continue
                    seen_hashes.add(h)
                kept_uploads.append((uf, st.st_size, st.st_mtime))
            except Exception:
                pass

        total_up_bytes = sum(x[1] for x in kept_uploads)
        while total_up_bytes > max_uploads_bytes and len(kept_uploads) > 1:
            oldest_p, oldest_sz, _ = kept_uploads.pop()
            try:
                oldest_p.unlink(missing_ok=True)
                total_up_bytes -= oldest_sz
                ws_freed += oldest_sz
                ws_removed += 1
            except Exception:
                pass

    report['workspace'] = {'freed_bytes': ws_freed, 'files_removed': ws_removed}
    report['freed_bytes_total'] += ws_freed
    report['files_removed'] += ws_removed

    # 6. Clean Cloud Desktop Chromium & user cache bloat if Chromium is not running
    desktop_home = data_root.parent / 'cloud-computer' / 'desktop-home'
    if desktop_home.exists() and desktop_home.is_dir():
        cache_freed = 0
        chrom_prof = desktop_home / '.local' / 'chromium_profile'
        chromium_running = (chrom_prof / 'SingletonLock').exists() or (chrom_prof / 'SingletonLock').is_symlink()
        cache_targets = [
            desktop_home / '.cache' / 'pip',
            desktop_home / '.cache' / 'luakit',
        ]
        if not chromium_running:
            cache_targets.extend([
                chrom_prof / 'Default' / 'Cache',
                chrom_prof / 'Default' / 'Code Cache',
                chrom_prof / 'Default' / 'GPUCache',
                chrom_prof / 'Default' / 'DawnWebGPUCache',
                chrom_prof / 'Default' / 'DawnGraphiteCache',
                chrom_prof / 'Default' / 'Service Worker' / 'CacheStorage',
                chrom_prof / 'Default' / 'Service Worker' / 'ScriptCache',
                chrom_prof / 'component_crx_cache',
                chrom_prof / 'WasmTtsEngine',
                chrom_prof / 'BrowserMetrics-spare.pma',
                chrom_prof / 'ShaderCache',
                chrom_prof / 'GrShaderCache',
                desktop_home / '.cache' / 'chromium-headless',
                desktop_home / '.cache' / 'chromium',
            ])
        for ct in cache_targets:
            try:
                if not ct.exists():
                    continue
                if ct.is_file():
                    sz = ct.stat().st_size
                    ct.unlink(missing_ok=True)
                    cache_freed += sz
                elif ct.is_dir() and not ct.is_symlink():
                    sz = sum(f.stat().st_size for f in ct.rglob('*') if f.is_file() and not f.is_symlink())
                    shutil.rmtree(ct, ignore_errors=True)
                    cache_freed += sz
            except Exception:
                pass
        report['browser_cache_freed_bytes'] = cache_freed
        report['freed_bytes_total'] += cache_freed

    return report


run_storage_maintenance = run_storage_hygiene
