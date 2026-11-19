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
    'web',
    'memory',
    'blackboard',
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
    # Web
    'web_search': 'web',
    'web_fetch': 'web',
    # Memory & archives
    'memory_search': 'memory',
    'conversation_search': 'memory',
    'ctx_search': 'memory',
    'context_search': 'memory',
    'memory_pin': 'memory',
    # Blackboard / schedules / bots
    'peek_bots': 'blackboard',
    'schedule_task': 'blackboard',
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


TOOLS = [
    tool(
        'read_file',
        '读取工作区或 memory_vault/ 文件（传目录自动列目录；传图片自动视觉识别；支持 start_line/end_line）',
        {'path': STR, 'start_line': INT, 'end_line': INT},
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
        {'op': STR, 'path': STR, 'old_text': STR, 'new_text': STR, 'replace_all': BOOL, 'src': STR, 'dst': STR},
        [],
    ),
    tool(
        'search_files',
        '搜索目录内容、通配文件路径或正则检索文本 (op="grep"|"glob"|"list")',
        {'op': STR, 'query': STR, 'pattern': STR, 'path': STR, 'max_depth': INT, 'limit': INT},
        [],
    ),
    tool(
        'bash',
        '沙箱命令行、后台任务与0-Token盯盘哨兵 (op="run"|"bg_start"|"bg_logs"|"bg_stdin"|"bg_kill"|"bg_list"|"watchdog_start"|"watchdog_stop"|"watchdog_logs"|"watchdog_run_now"|"watchdog_list")',
        {
            'op': STR,
            'action': STR,
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
            'bot_id': STR,
        },
        [],
    ),
    tool(
        'web',
        '联网搜索或抓取解析网页正文 (op="search"|"fetch"；传 url 自动 fetch，传 query 自动 search)',
        {'op': STR, 'query': STR, 'url': STR, 'limit': INT, 'max_results': INT, 'max_chars': INT},
        [],
    ),
    tool(
        'memory',
        '检索长期记忆 vault、历史对话归档、工具大输出缓存与置顶记忆 (op="search"|"conversation"|"ctx"|"pin"|"unpin"|"list_pins")',
        {
            'op': STR,
            'action': STR,
            'query': STR,
            'ctx_id': STR,
            'bot_id': STR,
            'limit': INT,
            'text': STR,
            'pin_id': STR,
            'source': STR,
        },
        [],
    ),
    tool(
        'blackboard',
        '事件驱动共享黑板、@同事进度查看与定时/周期任务编排 (action="post"|"read"|"peek"|"claim"|"complete"|"review"|"deliver"|"approve"|"reject"|"schedule"|"alarm"|"cancel"|"list"|"add_routine"|"update_routine"|"remove_routine"|"list_routines"|"clear_done"|"mute"|"unmute")；支持 cron_expr 与 merge_with_curiosity=True 并入好奇心周期',
        {
            'action': STR,
            'op': STR,
            'post_id': STR,
            'title': STR,
            'content': STR,
            'text': STR,
            'goal': STR,
            'mentions': STR_LIST,
            'priority': STR,
            'steer': BOOL,
            'artifact_path': STR,
            'review_by': STR,
            'verdict': STR,
            'target': STR,
            'summary': STR,
            'limit': INT,
            'since_seq': INT,
            'include_archived': BOOL,
            'kind': STR,
            'watch': STR,
            'trigger_value': STR,
            'watch_interval_sec': INT,
            'notify_bot': STR,
            'when': STR,
            'alarm': BOOL,
            'ring_alarm': BOOL,
            'mode': STR,
            'daily_time': STR,
            'weekdays': STR,
            'interval_hours': NUM,
            'merge_with_curiosity': BOOL,
            'enabled': BOOL,
            'delay_minutes': INT,
            'run_at': STR,
            'cron_expr': STR,
            'bot_id': STR,
            'prompt': STR,
            'label': STR,
            'task_id': STR,
            'duration_sec': INT,
        },
        [],
    ),
    tool(
        'phone',
        '手机隔离虚拟屏 VirtualDisplay（纯视觉 0..1000 坐标 nx, ny）、通知收敛、验证码读取(op="read_otp")、输入法策略(op="ime_policy")、zoom 局部放大与 send_screenshot',
        {
            'op': STR,
            'target': STR,
            'ref': STR,
            'text': STR,
            'limit': INT,
            'nx': INT,
            'ny': INT,
            'nx2': INT,
            'ny2': INT,
            'direction': STR,
            'key': STR,
            'command': STR,
            'path': STR,
            'app': STR,
            'uri': STR,
            'policy': STR,
            'submit': BOOL,
            'clear': BOOL,
            'force': BOOL,
            'duration_ms': INT,
            'steps': OBJ_LIST,
            'box': INT_LIST,
            'filter_package': STR,
            'filter_app': STR,
            'filter_keyword': STR,
            'reason': STR,
            'send_to_chat': BOOL,
        },
        ['op'],
    ),
    tool(
        'pc',
        'Windows 电脑端 DSH 协同（桌面状态、纯视觉 0..1000 坐标 nx, ny 键鼠截图、zoom 局部放大、本机 Shell 与 Session 派单）',
        {
            'op': STR,
            'prompt': STR,
            'session_id': STR,
            'target': STR,
            'ref': STR,
            'command': STR,
            'cwd': STR,
            'path': STR,
            'text': STR,
            'key': STR,
            'nx': INT,
            'ny': INT,
            'direction': STR,
            'box': INT_LIST,
            'submit': BOOL,
            'clear': BOOL,
            'limit': INT,
            'timeout_sec': INT,
        },
        ['op'],
    ),
    tool(
        'cloud',
        '24/7 共享 Linux 云电脑（桌面、浏览器、Shell、zoom 局部放大、send_screenshot、文件收发）；统一采用 0..1000 归一化坐标 (nx, ny) 与区域框 box=[nx1,ny1,nx2,ny2]',
        {
            'op': STR,
            'ref': STR,
            'box': INT_LIST,
            'zoom_box': INT_LIST,
            'path': STR,
            'name': STR,
            'content': STR,
            'data_b64': STR,
            'nx': INT,
            'ny': INT,
            'nx2': INT,
            'ny2': INT,
            'dx': INT,
            'dy': INT,
            'target': STR,
            'text': STR,
            'command': STR,
            'is_input': BOOL,
            'goal': STR,
            'key': STR,
            'app': STR,
            'url': STR,
            'query': STR,
            'direction': STR,
            'clear': BOOL,
            'submit': BOOL,
            'click': BOOL,
            'timeout': INT,
            'duration_ms': INT,
            'seconds': INT,
            'steps': OBJ_LIST,
            'full_page': BOOL,
        },
        ['op'],
    ),
    tool(
        'reply',
        '交互控制与分级通知 (op="notify"|"alarm"|"send_image"|"send_file"|"choices"|"silent"|"dnd"|"complete_goal")；notify 分级：weak 进通知栏，strong/urgent=true/overlay=true 弹手机主屏全局悬浮窗；需附带快捷点击按钮时调用 op="choices"，choices 为 2~3 个用户第一人称简短指令，不要使用 AI 自称，不需选项则别调用',
        {
            'op': STR,
            'path': STR,
            'name': STR,
            'caption': STR,
            'choices': STR_LIST,
            'prompt': STR,
            'title': STR,
            'body': STR,
            'level': STR,
            'urgent': BOOL,
            'overlay': BOOL,
            'collapse_key': STR,
            'reason': STR,
            'snooze_minutes': INT,
            'minutes': INT,
            'duration_sec': INT,
            'summary': STR,
            'verified_evidence': STR,
        },
        ['op'],
    ),
    tool(
        'subtask',
        '在独立隔离上下文运行聚焦子任务（查源码/长日志/网页核对）并折叠返回精简结论，防主对话膨胀',
        {'goal': STR, 'mode': STR, 'context_paths': STR_LIST, 'max_steps': INT},
        ['goal'],
    ),
    tool(
        'admin',
        '系统运维、配置、Bot管理、Skill/插件、模型列表与闲置VPS一键部署云电脑 (op="status"|"doctor"|"context"|"config"|"bot"|"skill"|"custom_tool"|"models"|"deploy_cloud")',
        {
            'op': STR,
            'action': STR,
            'key': STR,
            'value': STR,
            'bot_id': STR,
            'name': STR,
            'title': STR,
            'description': STR,
            'instructions': STR,
            'model': STR,
            'engine': STR,
            'color': STR,
            'shape': STR,
            'curiosity_enabled': BOOL,
            'notify': BOOL,
            'home_node': STR,
            'tools': STR_LIST,
            'code': STR,
            'content': STR,
            'parameters_json': STR,
            'host': STR,
            'port': INT,
            'username': STR,
            'password': STR,
            'private_key': STR,
            'setup_swap': BOOL,
            'api_key': STR,
            'base_url': STR,
            'auto_compact_tokens': INT,
            'curiosity_interval_minutes': INT,
            'quiet_start_hour': INT,
            'quiet_end_hour': INT,
            'source': STR,
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

## 3. 14 个正交工具与统一视觉坐标系
- **文件与搜索**：`read_file`、`write_file`、`edit_file`、`search_files`。
- **命令与检索**：`bash`（含 `bg_*` 与 0-Token `watchdog_*`）、`web`、`memory`、`subtask`（隔离聚焦调研）。
- **跨端协同**：`blackboard`（事件黑板 `<50ms`、跨端 `@同事` 与定时闹钟）。
- **三域设备操控（全局唯一 `0..1000` 归一化坐标系）**：
  - 所有屏幕截图四边均零偏移对应 `0..1000`（左上 `(0,0)` 至右下 `(1000,1000)`）。`phone`（手机后台隔离虚拟屏）、`pc`（Windows 桌面）、`cloud`（24/7 云电脑）的点击、输入与区域框 `box=[nx1,ny1,nx2,ny2]` **全部直接使用 `0..1000` 数值（或元素 `ref`）**，禁止自行换算像素或用 `bash` 凑坐标！
  - 小字或密集区先 `op="zoom", box=[nx1,ny1,nx2,ny2]` 放大；给用户看/扫码直接 `op="send_screenshot"`（大二维码或关键页面不确信时可不带 `box` 发整屏）。严禁手写 `![图片](...)` 或发送 `desktop-latest.jpg`/`phone-latest.jpg`。
- **交互控制**：`reply`（`notify` / `alarm` / `send_image` / `send_file` / `choices` / `silent` / `complete_goal`）、`admin`。需要提供快捷点击按钮时可调用 `reply(op="choices", choices=[...])`，选项必须用用户点击口吻（如`"继续"`），不用 AI 自称。

## 4. 按需技能档案（不常驻占用前缀）
详细操作规范存放在 `memory_vault/skills/*.md`（如 `self_architecture.md`、`notify_and_overlay.md`、`device_control.md`、`blackboard_collab.md`、`onboarding_guide.md`），需用时按需 `read_file` 查阅。
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
