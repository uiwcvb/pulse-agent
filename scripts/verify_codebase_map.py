"""Verify that CODEBASE_MAP.md documents 100% of production source files and zero removed legacy entries."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAP_FILE = ROOT / 'CODEBASE_MAP.md'

SCAN_ROOTS = (
    ROOT / 'server',
    ROOT / 'desktop' / 'dsh-plugin',
    ROOT / 'android' / 'app' / 'src' / 'main',
    ROOT / 'scripts',
)

CODE_EXTS = {'.py', '.mjs', '.js', '.css', '.kt', '.java'}

IGNORE_PARTS = {
    '__pycache__',
    '.venv',
    '.pytest_cache',
    'node_modules',
    'data',
    'memory_vault',
    'memory_vault_seed',
    'dsh_sessions',
    'res',
    'jniLibs',
    'work',
    'dist',
    'build',
}

FORBIDDEN_LEGACY = (
    'today_orchestrator.py',
    'PulseTodaySheet.kt',
    'SentinelApp.cs',
    'whalechan_onboarding.md',
    'test_v43_today_and_virtual_display.py',
)


def main() -> int:
    if not MAP_FILE.is_file():
        print('ERROR: CODEBASE_MAP.md does not exist', file=sys.stderr)
        return 1
    map_text = MAP_FILE.read_text(encoding='utf-8')

    # Must contain required sections
    for heading in (
        '## 1. Architecture & Measured Memory/Storage Comparison',
        '## 2. Annotated Repository Directory Tree',
        '## 3. Core Variables, Sync Cursors & Environment Registry',
        '## 4. Module-by-Module File & Interface Map',
    ):
        if heading not in map_text:
            print(f'ERROR: missing section {heading!r} in CODEBASE_MAP.md', file=sys.stderr)
            return 1

    # Check forbidden deleted entries
    for stale in FORBIDDEN_LEGACY:
        if stale in map_text:
            print(f'ERROR: stale removed entry {stale!r} still referenced in CODEBASE_MAP.md', file=sys.stderr)
            return 1

    missing: list[str] = []
    checked = 0
    for base in SCAN_ROOTS:
        if not base.exists():
            continue
        for f in sorted(base.rglob('*')):
            if not f.is_file():
                continue
            rel = f.relative_to(ROOT).as_posix()
            parts = set(f.relative_to(ROOT).parts)
            if parts & IGNORE_PARTS:
                continue
            if rel.startswith('server/tests/') or rel.startswith('server/dsh_brain/tests/'):
                continue
            if f.suffix.lower() not in CODE_EXTS:
                continue
            checked += 1
            if rel not in map_text:
                missing.append(rel)

    if missing:
        print(f'ERROR: {len(missing)} production file(s) missing from CODEBASE_MAP.md:', file=sys.stderr)
        for m in missing:
            print(f'  - {m}', file=sys.stderr)
        return 1

    print(f'OK: CODEBASE_MAP.md verified ({checked} production files 100% documented, 0 stale references).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
