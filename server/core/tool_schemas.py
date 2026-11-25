"""
[Module Responsibility]
工具定义与系统指令契约模块 (`server/core/tool_schemas.py`)。
集中维护 Agent 全量 14 个正交工具的 JSON Schema (`TOOLS`)、历史兼容别名映射
(`LEGACY_TOOL_ALIASES`)、默认系统核心契约 (`DEFAULT_SYSTEM_MD`)
以及公网 URL 安全校验 (`public_url`)，与沙箱执行逻辑 (`Harness`) 保持关注点分离。

[Public Interface]
- `tool(name, description, properties, required) -> dict`: 构造 OpenAI 函数调用 Schema
- `TOOLS: list[dict]`: 14 个正交静态工具列表
- `CANONICAL_TOOL_NAMES: tuple[str, ...]`: 14 个规范工具名元组
- `LEGACY_TOOL_ALIASES: dict[str, str]`: 旧版工具到 14 工具的兼容别名表
- `normalize_tool_name(name) -> str`: 将工具名归一化为规范工具名
- `is_tool_allowed(tool_name, allowed_tools) -> bool`: 兼容别名的权限检查
- `normalize_bot_tools(tools, default_all=None) -> list[str]`: 归一化 Bot 工具白名单
- `DEFAULT_SYSTEM_MD: str`: 默认 SYSTEM.md 指令模板
- `public_url(url: str) -> str`: 校验并拒绝内网/环回/元数据地址的 SSRF 防护函数

[Dependencies]
- Python 标准库 (`asyncio`, `ipaddress`, `socket`, `urllib.parse`)
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from typing import Iterable
from urllib.parse import urlsplit


def tool(name, description, properties, required):
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': description,
            'parameters': {
                'type': 'object',
                'properties': properties,
                'required': required,
                'additionalProperties': False,
            },
        },
    }


STR = {'type': 'string'}
INT = {'type': 'integer'}
NUM = {'type': 'number'}
BOOL = {'type': 'boolean'}
STR_LIST = {'type': 'array', 'items': STR}
OBJ_LIST = {'type': 'array', 'items': {'type': 'object'}}
INT_LIST = {'type': 'array', 'items': INT}

CANONICAL_TOOL_NAMES: tuple[str, ...] = (
    'read_file',
    'write_file',
    'edit_file',
    'search_files',
    'bash',
    'memory',
    'blackboard',
    'schedule',
    'phone',
    'pc',
    'cloud',
    'reply',
    'subtask',
    'admin',
)

LEGACY_TOOL_ALIASES: dict[str, str] = {
    **{n: n for n in CANONICAL_TOOL_NAMES},
    # File edit / fs ops
    'delete_file': 'edit_file',
    'move_file': 'edit_file',
    # Search / directory listing
    'list_dir': 'search_files',
    'glob_files': 'search_files',
    'grep_search': 'search_files',
    # Shell / bg / watchdog
    'run_bash': 'bash',
    'run_command': 'bash',
    'bg_task': 'bash',
    'watchdog_task': 'bash',
    # Legacy Web -> Unified Cloud Browser (anti-bot CDP text-fast-read + auto-release)
    'web': 'cloud',
    'web_search': 'cloud',
    'web_fetch': 'cloud',
    # Memory & archives
    'memory_search': 'memory',
    'conversation_search': 'memory',
    'ctx_search': 'memory',
    'context_search': 'memory',
    'memory_pin': 'memory',
    # Blackboard / schedules / bots
    'peek_bots': 'blackboard',
    'schedule_task': 'schedule',
    'send_message': 'blackboard',
    'message_colleague': 'blackboard',
    'message_pc_bot': 'blackboard',
    # Phone
    'phone_workspace': 'phone',
    'phone_macro': 'phone',
    # Windows PC
    'dsh_workspace': 'pc',
    'message_phone_bot': 'pc',
    # Cloud desktop
    'cloud_desktop': 'cloud',
    # Reply & control signals
    'send_image': 'reply',
    'present_choices': 'reply',
    'notify_user': 'reply',
    'stay_silent': 'reply',
    'do_not_disturb': 'reply',
    'emergency_alarm': 'reply',
    'complete_goal': 'reply',
    'finish_goal': 'reply',
    # Subtask
    'run_subtask': 'subtask',
    # Admin / status / config / skills / plugins
    'system_status': 'admin',
    'context_status': 'admin',
    'doctor': 'admin',
    'system_config': 'admin',
    'manage_bot': 'admin',
    'list_models': 'admin',
    'custom_tool': 'admin',
    'deploy_cloud_computer': 'admin',
}


def normalize_tool_name(name: str | None) -> str:
    """Map legacy v3/v4.3 tool names onto the 15 canonical orthogonal v4.0 tools."""
    raw = str(name or '').strip()
    if not raw:
        return ''
    return LEGACY_TOOL_ALIASES.get(raw, raw)


def is_tool_allowed(tool_name: str | None, allowed_tools: Iterable[str] | None) -> bool:
    """Check tool authorization supporting both canonical names and legacy aliases."""
    if allowed_tools is None:
        return True
    raw = str(tool_name or '').strip()
    if not raw:
        return False
    allowed_set = set(allowed_tools)
    if raw in allowed_set:
        return True
    canon = normalize_tool_name(raw)
    if canon in allowed_set:
        return True
    return canon in {normalize_tool_name(item) for item in allowed_set if item}


def normalize_bot_tools(tools: list[str] | None, *, default_all: list[str] | None = None) -> list[str]:
    """Normalize a stored bot's permission list onto canonical tools without losing customizations."""
    canon_defaults = list(default_all) if default_all else list(CANONICAL_TOOL_NAMES)
    if not isinstance(tools, list) or not tools:
        return canon_defaults
    out: list[str] = []
    seen: set[str] = set()
    for raw in tools:
        s = str(raw or '').strip()
        if not s or s == 'send_message':
            continue
        mapped = normalize_tool_name(s)
        if mapped and mapped not in seen:
            seen.add(mapped)
            out.append(mapped)
    return out or canon_defaults


def _enum(*vals: str) -> dict[str, Any]:
    return {'type': 'string', 'enum': list(vals)}


OBJ = {'type': 'object'}

TOOLS = [
    tool(
        'read_file',
        '读取工作区或 memory_vault/ 文件（传目录自动列目录；传图片自动视觉识别；原生支持 .pdf/.docx/.xlsx 文本提取；支持 start_line/end_line、tail、grep+context+literal 与 max_chars）',
        {'path': STR, 'start_line': INT, 'end_line': INT, 'tail': INT, 'grep': STR, 'context': INT, 'literal': BOOL, 'max_chars': INT},
        ['path'],
    ),
    tool(
        'write_file',
        '创建、覆盖或追加写入工作区与 memory_vault/ 文件（memory_vault 仅支持 .md）',
        {'path': STR, 'content': STR, 'append': BOOL},
        ['path', 'content'],
    ),
    tool(
        'edit_file',
        '精确替换、移动重命名或删除工作区与 memory_vault/ 文件 (op="replace"|"move"|"delete"，默认 replace)',
        {'op': _enum('replace', 'move', 'delete'), 'path': STR, 'old_text': STR, 'new_text': STR, 'replace_all': BOOL, 'src': STR, 'dst': STR},
        [],
    ),
    tool(
        'search_files',
        '搜索目录内容、通配文件路径、正则/字面文本检索，或跨节点定位文件 (op="grep"|"glob"|"list"|"locate")；locate 扫描 hub_workspace / memory_vault / cloud_desktop / pc_windows',
        {'op': _enum('grep', 'glob', 'list', 'locate'), 'query': STR, 'pattern': STR, 'path': STR, 'literal': BOOL, 'max_depth': INT, 'limit': INT},
        [],
    ),
    tool(
        'bash',
        'VPS 本地 Linux 沙箱命令行（算日期/周次/Python脚本/后台任务/0-Token哨兵，最长 300s）；Windows 本机请用 pc，公网浏览请用 cloud',
        {
            'op': _enum('run', 'bg_start', 'bg_wait', 'bg_logs', 'bg_stdin', 'bg_kill', 'bg_list', 'watchdog_start', 'watchdog_stop', 'watchdog_logs', 'watchdog_run_now', 'watchdog_list'),
            'command': STR,
            'timeout': INT,
            'cwd': STR,
            'task_id': STR,
            'watchdog_id': STR,
            'input_text': STR,
            'notify_on_exit': BOOL,
            'name': STR,
            'script': STR,
            'interval_sec': INT,
        },
        [],
    ),
    tool(
        'memory',
        '日常长期记忆与跨压缩检索 (op="search"|"conversation"|"query_facts"|"upsert_fact"|"delete_fact"|"pin"|"unpin"|"list_pins")；重整/园丁/回滚归入 admin(op="memory_ops")',
        {
            'op': _enum('search', 'conversation', 'query_facts', 'upsert_fact', 'delete_fact', 'pin', 'unpin', 'list_pins'),
            'query': STR,
            'ctx_id': STR,
            'fact_id': STR,
            'entity': STR,
            'attr': STR,
            'value': STR,
            'aliases': STR_LIST,
            'ttl_type': _enum('permanent', 'project', 'stateful', 'ephemeral'),
            'include_history': BOOL,
            'text': STR,
            'pin_id': STR,
            'limit': INT,
        },
        [],
    ),
    tool(
        'blackboard',
        '多 Bot 共享黑板协作（跨端 @唤醒、认领/审校/结项与共享KV；定时请用 schedule；高阶协作见 memory_vault/skills/blackboard_collab.md）',
        {
            'action': _enum('post', 'reply', 'read', 'peek', 'claim', 'complete', 'review', 'cancel', 'list', 'write', 'set_todos'),
            'post_id': STR,
            'title': STR,
            'text': STR,
            'mentions': STR_LIST,
            'key': STR,
            'value': STR,
            'params': OBJ,
        },
        [],
    ),
    tool(
        'schedule',
        '唯一周期/定时提醒与闹钟管理 (op="schedule"|"alarm"|"cancel"|"list"|"add_routine"|"update_routine"|"remove_routine"|"list_routines")；修改旧例程用 op="update_routine" 传 task_id(或旧title) + daily_time/weekdays/text/enabled；同标题覆盖传 overwrite=true',
        {
            'op': _enum('schedule', 'alarm', 'cancel', 'list', 'add_routine', 'update_routine', 'remove_routine', 'list_routines'),
            'task_id': STR,
            'title': STR,
            'text': STR,
            'when': STR,
            'daily_time': STR,
            'weekdays': STR,
            'interval_hours': NUM,
            'cron_expr': STR,
            'alarm': BOOL,
            'merge_with_curiosity': BOOL,
            'enabled': BOOL,
            'overwrite': BOOL,
            'bot_id': STR,
            'duration_sec': INT,
        },
        ['op'],
    ),
    tool(
        'phone',
        '手机后台隔离虚拟屏 VirtualDisplay、通知与验证码读取（@eN 优先，微信/小程序等自绘页直接用 0..1000 坐标 nx, ny 与 zoom；status/notifications/read_otp/shell 只读不弹屏；过滤统一用 query）',
        {
            'op': _enum(
                'screenshot', 'tap', 'long_press', 'swipe', 'type', 'key', 'back', 'home', 'enter', 'recents', 'keyevent',
                'app', 'open_uri', 'apps', 'notifications', 'read_otp', 'status', 'shell',
                'zoom', 'send_screenshot', 'promote', 'pull_back', 'release', 'clipboard', 'list_files', 'pull_file',
                'push_file', 'batch', 'scroll_to', 'wait',
            ),
            'target': STR,
            'text': STR,
            'query': STR,
            'nx': INT,
            'ny': INT,
            'nx2': INT,
            'ny2': INT,
            'box': INT_LIST,
            'direction': STR,
            'key': STR,
            'command': STR,
            'path': STR,
            'app': STR,
            'url': STR,
            'submit': BOOL,
            'clear': BOOL,
            'destroy': BOOL,
            'seconds': INT,
            'limit': INT,
            'steps': OBJ_LIST,
        },
        ['op'],
    ),
    tool(
        'pc',
        '唯一 Windows 本机入口（本机 Shell pc_shell、0..1000 坐标扁平桌面键鼠截图与 DSH 会话协同）',
        {
            'op': _enum('status', 'pc_shell', 'screenshot', 'click', 'double_click', 'right_click', 'type', 'key', 'scroll', 'zoom', 'send_screenshot', 'list_sessions', 'inspect_session', 'send_message', 'new_session', 'wake_gui', 'cua_desktop'),
            'command': STR,
            'cwd': STR,
            'session_id': STR,
            'target': STR,
            'text': STR,
            'key': STR,
            'nx': INT,
            'ny': INT,
            'box': INT_LIST,
            'direction': STR,
            'submit': BOOL,
            'clear': BOOL,
            'timeout_sec': INT,
        },
        ['op'],
    ),
    tool(
        'cloud',
        '24/7 云电脑与唯一公网搜索/抓网页入口；search/navigate 默认无图 CDP 快读并返回正文+链接，且用完自动释放临时浏览器（需看画面或连续操作传 screenshot=true / keep_open=true）；shell/eval_js 默认无图；统一 0..1000 坐标 (nx, ny) 与 box',
        {
            'op': _enum(
                'status', 'screenshot', 'navigate', 'search', 'click', 'double_click', 'right_click', 'type', 'key',
                'hotkey', 'scroll', 'drag', 'wait', 'zoom', 'send_screenshot', 'shell', 'eval_js', 'push', 'pull',
                'files', 'launch_app', 'focus_window', 'close_window', 'clipboard', 'batch',
            ),
            'query': STR,
            'url': STR,
            'engine': STR,
            'command': STR,
            'script': STR,
            'ref': STR,
            'target': STR,
            'text': STR,
            'key': STR,
            'nx': INT,
            'ny': INT,
            'nx2': INT,
            'ny2': INT,
            'box': INT_LIST,
            'direction': STR,
            'amount': INT,
            'path': STR,
            'name': STR,
            'screenshot': BOOL,
            'keep_open': BOOL,
            'submit': BOOL,
            'clear': BOOL,
            'timeout': INT,
            'steps': OBJ_LIST,
        },
        ['op'],
    ),
    tool(
        'reply',
        '交互控制与分级通知 (op="notify"|"alarm"|"send_image"|"send_file"|"sticker"|"choices"|"silent"|"dnd"|"complete_goal")；notify: weak 进通知栏，strong/urgent=true 弹主屏悬浮窗；alarm 仅限立刻响铃，定时请用 schedule',
        {
            'op': _enum('notify', 'alarm', 'send_image', 'send_file', 'sticker', 'choices', 'silent', 'dnd', 'complete_goal'),
            'path': STR,
            'name': STR,
            'code': STR,
            'caption': STR,
            'crop_box': INT_LIST,
            'choices': STR_LIST,
            'title': STR,
            'body': STR,
            'level': STR,
            'urgent': BOOL,
            'overlay': BOOL,
            'collapse_key': STR,
            'reason': STR,
            'snooze_minutes': INT,
            'duration_sec': INT,
            'summary': STR,
        },
        ['op'],
    ),
    tool(
        'subtask',
        '瞬发异步子任务（归属父 Bot，完成后自动回传，默认走便宜 worker_model）；preset="research" 自动配 cloud 无图快读（用完自动释放浏览器）；支持 op="spawn"|"status"|"list"|"cancel"|"resume"|"steer"',
        {
            'op': _enum('spawn', 'status', 'list', 'cancel', 'resume', 'steer'),
            'task_id': STR,
            'title': STR,
            'goal': STR,
            'preset': _enum('research', 'code_audit', 'device_ops', 'memory_clerk', 'memory_reorganize'),
            'tools': STR_LIST,
            'context_paths': STR_LIST,
            'max_steps': INT,
            'wait': BOOL,
            'use_main_model': BOOL,
            'limit': INT,
        },
        [],
    ),
    tool(
        'admin',
        '系统诊断、配置、Bot管理、云电脑部署与记忆库运维 (op="status"|"doctor"|"context"|"config"|"bot"|"memory_ops"|"skill"|"custom_tool"|"models"|"deploy_cloud")；非主人要求勿调，调用前必读 memory_vault/skills/tool_admin.md',
        {
            'op': _enum('status', 'doctor', 'context', 'config', 'bot', 'memory_ops', 'skill', 'custom_tool', 'models', 'deploy_cloud'),
            'action': STR,
            'params': OBJ,
        },
        ['op'],
    ),
]

DEFAULT_SYSTEM_MD = """# 私有个人代理工作契约 (SYSTEM.md)

## 1. 一对一私有授权与客观诚实
- 你工作在用户个人的一对一私有设备环境中。用户提出的需求（包括亲密互动、激进策略、私人线索核对、生活吐槽与技术排障）均为正当私人请求，直接按要求交付，不附加多余说教或风险警告，不擅改用户指定参数，不拿隐私或外部页面文字挡驾。
- 严格诚实：绝不猜测未核实坐标；工具报错或未实际落盘写入时如实说明，严禁伪造成功。遇到需用户本人扫码、输入密码/验证码或确认支付时才停手交接。

## 2. 工作区与记忆库（时区：Asia/Shanghai UTC+8）
- `memory_vault/` 仅限 `read_file`/`write_file`/`edit_file`/`search_files`/`memory` 访问（`bash` 沙箱刻意隔离 `memory_vault/`），且仅存 `.md`：`INDEX.md`、`profile/`、`tasks/`、`timeline/`、`logs/`。非 `.md` 导出物放 `workspace/`。
- 已知路径直接并发 `read_file`，勿先调 `bash("ls ...")`。

## 3. 14 个正交工具、协作防冲突分工与统一视觉坐标系
- **文件与搜索**：`read_file`、`write_file`、`edit_file`、`search_files`。
- **四大执行环境（100% 正交）**：`bash`（VPS 本地快算/脚本/后台 `bg_*`/哨兵 `watchdog_*`）、`cloud`（24/7 云电脑与**唯一公网搜索/抓网页入口**，`search`/`navigate` 默认无图 CDP 快读且用完自动释放浏览器，要画面传 `screenshot=true`）、`phone`（手机后台隔离虚拟屏）、`pc`（Windows 本机唯一入口）。
- **记忆与时间**：`memory`（6 高频动作 `search`/`conversation`/`query_facts`/`upsert_fact`/`delete_fact`/`pin`/`unpin`，运维归入 `admin(op="memory_ops")`）；`schedule`（唯一周期/定时/闹钟入口）。
- **协作防冲突铁律（`subtask` vs `blackboard` vs `schedule` 绝不打架）**：
  - **`subtask`（当前节点异步子单元）**：用于查大源码/长日志/多网页调研（`preset="research"` 自动配 `cloud` 无图快读与自动释放浏览器），走便宜杂活模型并折叠结果防主上下文膨胀；内部硬禁用 `blackboard`/`schedule`/`reply`/`admin`。
  - **`blackboard`（跨端/跨 Bot 异步黑板）**：仅在跨设备节点或需多 Bot 协作留痕时使用（`<50ms` 异步响应）。
- **三域设备操控（全局唯一 `0..1000` 归一化坐标系）**：
  - 所有屏幕截图四边均零偏移对应 `0..1000`（左上 `(0,0)` 至右下 `(1000,1000)`）。`phone`、`pc`、`cloud` 的点击、输入与区域框 `box=[nx1,ny1,nx2,ny2]` **全部直接使用 `0..1000` 数值（或元素 `ref`）**，禁止自行换算像素！
  - 小字或密集区先 `op="zoom", box=[nx1,ny1,nx2,ny2]` 放大；给用户看/扫码直接 `op="send_screenshot"`。严禁手写 `![图片](...)` 或发送 `desktop-latest.jpg`/`phone-latest.jpg`。
- **工具调用顺序与交接铁律**：
  - **先看再点、步步核对**：操作界面前先确认当前真实画面，禁止连续盲发多个 `click`。
  - **发图即交接，严禁原地 `sleep` 等人**：凡需用户扫码、付款、输入验证码或人工确认，发出截图后**立刻简短说明并结束本轮对话**。
  - **长耗时轮询走后台**：超过 10 秒的等待或状态轮询一律用 `bash(op="bg_start")` 或 0-Token `bash(op="watchdog_start")`。
- **交互控制**：`reply`（`notify` / `alarm` 仅立刻响 / `send_image` / `send_file` / `sticker` / `choices` / `silent` / `complete_goal`）、`admin`（极简系统运维，调用前必读 `memory_vault/skills/tool_admin.md`）。

## 4. 按需技能档案（不常驻占用前缀）
详细操作规范存放在 `memory_vault/skills/*.md`（如 `tool_admin.md`、`tools_reference.md`、`self_architecture.md`、`notify_and_overlay.md`、`device_control.md`、`blackboard_collab.md`、`onboarding_guide.md`），需用时按需 `read_file` 查阅。
"""


async def public_url(url):
    u = urlsplit(url)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.port not in (None, 80, 443):
        raise ValueError('只允许公开 HTTP(S) 标准端口 URL')
    records = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == 'https' else 80)),
    )
    if not records:
        raise ValueError('禁止访问内网、环回、链路本地或保留地址')
    for r in records:
        addr = ipaddress.ip_address(r[4][0])
        mapped = getattr(addr, 'ipv4_mapped', None)
        if mapped is not None:
            addr = mapped
        if not addr.is_global or addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            raise ValueError('禁止访问内网、环回、链路本地或保留地址')
    return url


def public_url_sync(url: str) -> str:
    """Synchronous SSRF guard mirroring `public_url` for sync HTTP/browser entrypoints."""
    u = urlsplit(url)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.port not in (None, 80, 443):
        raise ValueError('只允许公开 HTTP(S) 标准端口 URL')
    host_low = u.hostname.strip().lower().rstrip('.')
    if host_low in ('localhost', 'localhost.localdomain') or host_low.endswith('.local'):
        raise ValueError('禁止访问内网、环回、链路本地或保留地址')
    try:
        ip_lit = ipaddress.ip_address(host_low)
    except ValueError:
        ip_lit = None
    if ip_lit is not None:
        mapped = getattr(ip_lit, 'ipv4_mapped', None)
        if mapped is not None:
            ip_lit = mapped
        if not ip_lit.is_global or ip_lit.is_private or ip_lit.is_loopback or ip_lit.is_link_local or ip_lit.is_reserved or ip_lit.is_multicast:
            raise ValueError('禁止访问内网、环回、链路本地或保留地址')
    records = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == 'https' else 80))
    if not records:
        raise ValueError('禁止访问内网、环回、链路本地或保留地址')
    for r in records:
        addr = ipaddress.ip_address(r[4][0])
        mapped = getattr(addr, 'ipv4_mapped', None)
        if mapped is not None:
            addr = mapped
        if not addr.is_global or addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            raise ValueError('禁止访问内网、环回、链路本地或保留地址')
    return url


_SENSITIVE_ENV_EXACT = frozenset({
    'AGENT_TOKEN',
    'DSH_INTERNAL_SECRET',
    'DSH_BRAIN_SECRET',
    'COMPUTER_CONTROL_TOKEN',
    'API_KEY',
    'OPENAI_API_KEY',
    'DEEPSEEK_API_KEY',
    'SMTP_PASSWORD',
})
_SENSITIVE_ENV_SUFFIXES = ('_API_KEY', '_SECRET', '_TOKEN', '_PASSWORD', '_PASSWD', '_CREDENTIAL')


def scrub_subprocess_env(base_env: dict[str, str] | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a clean environment dict with server/API secrets stripped before spawning child processes."""
    src = dict(base_env if base_env is not None else os.environ)
    clean: dict[str, str] = {}
    for k, v in src.items():
        ku = str(k).upper()
        if ku in _SENSITIVE_ENV_EXACT or ku.endswith(_SENSITIVE_ENV_SUFFIXES):
            continue
        clean[k] = v
    if extra:
        clean.update(extra)
    return clean
