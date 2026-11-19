# Proactive Personal Agent · Codebase Map & Variable Registry (`v4.0.15` / Build 103)

> 本文件为全仓单一权威结构图谱 (`scripts/verify_codebase_map.py` 自动化校验 100% 文件覆盖与 0 废弃引用)。
> 架构已升级为 **Python FastAPI Gateway + 官方 `@deepseek-ai/*` (`0.2.2-rc.1`) Node DSH Brain Sidecar**，全面移除冗余 `Today` 模块并完成默认助手去个性化清理。

---

## 1. Architecture & Measured Memory/Storage Comparison (Cloud Server Baseline Telemetry)

| 运行组件 / 路径 | 实测常驻内存 (RSS) / 磁盘占用 | 角色说明 |
| :--- | :--- | :--- |
| Legacy Python `uvicorn main:app` (`:18190`) | **210,968 KB (~206 MB RSS)** | 网关、SQLite WAL 状态、加密信封、WS 推流、工具执行与传统大脑兜底 |
| Official `@deepseek-ai/*` (`0.2.2-rc.1`) Node DSH Sidecar (`:18199`) | **~80–135 MB RSS** (`Node v22.23.4`) | 官方 Cordis Kernel + AgentLoop + JsonlPersistence + CompactionEngine (`server/dsh_brain/`) |
| `computer_runtime.py` | **125,236 KB (~122 MB RSS)** | 24/7 云电脑隔离守护进程 |
| `Xvfb :91` | **81,600 KB (~80 MB RSS)** | 云电脑虚拟 X11 显示服务 |
| `/opt/proactive-agent` 磁盘分布 | **4.4 GB Total** | `cloud-computer` 2.0G · `data` 490M · `dist` 136M · `venv` 118M · `server` 29M |
| Cloud Host RAM 余量 | **5931 MB Total / 2188 MB Used / 3743 MB Available** | 双引擎并行后仍有 >3.5 GB 充裕内存，Node 原生 ReAct 循环与 KV 压缩可显著降低状态漂移 |

### Cross-Service Turn Loop
1. 客户端（Android 原生 / Desktop `dsh-plugin`）通过 HTTPS `/api/secure` 或 WS `/ws/client` 发送 `POST /api/chat` 到 `server/main.py` (`:18190`)。
2. 当 `BRAIN_ENGINE=dsh`（或 `auto` 且 Sidecar `/health` 就绪）时，`BrainToolsMixin._chat` 委派 `DshBrainAdapter.run_chat_turn` 向本地 `127.0.0.1:18199/turn/stream` 发起 HTTP NDJSON 流。
3. Node DSH Sidecar 通过 `OpenAiCompatibleDshAdapter` 动态按会话路由至用户配置的 OpenAI 兼容接口（例如 `https://api.example.com/v1` + `deepseek-chat`），并由 `python_tools_plugin.mjs` 通过回环 `POST http://127.0.0.1:18190/internal/dsh/tool` 调用 Python 14 项规范工具与视觉截图附件。
4. 多步调用中的中间叙述句自动降级 (`demote_stream_to_step`) 为 `step_text` 与 `turn_steps`，仅最终气泡正文写入 `stream.text`；中断/停止时若无最终正文，自动将最后一条 `step_text` + `（已停止）` 提升为助手气泡，且绝不自动关闭手机 `VirtualDisplay`。

---

## 2. Annotated Repository Directory Tree

```text
proactive-personal-agent/
├── CODEBASE_MAP.md                       # 全仓架构图谱与变量/状态注册表
├── server/
│   ├── main.py                           # FastAPI 网关入口、/health、/internal/dsh/tool、WS 与 API 路由
│   ├── brain.py                          # Brain 主协调器（生命周期、stop_immediately、enqueue_chat/chat、emit）
│   ├── assistants.py                     # 默认助手花名册与多节点 placement（已去个性化与去 Today）
│   ├── bot_identity.py                   # 助手头像与身份元数据路由
│   ├── escalation.py                     # 桌面/通知升级策略
│   ├── harness.py                        # Sandbox + 14 canonical tool dispatcher
│   ├── sandbox.py                        # 工作区路径隔离与安全命令沙箱
│   ├── security.py                       # CryptoBox (X25519 + AES-GCM) 端到端信封加密
│   ├── storage.py                        # Store（SQLite WAL + state.json 兼容镜像与 order_seq 分配）
│   ├── vault_manager.py                  # Zettelkasten Markdown 记忆金库管理
│   ├── cloud_os/                         # 24/7 云电脑隔离窗口/浏览器/tmux 子系统
│   ├── computer_*.py                     # 云电脑运行时、文件镜像、桌面截图与 CUA 路由
│   ├── core/                             # 引擎、提示词、上下文检索、同步与工具核心模块
│   │   ├── dsh_brain_adapter.py          # Python ↔ Node DSH Sidecar NDJSON 桥接与 ContextVar 隔离执行器
│   │   ├── sync_engine.py                # order_seq / version 增量同步引擎（bots/pins/config/schedules/vault）
│   │   ├── brain_tools_mixin.py          # 对话执行与 DSH 分发、final_message 原子交接
│   │   ├── brain_turn_ops_mixin.py       # 原地编辑重发 / Reroll 与前置抢占
│   │   ├── stream_publisher_mixin.py     # 实时流发布、并发域仲裁、零占位符污染控制
│   │   └── ...                           # 记忆、视觉、手机/桌面控制、斜杠指令、黑板、连续性管理
│   ├── dsh_brain/                        # 官方 @deepseek-ai/* (0.2.2-rc.1) Node Brain Sidecar
│   │   ├── package.json
│   │   ├── src/
│   │   │   ├── server.mjs                # 127.0.0.1:18199 HTTP NDJSON 服务 (/health,/turn/stream,/turn/cancel,/session/reset)
│   │   │   ├── kernel.mjs                # Cordis + SessionStore + AgentLoop + step_text 降级与中断提取
│   │   │   ├── openai_adapter.mjs        # 动态 OpenAI 兼容 SSE 适配器与工具视觉截图折叠转发
│   │   │   └── python_tools_plugin.mjs   # 14 项规范工具与动态插件 Schema 清洗注册器
│   │   └── tests/dsh_sidecar.test.mjs
│   ├── plugins/                          # 可选邮件与外部集成插件
│   ├── transport/                        # 扩展与技能动态注册表
│   └── tests/                            # Pytest 全套单元测试与 DSH Sidecar 端到端集成测试
├── desktop/dsh-plugin/                   # DSH 桌面配套插件（Apple-HIG 分栏工作台 + Win32 CUA 桥接）
│   ├── index.js / setup.mjs              # 插件入口与依赖配置
│   ├── sentinel.mjs / lib-sentinel-net.mjs # 后台长轮询哨兵与网关通信
│   ├── lib-chat-sync.mjs / lib-chat-cache.mjs # order_seq 单调游标增量合并与本地缓存
│   ├── client.js / assets/*              # 桌面端渲染、CSS Grid 过程抽屉动画与交互脚本
│   └── cua/* / lib-*-*.mjs               # Windows CUA 引擎、DSH 会话调度与双向文件同步
├── android/app/src/main/java/dev/bubuki/proactive/
│   ├── NativePulseActivity.kt            # 主宿主 Activity、State 定义与 send/stop/edit/reroll 控制
│   ├── PulseActivitySync.kt              # WS 实时流状态机、final_message 原子切换与增量拉取
│   ├── PulseDeltaEngine.kt               # order_seq 游标查询与 server ID 防误合并去重
│   ├── PulseModels.kt                    # PulseBot / PulseMessage / NativePulseState 数据契约
│   ├── PulseScreen*.kt / markdown/*      # Compose 界面与全宽垂直弹簧过程卡片 (NativeTurnProcessCard)
│   ├── FloatingVirtualDisplay*.kt        # 独立虚拟屏悬浮窗（暂停不关闭、顶白条拖边变气泡、右下角方框缩小挂起）
│   └── RootPhone* / Display*             # Root / 无线 ADB 虚拟屏生命周期与帧编解码
└── scripts/
    ├── verify_codebase_map.py            # CODEBASE_MAP.md 100% 覆盖与陈旧引用审计脚本
    ├── deploy_vps.py                     # 云端自动化发布（含 proactive-dsh-brain + proactive-agent 双服务）
    ├── publish_apk.py                    # Release APK 签名发布与 SHA/size 元数据校验
    └── bootstrap_tools.py / deploy_cloud_computer.py / run_release_gate.py
```

---

## 3. Core Variables, Sync Cursors & Environment Registry

| 标识符 / 变量 | 类型 / 默认值 | 作用域与精确职责 |
| :--- | :--- | :--- |
| `order_seq` | `int` (SQLite 单调自增) | 消息在房间/全局队列中的唯一严格有序游标（唯一允许用于 `since_seq` 增量拉取） |
| `since_seq` | `int` (query param) | `/api/sync?since_seq=...` 游标；客户端取确认服务端消息 `max(order_seq)`，禁止混用毫秒级 `seq` |
| `stream_seq` | `int` (单调递增事件号) | `stream` / `execution` / `stopped` 实时帧保序序号（防止乱序旧帧覆盖新状态） |
| `state_seq` | `int` | `SyncEngine` 状态版本号（配合 `bots_ver`/`pins_ver`/`config_ver`/`schedules_ver`/`vault_ver`） |
| `messages_epoch` | `int` | 编辑/清空/撤销重置消息时间线后的 epoch 标记，变更时触发客户端安全全量对齐 |
| `BRAIN_ENGINE` | `'auto' \| 'dsh' \| 'legacy'` | 控制 `_chat` 分发至 Node DSH Sidecar 还是内置 Python LLM 循环（VPS 部署默认 `dsh`） |
| `DSH_BRAIN_URL` | `http://127.0.0.1:18199` | Node DSH Sidecar HTTP NDJSON 地址 |
| `DSH_BRAIN_PORT` | `18199` | Node DSH Sidecar 监听端口（严格绑定 `127.0.0.1`） |
| `DSH_BRAIN_DATA_DIR` | `/opt/proactive-agent/data/dsh_sessions` | DSH `JsonlPersistence` 会话持久化目录 |
| `DSH_INTERNAL_SECRET` | `hex(32)` / env | Node Sidecar 回调 `POST /internal/dsh/tool` 时校验 `X-Internal-Secret` 密钥 |
| `CANONICAL_TOOL_NAMES` | **14 tools** (`tuple[str]`) | `read_file, write_file, edit_file, search_files, bash, web, memory, blackboard, phone, pc, cloud, reply, subtask, admin`（已移除 `today`） |
| `explicit_destroy` | `bool` (`DisplayFrameCodec`) | `FloatingVirtualDisplay` 唯一合法自动销毁信号（聊天停止/暂停阶段绝不关闭悬浮窗） |
| `dockedAsEdgeBubble` | `bool` (`FloatingVirtualDisplay`) | 顶白条向屏幕左右边缘拖曳松手后进入的 `48dp` 贴边半透明实时预览气泡状态 |
| `minimizedToPill` | `bool` (`FloatingVirtualDisplay`) | 右下角小方框（`ResizeGripView`）用力向内缩小 `< 0.38f` 后触发的侧边挂起胶囊状态 |
| `customWidthFraction` | `float?` (`0.22f..0.84f`) | 手动角点缩放记录的宽度比例；低于 `MIN_ACTIVE_WIDTH_FRACTION (0.38f)` 松手触发挂起 |
| `streamStepText` / `turnSteps` | `str` / `list` | 多步推理中自动降级的工具前导句与原生工具历史，渲染在统一过程卡片内而不污染主回复气泡 |

---

## 4. Module-by-Module File & Interface Map

### 4.1 Python Gateway & Core (`server/`)
- `server/main.py` — FastAPI gateway, `create_app`, `/health`, `/internal/dsh/tool`, `/api/secure`, `/api/sync`, `/api/status`, `/api/chat`, `/ws/client`
- `server/brain.py` — `Brain` coordinator, `enqueue_chat`, `chat`, `stop_immediately` (preserves VirtualDisplay, promotes `step_text`), `emit`
- `server/assistants.py` — `DEFAULT_ASSISTANTS` (neutral `默认助手`), auto-migration of legacy persona defaults, `ensure_assistants`, `save_assistant`
- `server/bot_identity.py` — `install_identity_routes` for bot avatars & identities
- `server/escalation.py` — `Escalation` pending queue & notification acknowledgment
- `server/harness.py` — `Harness`, `TOOLS` schema exports, sandboxed execution router
- `server/sandbox.py` — Path traversal guards & bounded bash/file sandbox primitives
- `server/security.py` — `CryptoBox` encrypted transport box (`/api/transport`, `/api/secure`)
- `server/storage.py` — `Store`, `compact_trace_list`, `normalize_node_source`, `run_storage_maintenance`, monotonic `order_seq` persistence
- `server/vault_manager.py` — `Vault` Zettelkasten markdown storage & FTS indexing
- `server/static/release.json` — Legacy/fallback static release descriptor
- `server/core/__init__.py` — Core package initializer
- `server/core/blackboard_engine.py` — `BlackboardEngine` cross-bot board posts, task claims, and schedule/cron orchestration
- `server/core/blackboard_peek.py` — Peer progress inspection helpers (`peek_bots`)
- `server/core/blackboard_scheduler_helpers.py` — Cron/delay scheduling helpers for blackboard engine
- `server/core/bot_archive.py` — `export_bot_bundle` / `import_bot_bundle` portable bot archive serialization
- `server/core/brain_proactive_mixin.py` — `BrainProactiveMixin` curiosity & sleep consolidation scheduler loop
- `server/core/brain_prompt_mixin.py` — `BrainPromptMixin` KV-cache-friendly system prompt & tool list builder
- `server/core/brain_tools_mixin.py` — `BrainToolsMixin._chat` DSH Sidecar delegation, legacy loop fallback, `final_message` handoff
- `server/core/brain_turn_ops_mixin.py` — `BrainTurnOpsMixin` edit-and-rerun, reroll, preemption, DSH session reset
- `server/core/bridge_routes.py` — `register_bridge_routes` desktop DSH sentinel WS & local bridge RPC endpoints
- `server/core/canonical_tool_handlers.py` — Unified 14 canonical tool execution handlers, weak/strong notification routing & PC FS RPC staging
- `server/core/clock_wheel.py` — `UnifiedClockWheel` 1Hz background timer/deadline/watchdog wheel
- `server/core/cloud_computer_deployer.py` — Automated VPS cloud computer provisioning helpers
- `server/core/cloud_desktop_controller.py` — `CloudDesktopControllerMixin` cloud desktop viewport/control coordination
- `server/core/cloud_stuck_detector.py` — Stagnant GUI loop detection for cloud desktop operations
- `server/core/compaction_service.py` — `CompactionService` OpenCode/DSH Prefix-Reusing context compaction & working ledger checkpointing
- `server/core/long_term_memory.py` — `LongTermMemoryEngine` (Hindsight Experiences + OpenViking L0/L1/L2 + Graphiti Bi-Temporal recall & harness transcript builder)
- `server/core/context_search.py` — `ContextSearchEngine` SQLite FTS5 long-term conversation recall
- `server/core/curiosity_coalesce_helpers.py` — Periodic routine/blackboard coalescing helpers shared with `CuriosityEngine`
- `server/core/curiosity_engine.py` — `CuriosityEngine` idle inspection & sleep memory consolidation
- `server/core/desktop_controller.py` — `DesktopControllerMixin` unified desktop action dispatcher
- `server/core/dsh_brain_adapter.py` — `DshBrainAdapter` HTTP NDJSON client, `/internal/dsh/tool` executor, `step_text` demotion bridge
- `server/core/dsh_bridge_mixin.py` — `DshBridgeMixin` Windows DSH WebSocket RPC helpers
- `server/core/external_profile_db.py` — `ExternalProfileDB` isolated SQLite profile store & zero-context AI prompt builder
- `server/core/interconnect_router.py` — `InterconnectRouterMixin` cross-bot `send_message` wakeup router
- `server/core/interfaces.py` — Internal protocol & typing interfaces
- `server/core/llm_client.py` — `LLMClient` SSE streaming & fallback client
- `server/core/memory_engine.py` — `MemoryEngine` pins, user impression profiles, token stats
- `server/core/notification_hub.py` — `NotificationHub` mobile notification ingestion
- `server/core/observation_masker.py` — `maybe_mask_room_observations` pruning stale GUI trees from context
- `server/core/onboarding_service.py` — Provider endpoint inference (`infer_provider_defaults`) & neutral welcome flow
- `server/core/pc_desktop_controller.py` — `PcDesktopControllerMixin` Windows CUA action wrapper
- `server/core/phone_controller.py` — `PhoneControllerMixin` Android VirtualDisplay remote RPC controller
- `server/core/phone_ui_formatter.py` — Compact phone UI tree formatting helpers
- `server/core/phone_vision_helpers.py` — Phone screenshot crop/zoom helpers
- `server/core/proactive_gate.py` — `ProactiveGate` quiet hours & annoyance throttling
- `server/core/profile_share_routes.py` — Read-only external AI profile sharing & token management HTTP routes
- `server/core/prompt_guards.py` — Prompt guardrails (universal calibration, generic by default, `<persona>` opt-in)
- `server/core/scheduler_engine.py` — `SchedulerEngine` cron/alarm persistence & firing
- `server/core/search_engine.py` — Web search / fetch engine helpers
- `server/core/skill_templates.py` — Built-in modular YAML-frontmatter Markdown skill templates (`self_architecture`, `notify_and_overlay`, `device_control`, `blackboard_collab`, `onboarding_guide`)
- `server/core/slash_commands.py` — `/goal`, `/compact`, `/mcp`, `/memory`, `/models`, `/undo`, `/redo`, `/clear`, `/pin`, `/status`, `/doctor`, `/help` + DSH reset hooks
- `server/core/sqlite_curiosity_mixin.py` — SQLite storage helpers for curiosity & deadlines
- `server/core/sqlite_state.py` — `SqliteStateDB` WAL schema & ACID transactions
- `server/core/storage_hygiene.py` — Periodic SQLite vacuum & artifact cleanup
- `server/core/stream_publisher_mixin.py` — `StreamPublisherMixin` live stream publisher & domain concurrency resolver
- `server/core/subtask_runner.py` — Isolated read-only subagent runner (`subtask` tool)
- `server/core/sync_engine.py` — `SyncEngine` cursor (`order_seq`) & monotonic state version coordinator
- `server/core/task_continuity.py` — `TaskContinuityManager` goal mode contract & foreground chain continuity
- `server/core/tool_handlers.py` — `ToolHandlersMixin` domain tool adapter methods
- `server/core/tool_schemas.py` — `CANONICAL_TOOL_NAMES` (14 tools) & schema registry
- `server/core/turn_history.py` — `build_compact_turn_step`, `append_turn_step`, `expand_stored_turn_steps`
- `server/core/turn_ledger.py` — `TurnLedgerManager` persistent turn journal & interrupt recovery
- `server/core/vfs_router.py` — `PerNodePhoneRegistry` multi-node phone state tracker
- `server/core/vision_engine.py` — `VisionEngine` multimodal image inspection
- `server/core/xml_tool_parser.py` — XML tool tag fallback parser

### 4.2 Official `@deepseek-ai/*` (`0.2.2-rc.1`) Node DSH Sidecar (`server/dsh_brain/`)
- `server/dsh_brain/package.json` — ESM package manifest (`type: "module"`)
- `server/dsh_brain/src/server.mjs` — `startDshSidecarServer` (`127.0.0.1:18199`) HTTP NDJSON server
- `server/dsh_brain/src/kernel.mjs` — `createDshSidecarKernel` Cordis + `@deepseek-ai/dsh-agent` + `step_text` demotion + abort capture
- `server/dsh_brain/src/openai_adapter.mjs` — `OpenAiCompatibleDshAdapter` per-session dynamic OpenAI SSE adapter & tool vision forwarding
- `server/dsh_brain/src/python_tools_plugin.mjs` — `DEFAULT_TOOL_SCHEMAS`, `sanitizeSchemaForDsh`, `/internal/dsh/tool` bridge
- `server/dsh_brain/tests/dsh_sidecar.test.mjs` — Native Node unit tests for tool loop & mid-turn abort

### 4.3 Cloud OS & Plugins (`server/cloud_os/`, `server/plugins/`, `server/transport/`, `server/computer_*.py`)
- `server/cloud_os/__init__.py` — Cloud OS subsystem exports
- `server/cloud_os/agent_browser.py` — Headless/CDP browser automation for Cloud Computer
- `server/cloud_os/bash_session.py` — Persistent bash PTY session controller
- `server/cloud_os/browser_dom_scripts.py` — Injected browser DOM inspection scripts
- `server/cloud_os/subsystem.py` — Cloud OS subsystem lifecycle coordinator
- `server/cloud_os/tmux_terminal.py` — Tmux terminal wrapper
- `server/cloud_os/window_manager.py` — X11 window management helpers
- `server/computer_api.py` — `ComputerCoordinator` & `/api/computer/*` FastAPI routes
- `server/computer_browser.py` — Cloud browser session API helpers
- `server/computer_client.py` — HTTP client from gateway to Cloud Computer runtime
- `server/computer_desktop.py` — Cloud desktop screenshot & input handlers
- `server/computer_files.py` — `WorkspaceFiles`, `/api/files/*`, `mirror_upload_across_nodes`
- `server/computer_models.py` — Pydantic request/response models for Cloud Computer
- `server/computer_runtime.py` — Standalone Cloud Computer daemon
- `server/computer_security.py` — Cloud runtime token authentication
- `server/computer_shell.py` — Cloud shell command execution helpers
- `server/plugins/gmail_plugin.py` — Optional Gmail integration plugin
- `server/plugins/integration_hub.py` — Optional 3rd-party plugin hub
- `server/transport/__init__.py` — Transport package exports
- `server/transport/extension_registry.py` — Dynamic workspace skills & custom tools loader

### 4.4 Desktop DSH Companion Plugin (`desktop/dsh-plugin/`)
- `desktop/dsh-plugin/package.json` — Desktop plugin manifest
- `desktop/dsh-plugin/index.js` — Plugin entry registering sentinel, local HTTP routes, and UI assets
- `desktop/dsh-plugin/setup.mjs` — Interactive/CLI config setup helper
- `desktop/dsh-plugin/bridge_config.json` — Default local bridge configuration template
- `desktop/dsh-plugin/sentinel.mjs` — Background WebSocket/HTTP sentinel maintaining PC connectivity
- `desktop/dsh-plugin/lib-sentinel-net.mjs` — Encrypted/token-authenticated network helpers (`smartCloudFetchJson`)
- `desktop/dsh-plugin/lib-chat-cache.mjs` — Local disk cache for PC room messages & bots roster
- `desktop/dsh-plugin/lib-chat-sync.mjs` — `waitForCloudBotReply` (`order_seq` cursor), `applyCloudSyncPayload`, `applyInboxSyncVersions`
- `desktop/dsh-plugin/lib-local-routes.mjs` — Local `127.0.0.1:18195` HTTP route handlers
- `desktop/dsh-plugin/lib-file-bridge.mjs` — Desktop ↔ Cloud/VPS file sync routes
- `desktop/dsh-plugin/lib-dsh-sessions.mjs` — Windows DSH workspace session inspection & dispatch
- `desktop/dsh-plugin/lib-dsh-pc-tools.mjs` — Windows local PC shell & FS tool execution
- `desktop/dsh-plugin/cua-bridge.mjs` — Bridge from sentinel RPC to Win32 CUA engine
- `desktop/dsh-plugin/cua/index.mjs` — CUA action router
- `desktop/dsh-plugin/cua/win32-engine.mjs` — Windows UIA & native screenshot/input engine
- `desktop/dsh-plugin/cua/tree-utils.mjs` — Windows Accessibility UI tree serializer
- `desktop/dsh-plugin/cua/hud-overlay.mjs` — Windows floating CUA status HUD overlay
- `desktop/dsh-plugin/cua/README.md` — CUA subsystem documentation
- `desktop/dsh-plugin/client.js` — Main split-pane UI controller & direct CSS Grid drawer toggle
- `desktop/dsh-plugin/assets/client-ui.js` — Vault drawer, Bot studio & sync payload UI adapters
- `desktop/dsh-plugin/assets/dsb-render.js` — Markdown/LaTeX/SVG renderer, `renderTurnProcessHtml` drawer markup & `1/2/3/4` variant switcher
- `desktop/dsh-plugin/assets/dsb-ui.css` — Apple-HIG palette, high-contrast user bubbles & `grid-template-rows: 0fr -> 1fr` drawer transitions

### 4.5 Android Kotlin/Java Client (`android/app/src/main/java/dev/bubuki/proactive/`)
- `android/app/src/main/assets/transport.json` — Bundled non-secret transport endpoint template
- `android/app/src/main/java/dev/bubuki/proactive/PulseApplication.java` — Application bootstrap
- `android/app/src/main/java/dev/bubuki/proactive/LauncherActivity.java` — Launcher entry activity
- `android/app/src/main/java/dev/bubuki/proactive/MainActivity.java` — Legacy WebView/settings container & safe error formatting
- `android/app/src/main/java/dev/bubuki/proactive/NativePulseActivity.kt` — Primary Jetpack Compose host & `NativePulseModel` chat actions (`send`, `stop`, `editMessageAndResend`)
- `android/app/src/main/java/dev/bubuki/proactive/NativePhoneActivity.kt` — Root/ADB Phone diagnostic activity
- `android/app/src/main/java/dev/bubuki/proactive/PulseActivitySync.kt` — `onRealtimeEvent`, atomic `mergeHandoffMessage`, `refresh`, `refreshStreamOnly`
- `android/app/src/main/java/dev/bubuki/proactive/PulseActivityBotOps.kt` — Bot CRUD, memory vault ops, settings & model listing
- `android/app/src/main/java/dev/bubuki/proactive/PulseActivityMedia.kt` — Attachments, cloud desktop streaming, permission launchers
- `android/app/src/main/java/dev/bubuki/proactive/PulseDeltaEngine.kt` — `buildSyncQuery` (`since_seq = maxOrderSeq`), `deduplicateAssistantList` (`isEphemeralMsgId`), `handleStateSyncEvent`
- `android/app/src/main/java/dev/bubuki/proactive/PulseModels.kt` — `PulseBot`, `PulseMessage` (`turnSteps`), `NativePulseState` (`streamStepText`, `streamTurnSteps`)
- `android/app/src/main/java/dev/bubuki/proactive/PulseScreenChat.kt` — Room header, message list & composer layout
- `android/app/src/main/java/dev/bubuki/proactive/PulseScreenChatItems.kt` — `NativeMessage` rendering unified `NativeTurnProcessCard`
- `android/app/src/main/java/dev/bubuki/proactive/PulseScreenPanels.kt` — `NativeLiveStreamBubble` (unified `NativeTurnProcessCard`), Vault & Schedule panels
- `android/app/src/main/java/dev/bubuki/proactive/PulseScreenToolTrace.kt` — `NativeTurnProcessCard` full-width vertical spring process card
- `android/app/src/main/java/dev/bubuki/proactive/PulseScreenBotEditor.kt` — Bot editor sheet UI
- `android/app/src/main/java/dev/bubuki/proactive/PulseScreenCloudPanel.kt` — Cloud computer viewer & interactive control panel
- `android/app/src/main/java/dev/bubuki/proactive/PulseScreenComposer.kt` — Bottom composer bar & slash menu
- `android/app/src/main/java/dev/bubuki/proactive/PulseScreenFormatting.kt` — Humanized tool names & wiki-link formatters
- `android/app/src/main/java/dev/bubuki/proactive/PulseScreenHelpers.kt` — Shared UI composition helpers
- `android/app/src/main/java/dev/bubuki/proactive/PulseScreenModals.kt` — Action menus & confirmation modals
- `android/app/src/main/java/dev/bubuki/proactive/PulseScreenStickers.kt` — Sticker & chat image viewer components
- `android/app/src/main/java/dev/bubuki/proactive/NativePulseScreen.kt` — Root Compose navigation stage
- `android/app/src/main/java/dev/bubuki/proactive/NativePulseIcons.kt` — Custom vector icon set
- `android/app/src/main/java/dev/bubuki/proactive/BotAvatarView.kt` — Procedural shape bot avatars
- `android/app/src/main/java/dev/bubuki/proactive/OnboardingApiGate.kt` — First-run API key & endpoint onboarding gate
- `android/app/src/main/java/dev/bubuki/proactive/PulseAppleDesign.kt` — Apple HIG spring specs & press feedback modifiers
- `android/app/src/main/java/dev/bubuki/proactive/PulseMotionAndPower.kt` — Lifecycle/battery-aware animation gating
- `android/app/src/main/java/dev/bubuki/proactive/PulseTheme.kt` — Light/Dark semantic color palette
- `android/app/src/main/java/dev/bubuki/proactive/PulseApkUpdater.kt` — In-app APK update checker, fast downloader & installer
- `android/app/src/main/java/dev/bubuki/proactive/UiPreferences.kt` — Shared Compose UI preference defaults
- `android/app/src/main/java/dev/bubuki/proactive/markdown/MarkdownModel.kt` — CommonMark AST model
- `android/app/src/main/java/dev/bubuki/proactive/markdown/MarkdownText.kt` — Compose Markdown + code + math renderer
- `android/app/src/main/java/dev/bubuki/proactive/markdown/StreamingMarkdown.kt` — Incremental Markdown closure helpers
- `android/app/src/main/java/dev/bubuki/proactive/FloatingVirtualDisplay.kt` — System overlay lifecycle (`shouldAutoCloseVirtualDisplay`, `dockToEdgeBubble`, `minimizeToSidePill`)
- `android/app/src/main/java/dev/bubuki/proactive/FloatingVirtualDisplayChrome.kt` — Pearl bezels, `createTopGrabberHandle`, `createPearlEdgeBubbleView`, `createPearlDockedPillView`
- `android/app/src/main/java/dev/bubuki/proactive/FloatingVirtualDisplayControls.kt` — `PipSpringPhysics`, square `ResizeGripView`, corner shrink-to-suspend, edge bubble drag
- `android/app/src/main/java/dev/bubuki/proactive/FloatingVirtualDisplayCompose.kt` — In-app PiP fallback widget
- `android/app/src/main/java/dev/bubuki/proactive/VirtualDisplayAppDrawer.kt` — Floating launchable apps picker
- `android/app/src/main/java/dev/bubuki/proactive/VirtualDisplayUiTreeHelper.java` — Privileged accessibility tree snapshot serializer
- `android/app/src/main/java/dev/bubuki/proactive/AgentForegroundService.java` — Persistent WS channel, command router & virtual display guard
- `android/app/src/main/java/dev/bubuki/proactive/AgentActionReceiver.java` — Notification quick action broadcast receiver
- `android/app/src/main/java/dev/bubuki/proactive/AgentNotificationListener.java` — Optional Android notification listener service
- `android/app/src/main/java/dev/bubuki/proactive/BootReceiver.java` — Device boot service starter
- `android/app/src/main/java/dev/bubuki/proactive/OverlayController.java` — Display-0 strong-notification floating modal card with open-chat/ack/snooze actions
- `android/app/src/main/java/dev/bubuki/proactive/PrivilegeManager.java` — Root `su` & wireless ADB privilege IPC manager
- `android/app/src/main/java/dev/bubuki/proactive/AdbKeyPairStore.java` — Wireless ADB RSA keypair storage
- `android/app/src/main/java/dev/bubuki/proactive/WirelessAdbController.java` — Android native ADB TLS pairing & connection
- `android/app/src/main/java/dev/bubuki/proactive/WirelessAdbPairingReceiver.java` — Pairing notification input handler
- `android/app/src/main/java/dev/bubuki/proactive/DisplayRuntime.java` — SurfaceFlinger / ImageReader virtual display daemon
- `android/app/src/main/java/dev/bubuki/proactive/DisplayFrameCodec.java` — VirtualDisplay state JSON & WebP/JPEG frame encoder
- `android/app/src/main/java/dev/bubuki/proactive/DisplayInputHelper.java` — Injected touch/key/text input event dispatcher
- `android/app/src/main/java/dev/bubuki/proactive/DisplayTaskContainment.java` — Isolated task stack containment for VirtualDisplay
- `android/app/src/main/java/dev/bubuki/proactive/DisplayTestActivity.java` — Diagnostic virtual display test activity
- `android/app/src/main/java/dev/bubuki/proactive/RootDisplayServer.java` — Privileged UNIX socket server for DisplayRuntime
- `android/app/src/main/java/dev/bubuki/proactive/RootCommands.java` — Privileged shell command execution helper
- `android/app/src/main/java/dev/bubuki/proactive/RootClipboard.java` — Privileged clipboard reader/writer
- `android/app/src/main/java/dev/bubuki/proactive/RootClipboardBroker.java` — Clipboard IPC broker
- `android/app/src/main/java/dev/bubuki/proactive/PulseAccessibilityService.kt` — Multi-display accessibility tree inspector & node actions
- `android/app/src/main/java/dev/bubuki/proactive/RootPhoneHarness.kt` — VirtualDisplay coordinator (`command`, `status`, `markDisplayDestroyed`)
- `android/app/src/main/java/dev/bubuki/proactive/RootPhoneHarnessActions.kt` — Pure UI node tap/type action execution
- `android/app/src/main/java/dev/bubuki/proactive/RootPhoneHarnessLoop.kt` — Live preview poller & local autonomous phone task loop
- `android/app/src/main/java/dev/bubuki/proactive/RootPhoneAgent.kt` — Local Root phone VLM fallback agent
- `android/app/src/main/java/dev/bubuki/proactive/RootPhoneDeviceShell.kt` — Device telemetry & shell helpers
- `android/app/src/main/java/dev/bubuki/proactive/RootPhoneShellFiles.kt` — Local file pull/push helpers
- `android/app/src/main/java/dev/bubuki/proactive/RootPhoneScreen.kt` — Diagnostic VirtualDisplay Compose screen
- `android/app/src/main/java/dev/bubuki/proactive/RootPhoneService.kt` — Foreground keep-alive service for active phone tasks and background floating virtual display overlay
- `android/app/src/main/java/dev/bubuki/proactive/CloudFiles.java` — Android file upload/download helpers
- `android/app/src/main/java/dev/bubuki/proactive/AvatarImages.java` — Local avatar image decoder/cache
- `android/app/src/main/java/dev/bubuki/proactive/Io.java` — Low-level stream I/O helpers
- `android/app/src/main/java/dev/bubuki/proactive/SecurePrefs.java` — EncryptedSharedPreferences wrapper
- `android/app/src/main/java/dev/bubuki/proactive/SecureTransport.java` — X25519 + AES-GCM OkHttp & WebSocket client

### 4.6 Operations & Deployment Scripts (`scripts/`)
- `scripts/deploy_vps.py` — Packages Python server + Node `dsh_brain` Sidecar, configures `proactive-dsh-brain.service` & `proactive-agent.service` on the target host
- `scripts/publish_apk.py` — Validates, uploads, and verifies Release APK + `release.json` on the target host
- `scripts/verify_codebase_map.py` — Automated verifier checking 100% file coverage and zero stale removed entries in `CODEBASE_MAP.md`
- `scripts/bootstrap_tools.py` — Local dev toolchain setup script
- `scripts/deploy_cloud_computer.py` — Remote Cloud Computer runtime installer
- `scripts/run_release_gate.py` — Pre-release sanity gate script
