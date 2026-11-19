# DSH 替代老版本 Python 大脑（保留成熟三端 UI）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在完整保留现有成熟 Apple HIG Android UI、FloatingVirtualDisplay（Mi 极简手势悬浮窗）、Linux 云端桌面（Xvfb+CDP）与桌面端 DSH 插件的前提下，仅将 `server/brain.py` 及其手动多轮工具循环替换为基于 `@deepseek-ai/*`（DSH）的原生 Harness 大脑引擎。

**Architecture:** 采用 **「Python Gateway (`server/main.py`) + Node DSH Brain Sidecar (`server/dsh_brain/`) + 原生物理执行器」** 双进程热插拔架构：`main.py` 继续向 Android / `desktop/dsh-plugin` 暴露完全兼容的 `/api/sync`、`/ws/client`、`SecureTransport`（AES-256-GCM）与 `order_seq` / `state_seq` 契约；对话推理、Tool-Calling 循环、上下文压缩、子代理与跨步历史记忆交给同一台主机（`127.0.0.1:18088`）上的 DSH Sidecar，DSH 通过反向工具回调调用既有的 `PhoneControllerMixin` / `DesktopControllerMixin` / `CloudDesktop` / `MemoryEngine` / `BlackboardEngine` / `CuriosityEngine`。

**Tech Stack:** Node.js + TypeScript (`@deepseek-ai/harness` / DSH core)、FastAPI (`server/main.py`)、SQLite WAL (`pulse_state.db`)、Kotlin + Jetpack Compose (`android/`)。

---

## Global Constraints

1. **绝不推翻成熟三端 UI**：Android Apple HIG 界面（`NativePulseScreen.kt` / `PulseAppleDesign.kt` / `FloatingVirtualDisplay*`）和 Windows DSH 侧边栏（`desktop/dsh-plugin/assets/`）保持原貌并仅做增量渲染适配，禁止重写成简陋占位界面。
2. **零硬熔断 & 零毒兜底**：DSH 替代大脑不设置 24 步硬上限或 30 分钟工具熔断；仅允许 `nudge` 级别提示或明确 `ok: false` 诚实报错。
3. **不保留无意义冗余**：界面与默认系统工具不再暴露 `Today` 卡片/菜单入口；`CloudDesktop` 禁止自动杀停 Chromium 或绘制假桌面图标装饰。
4. **物理端按 `source`/`device_id` 严格隔离**：双手机与电脑独立通道，`phone` 操控缺 `device` 且多机在线时明确报错，不猜设备不串 Bot。
5. **虚拟屏原生无损全屏接管 (`promote` / `pull_back`)**：涉及支付/验证码等安全场景时保持先切前台主屏由用户接管，挂起为侧边珍珠胶囊（按钮变为橙色 `我处理好了，继续`），结束后无损搬回虚拟屏。

## Review Focus

1. **DSH Sidecar 崩溃/重启后的状态连续性**：Sidecar 重启后必须能从 SQLite `pulse_state.db` 按 `(bot_id, order_seq)` 恢复多轮工具历史，不能丢上下文。
2. **即时中断 (`stop_immediately`) 0ms 响应**：Android/DSH 点击停止时，`main.py` 必须立刻向 Sidecar 发送 `AbortSignal` 并同步置停进行中的 `phone`/`cloud` 命令。
3. **流式思维与工具折叠展示**：Sidecar 输出 DSH 事件流（`thought` / `tool_call` / `tool_result` / `text`）翻译回老端 `streamTrace` + `streamText`，不破坏既有消息气泡渲染。
4. **多模态纯视觉截图零丢帧**：DSH 调用 `phone`/`cloud` 拿回的带 `0..1000` 外标尺 JPEG 与 `zoom` 局部图必须直接作为 `image_url` 内容块进上下文，不能被纯文本序列化丢弃。
5. **平滑灰度切换开关**：在 `server/` 内保留 `BRAIN_ENGINE=legacy|dsh` 配置切换；实测全绿前绝不下线 `legacy` 回退路径。

---

### Task 1: 搭建 `server/dsh_brain/` Node DSH Sidecar 骨架与健康检查

**Files:**
- Create: `server/dsh_brain/package.json`
- Create: `server/dsh_brain/src/server.mjs`
- Test: `server/dsh_brain/tests/health.test.mjs`

- [ ] **Step 1:** 在 `server/dsh_brain/package.json` 声明本地 `@deepseek-ai/*` 核心依赖（不强绑 CLI UI），暴露 HTTP+SSE 本地端口 `127.0.0.1:18088`。
- [ ] **Step 2:** 编写 `/health`、`POST /turns`（SSE 流）、`POST /turns/:id/abort`、`POST /compact` 四个极简端点与对应的红灯测试。
- [ ] **Step 3:** 运行 `node --test server/dsh_brain/tests/health.test.mjs` 确认绿灯通过。

### Task 2: 实现 Python Gateway 反向工具回调桥 (`/internal/tools/*`)

**Files:**
- Create: `server/core/dsh_tool_bridge.py`
- Modify: `server/main.py`
- Test: `server/tests/test_dsh_tool_bridge.py`

- [ ] **Step 1:** 将现有 14 个核心正交工具（`read_file`, `write_file`, `edit_file`, `search_files`, `bash`, `web`, `memory`, `blackboard`, `phone`, `pc`, `cloud`, `reply`, `subtask`, `admin` —— 剔除 `today`）包装为受本地一次性密钥保护的 `POST /internal/tools/execute`。
- [ ] **Step 2:** 确保 `phone` / `cloud` / `pc` 工具返回值中的 `_multimodal_b64` 以标准结构透传给 `dsh_brain` 作为原生视觉图片块进模型上下文。
- [ ] **Step 3:** 运行 `pytest server/tests/test_dsh_tool_bridge.py` 验证文本与图片多模态回传无损。

### Task 3: 用 DSH Session + Blackboard 替代 `BrainToolsMixin` 手动循环

**Files:**
- Create: `server/dsh_brain/src/dsh_runner.mjs`
- Create: `server/core/dsh_brain_adapter.py`
- Modify: `server/brain.py`
- Test: `server/tests/test_dsh_brain_adapter.py`

- [ ] **Step 1:** 在 `dsh_runner.mjs` 中为每个 `(source, bot_id)` 维护独立 DSH Session，注入 Bot `SYSTEM.md` 静态前缀 + `MemoryEngine` / `Vault` 记忆索引（保证 KV-Cache 前缀稳定，不含易变动态块）。
- [ ] **Step 2:** `dsh_brain_adapter.py` 将 Sidecar 的 SSE 事件实时映射为老版本 `Brain.emit(...)` 格式与 `pulse_state.db` `order_seq` 消息行，支持 `edit_and_rerun`、`reroll_last` 与 `stop_immediately`。
- [ ] **Step 3:** 在 `server/brain.py` 支持 `BRAIN_ENGINE=dsh` 切换路由，并在 `test_dsh_brain_adapter.py` 完成多轮工具调用、中断、编辑重发测试。

### Task 4: 接入好奇心引擎 (`CuriosityEngine`) 与真机端到端验收

**Files:**
- Modify: `server/core/curiosity_engine.py`
- Modify: `deploy/proactive-agent.service` (systemd 加上 `dsh-brain` sidecar 依赖或统一启动)
- Test: `server/tests/test_v50_curiosity_pure_vision_no_toxic_fallbacks.py`

- [ ] **Step 1:** 将 `CuriosityEngine` 主动唤醒与黑板到期回访路由至当前激活的 `Brain` 入口（兼容 `legacy` 与 `dsh`）。
- [ ] **Step 2:** 实机完成整套回归测试：`pytest server/tests` 全绿、`node --check desktop/dsh-plugin` 全绿、Android APK 真机连接测试（对话、虚拟屏操控、全屏接管、停止中断）。
