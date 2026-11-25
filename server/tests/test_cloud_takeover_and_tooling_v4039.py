"""Regression tests for v4.0.39: Cloud Computer takeover bypass, tool schema hygiene, schedule split, doc extraction & web.request."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import zipfile

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from computer_models import InputAction, NavigateInput
from core.doc_extractor import extract_document_text
from core.tool_schemas import CANONICAL_TOOL_NAMES, TOOLS, normalize_tool_name


def test_cloud_models_have_actor_default_agent():
    act = InputAction(kind='click', x=100, y=200)
    assert act.actor == 'agent'
    act_u = InputAction(kind='click', x=100, y=200, actor='user')
    assert act_u.actor == 'user'
    nav = NavigateInput(url='https://example.com')
    assert nav.actor == 'agent'
    nav_u = NavigateInput(url='https://example.com', actor='user')
    assert nav_u.actor == 'user'


def test_tool_schemas_have_schedule_and_enum_and_missing_fields_fixed():
    assert 'schedule' in CANONICAL_TOOL_NAMES
    assert len(CANONICAL_TOOL_NAMES) == 15
    assert normalize_tool_name('schedule_task') == 'schedule'

    by_name = {t['function']['name']: t['function']['parameters'] for t in TOOLS}
    assert set(by_name.keys()) == set(CANONICAL_TOOL_NAMES)

    # Every op-based tool must have enum on op/action
    for tname in ('edit_file', 'search_files', 'bash', 'web', 'memory', 'schedule', 'phone', 'pc', 'cloud', 'reply', 'admin'):
        assert 'enum' in by_name[tname]['properties']['op'], f'{tname}.op missing enum'
    assert 'enum' in by_name['blackboard']['properties']['action']

    # Previously broken/missing parameters
    assert 'action' in by_name['pc']['properties']
    assert 'device_id' in by_name['pc']['properties']
    assert 'nx2' in by_name['pc']['properties']
    assert 'api_base_url' in by_name['admin']['properties']
    assert 'default_model' in by_name['admin']['properties']
    assert 'settings' in by_name['admin']['properties']
    assert 'crop_box' in by_name['reply']['properties']
    assert 'bg_wait' in by_name['bash']['properties']['op']['enum']
    assert 'request' in by_name['web']['properties']['op']['enum']
    assert 'promote' in by_name['phone']['properties']['op']['enum']
    assert 'amount' in by_name['cloud']['properties']


def test_doc_extractor_docx_and_xlsx(tmp_path: Path):
    docx_p = tmp_path / 'sample.docx'
    with zipfile.ZipFile(docx_p, 'w') as zf:
        zf.writestr(
            'word/document.xml',
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>Hello Docx 世界</w:t></w:r></w:p></w:body></w:document>',
        )
    txt, fmt = extract_document_text(docx_p)
    assert fmt == 'docx'
    assert 'Hello Docx 世界' in txt

    xlsx_p = tmp_path / 'sample.xlsx'
    with zipfile.ZipFile(xlsx_p, 'w') as zf:
        zf.writestr(
            'xl/sharedStrings.xml',
            '<sst><si><t>项目</t></si><si><t>状态</t></si></sst>',
        )
        zf.writestr(
            'xl/worksheets/sheet1.xml',
            '<worksheet><sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
            '<row><c><v>42</v></c></row></sheetData></worksheet>',
        )
    xtxt, xfmt = extract_document_text(xlsx_p)
    assert xfmt == 'xlsx'
    assert '项目\t状态' in xtxt
    assert '42' in xtxt


def test_cloud_desktop_takeover_blocks_agent_but_allows_user(monkeypatch):
    from computer_desktop import CloudDesktop

    d = object.__new__(CloudDesktop)
    d.user_takeover_locked = True
    d.control = 'user'
    d.current_url = 'https://example.com'
    d.input_lock = asyncio.Lock()
    d._active_windows_summary = lambda: 'Browser'
    d._auto_takeover_for_interactive = lambda: None
    d._quota_info = lambda: {}
    d.frame = lambda q=62: {'unchanged': False, 'image': 'abc', 'hash': 'h1'}

    async def fake_exec_single(**kwargs):
        return {'ok': True, 'x': 120, 'y': 240}

    d._exec_single_input = fake_exec_single

    res_agent = asyncio.run(d.input(InputAction(kind='click', x=120, y=240, actor='agent')))
    assert res_agent['ok'] is False
    assert 'user_takeover_locked' in res_agent['error']

    res_user = asyncio.run(d.input(InputAction(kind='click', x=120, y=240, actor='user', include_frame=True)))
    assert res_user['ok'] is True
    assert res_user['x'] == 120
    assert res_user['image'] == 'abc'
