package dev.bubuki.proactive

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.gestures.*
import androidx.compose.foundation.interaction.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.*
import androidx.compose.foundation.shape.*
import androidx.compose.foundation.text.*
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.*
import androidx.compose.ui.draw.*
import androidx.compose.ui.focus.*
import androidx.compose.ui.graphics.*
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.*
import androidx.compose.ui.platform.*
import androidx.compose.ui.semantics.*
import androidx.compose.ui.text.*
import androidx.compose.ui.text.font.*
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.style.*
import androidx.compose.ui.unit.*
import androidx.compose.ui.window.*
import dev.bubuki.proactive.markdown.MarkdownText
import kotlinx.coroutines.launch
import org.json.JSONArray
import kotlin.math.abs
import kotlin.math.roundToInt

/**
 * [Module] PulseScreenComposer
 * [Responsibility]
 * 聊天室頂欄導航與底部斜杠指令輸入欄領域模塊：負責頂部狀態欄、Bot 快速切換膠囊欄、上下文 Token 進度條、
 * 子代理與 Todo 狀態橫幅 (NativeChat)，以及底部斜杠指令面板、附件預覽、消息編輯欄與輸入發送框 (NativeComposer)。
 *
 * [Public API]
 * - NativeChat(model: NativePulseModel, bot: PulseBot, modifier: Modifier, onComposerHeight: (Int) -> Unit)
 * - NativeComposer(model: NativePulseModel, bot: PulseBot, onHeight: (Int) -> Unit)
 */

@Composable
internal fun NativeComposer(model: NativePulseModel, bot: PulseBot, onHeight: (Int) -> Unit) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val focus = LocalFocusManager.current
    val haptics = LocalHapticFeedback.current
    val requester = remember { FocusRequester() }
    val executing = s.workingBot == bot.id || s.busyBot == bot.id || s.streamBotId == bot.id
    val hasDraft = !s.uploading && (s.draft.isNotBlank() || s.attachments.isNotEmpty())
    var showComposerTray by remember { mutableStateOf(false) }
    var showAttachPicker by remember { mutableStateOf(false) }
    var previewThumb by remember { mutableStateOf<ImageBitmap?>(null) }
    var showStickerDrawer by remember { mutableStateOf(false) }

    val enabledPack = remember(s.stickerPacks) {
        s.stickerPacks.firstOrNull { it.optBoolean("enabled", true) && (it.optJSONArray("stickers")?.length() ?: 0) > 0 }
    }

    Surface(
        Modifier.fillMaxWidth()
            .onSizeChanged { onHeight(it.height) }
            .pointerInput(bot.id) {
                var dx = 0f
                var dy = 0f
                awaitEachGesture {
                    awaitFirstDown(requireUnconsumed = false)
                    dx = 0f
                    dy = 0f
                    do {
                        val event = awaitPointerEvent()
                        val change = event.changes.firstOrNull() ?: break
                        dx += change.position.x - change.previousPosition.x
                        dy += change.position.y - change.previousPosition.y
                    } while (event.changes.any { it.pressed })
                    if (abs(dx) > abs(dy) && abs(dx) > 80f) {
                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                        if (dx < 0) model.switchAdjacentBot(1) else model.switchAdjacentBot(-1)
                    } else if (abs(dy) > abs(dx) && abs(dy) > 52f) {
                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                        if (dy < 0) {
                            showComposerTray = true
                        } else {
                            showComposerTray = false
                            showStickerDrawer = false
                            focus.clearFocus()
                        }
                    }
                }
            },
        color = c.surface.copy(alpha = 0.98f),
        tonalElevation = 0.dp
    ) {
        Column(Modifier.fillMaxWidth()) {
            HorizontalDivider(color = c.line.copy(alpha = 0.65f), thickness = 0.5.dp)

            Column(Modifier.fillMaxWidth().padding(horizontal = 10.dp, vertical = 7.dp)) {
                // 1. Spring-Animated Pull-Up Tool Tray
                AnimatedVisibility(
                    visible = showComposerTray,
                    enter = expandVertically(spring(dampingRatio = 0.78f, stiffness = 400f)) + fadeIn(tween(150)),
                    exit = shrinkVertically(spring(dampingRatio = 0.85f, stiffness = 450f)) + fadeOut(tween(120))
                ) {
                    Row(
                        Modifier.fillMaxWidth()
                            .padding(bottom = 8.dp)
                            .clip(RoundedCornerShape(14.dp))
                            .background(c.fill.copy(alpha = 0.75f))
                            .horizontalScroll(rememberScrollState())
                            .padding(6.dp),
                        horizontalArrangement = Arrangement.spacedBy(6.dp)
                    ) {
                        listOf(
                            Triple("虚拟屏", c.ink) { showComposerTray = false; model.launchFloatingVirtualDisplay() },
                            Triple("云桌面", c.ink) { showComposerTray = false; model.openCloudDesktop() },
                            Triple("压缩上下文", c.accent) { showComposerTray = false; model.compactBotContext(bot.id) },
                            Triple("个人主页", c.ink) { showComposerTray = false; model.openBotEditor(bot.id) }
                        ).forEach { (label, col, act) ->
                            Text(
                                label,
                                Modifier.widthIn(min = 78.dp)
                                    .clip(RoundedCornerShape(10.dp))
                                    .background(c.surface)
                                    .iosPressScale(0.95f) {
                                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                        act()
                                    }
                                    .padding(horizontal = 12.dp, vertical = 8.dp),
                                color = col,
                                fontSize = 12.sp,
                                fontWeight = FontWeight.Medium,
                                maxLines = 1,
                                textAlign = TextAlign.Center
                            )
                        }
                    }
                }

                // 2. Spring-Animated Optional Sticker Plugin Drawer (Shirakami Fubuki Pack)
                AnimatedVisibility(
                    visible = showStickerDrawer && enabledPack != null,
                    enter = expandVertically(spring(dampingRatio = 0.78f, stiffness = 400f)) + fadeIn(tween(160)),
                    exit = shrinkVertically(spring(dampingRatio = 0.85f, stiffness = 450f)) + fadeOut(tween(120))
                ) {
                    val packId = enabledPack?.optString("id", "fubuki") ?: "fubuki"
                    val packName = enabledPack?.optString("name", "表情贴纸") ?: "表情贴纸"
                    val stickersArr = enabledPack?.optJSONArray("stickers") ?: JSONArray()
                    val stickers = remember(stickersArr) { (0 until stickersArr.length()).map { stickersArr.getJSONObject(it) } }
                    Column(
                        Modifier.fillMaxWidth()
                            .padding(bottom = 8.dp)
                            .clip(RoundedCornerShape(16.dp))
                            .background(c.fill.copy(alpha = 0.72f))
                            .padding(10.dp),
                        verticalArrangement = Arrangement.spacedBy(6.dp)
                    ) {
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Text("$packName (点按让 ${bot.name} 互动)", color = c.muted, fontSize = 12.sp, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f))
                            Text("收起", Modifier.clickable { showStickerDrawer = false }, color = c.accent, fontSize = 12.sp)
                        }
                        LazyRow(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                            items(stickers) { st ->
                                val fname = st.optString("file")
                                val slabel = st.optString("label", fname)
                                val url = st.optString("url", "/stickers/$packId/$fname")
                                Column(
                                    Modifier.clip(RoundedCornerShape(12.dp))
                                        .background(c.surface)
                                        .iosPressScale(0.93f) {
                                            showStickerDrawer = false
                                            model.sendQuickPrompt("[表情: $slabel]")
                                        }
                                        .padding(6.dp),
                                    horizontalAlignment = Alignment.CenterHorizontally
                                ) {
                                    PulseStickerThumb(model, url, 58.dp)
                                    Text(slabel, color = c.muted, fontSize = 10.sp, maxLines = 1, modifier = Modifier.padding(top = 2.dp))
                                }
                            }
                        }
                    }
                }

                // 4. Stop Pill when typing while Bot is busy
                AnimatedVisibility(visible = executing && hasDraft) {
                    Row(Modifier.fillMaxWidth().padding(bottom = 6.dp), horizontalArrangement = Arrangement.End) {
                        Row(
                            Modifier.clip(CircleShape)
                                .background(c.warn.copy(alpha = 0.14f))
                                .clickable { model.stop() }
                                .padding(horizontal = 12.dp, vertical = 4.5.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Box(Modifier.size(7.dp).clip(RoundedCornerShape(2.dp)).background(c.warn))
                            Text("停止当前生成", Modifier.padding(start = 6.dp), color = c.warn, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                        }
                    }
                }

                // 5. Modern Attachment Carousel (Thumbnails, File Badges, Per-Item Progress & Single Remove)
                AnimatedVisibility(
                    visible = s.attachments.isNotEmpty() || s.pendingUploads.isNotEmpty(),
                    enter = expandVertically(spring(dampingRatio = 0.80f, stiffness = 420f)) + fadeIn(),
                    exit = shrinkVertically(spring(dampingRatio = 0.85f, stiffness = 460f)) + fadeOut()
                ) {
                    ModernAttachmentStrip(
                        attachments = s.attachments,
                        pendingUploads = s.pendingUploads,
                        attachmentThumbs = s.attachmentThumbs,
                        onPreviewThumb = { previewThumb = it },
                        onRemoveAttachment = model::removeAttachment,
                        onRetryPending = model::retryPendingUpload,
                        onDismissPending = model::dismissPendingUpload,
                        onClearAll = model::clearAttachments
                    )
                }

                // 5b. Inline Modern Attachment Source Picker (Photos vs Documents)
                AnimatedVisibility(
                    visible = showAttachPicker,
                    enter = expandVertically(spring(dampingRatio = 0.78f, stiffness = 430f)) + fadeIn(),
                    exit = shrinkVertically(spring(dampingRatio = 0.85f, stiffness = 460f)) + fadeOut()
                ) {
                    Row(
                        Modifier.fillMaxWidth().padding(bottom = 8.dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        ModernAttachSourceButton(
                            icon = "image",
                            title = "相册照片",
                            subtitle = "多图直传 · 原图预览",
                            tint = Color(0xFF0A84FF),
                            modifier = Modifier.weight(1f),
                            onClick = {
                                showAttachPicker = false
                                model.chooseMediaGallery()
                            }
                        )
                        ModernAttachSourceButton(
                            icon = "file",
                            title = "浏览文件",
                            subtitle = "PDF / 文档 / 代码多选",
                            tint = Color(0xFF5E5CE6),
                            modifier = Modifier.weight(1f),
                            onClick = {
                                showAttachPicker = false
                                model.chooseDocuments()
                            }
                        )
                    }
                }

                // 6. Telegram-Style Edit Message Bar (Spring Animated)
                AnimatedVisibility(
                    visible = s.editingMessage != null,
                    enter = expandVertically(spring(dampingRatio = 0.78f, stiffness = 420f)) + fadeIn(),
                    exit = shrinkVertically(spring(dampingRatio = 0.85f, stiffness = 460f)) + fadeOut()
                ) {
                    val editing = s.editingMessage
                    if (editing != null) {
                        Row(
                            Modifier.fillMaxWidth()
                                .padding(bottom = 6.dp)
                                .clip(RoundedCornerShape(12.dp))
                                .background(c.fill.copy(alpha = 0.78f))
                                .padding(start = 10.dp, end = 4.dp, top = 3.dp, bottom = 3.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            PulseIcon("edit", c.accent, Modifier.size(17.dp))
                            Box(
                                Modifier.padding(start = 8.dp)
                                    .width(2.5.dp)
                                    .height(26.dp)
                                    .clip(RoundedCornerShape(2.dp))
                                    .background(c.accent)
                            )
                            Column(Modifier.padding(start = 8.dp).weight(1f)) {
                                Text("编辑消息", color = c.accent, fontSize = 11.5.sp, fontWeight = FontWeight.SemiBold)
                                Text(
                                    editing.text.replace("\n", " "),
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                    fontSize = 12.5.sp,
                                    color = c.ink
                                )
                            }
                            PulseRoundButton("close", "取消编辑", { model.startEditingMessage(null) }, background = false)
                        }
                    }
                }

                // 7. Telegram-Style Quote Reply Bar (Spring Animated)
                AnimatedVisibility(
                    visible = s.reply != null,
                    enter = expandVertically(spring(dampingRatio = 0.78f, stiffness = 420f)) + fadeIn(),
                    exit = shrinkVertically(spring(dampingRatio = 0.85f, stiffness = 460f)) + fadeOut()
                ) {
                    val quote = s.reply
                    if (quote != null) {
                        val quoteAuthor = if (quote.role == "user" && quote.kind != "bot_message") "我" else (quote.fromBotName.ifBlank { bot.name })
                        Row(
                            Modifier.fillMaxWidth()
                                .padding(bottom = 6.dp)
                                .clip(RoundedCornerShape(12.dp))
                                .background(c.fill.copy(alpha = 0.78f))
                                .padding(start = 10.dp, end = 4.dp, top = 3.dp, bottom = 3.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            PulseIcon("reply", c.accent, Modifier.size(17.dp))
                            Box(
                                Modifier.padding(start = 8.dp)
                                    .width(2.5.dp)
                                    .height(26.dp)
                                    .clip(RoundedCornerShape(2.dp))
                                    .background(c.accent)
                            )
                            Column(Modifier.padding(start = 8.dp).weight(1f)) {
                                Text(
                                    "回复 $quoteAuthor",
                                    color = c.accent,
                                    fontSize = 11.5.sp,
                                    fontWeight = FontWeight.SemiBold
                                )
                                Text(
                                    quote.text.replace("\n", " "),
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                    fontSize = 12.5.sp,
                                    color = c.muted
                                )
                            }
                            PulseRoundButton("close", "取消引用", { model.reply(null) }, background = false)
                        }
                    }
                }

                // 7.5. OpenCode / Grok Bot Style `/` Slash Command Autocomplete Popup
                val slashQuery = remember(s.draft) {
                    val trimmed = s.draft.trimStart()
                    if (trimmed.startsWith("/") && !trimmed.contains('\n') && !trimmed.contains(' ')) {
                        trimmed.lowercase()
                    } else null
                }
                val slashSuggestions = remember(slashQuery) {
                    if (slashQuery == null) emptyList()
                    else listOf(
                        Triple("/goal ", "深度目标闭环…", "开启长程死磕模式：自动拆解并持续执行直至目标 100% 完成"),
                        Triple("/compact", "压缩上下文", "归档较早对话至记忆库与 FTS5，释放 Token 窗口"),
                        Triple("/mcp", "MCP 与工具矩阵", "查看已挂载的 MCP 扩展工具、技能包与跨端工具"),
                        Triple("/memory", "记忆库概览", "查看 Zettelkasten 记忆库索引与个人印象档案"),
                        Triple("/models", "查看或切换模型", "列出可用大模型，或用 /model <ID> 切换模型"),
                        Triple("/sessions", "多端会话与工作区", "查看四端 Bot 房间及 Windows DSH 工作区会话"),
                        Triple("/undo", "撤销上一轮对话", "撤回当前房间最后一轮问答（可用 /redo 恢复）"),
                        Triple("/redo", "重做已撤销对话", "恢复最近一次通过 /undo 撤回的问答"),
                        Triple("/status", "上下文与缓存状态", "查看 Token 预算、Prefix KV 缓存命中率与在线状态"),
                        Triple("/doctor", "Harness 通道自检", "诊断多模态视觉、图片通道、记忆库与跨端桥接"),
                        Triple("/pins", "查看置顶记忆", "查看全部 📌 置顶记忆（或用 /pin <内容> 新增）"),
                        Triple("/pin ", "新增置顶记忆…", "填入 /pin 前缀以输入要永久钉住的规则"),
                        Triple("/clear", "归档并清空当前屏", "将当前房间历史归档至 FTS5 并开启新上下文"),
                        Triple("/share", "导出当前对话", "将当前房间对话导出为工作区 Markdown 文件"),
                        Triple("/help", "斜杠指令帮助", "显示所有支持的 Harness / 快捷指令大全")
                    ).filter { (cmd, title, _) ->
                        cmd.trim().startsWith(slashQuery) ||
                            (slashQuery.startsWith("/compack") && cmd == "/compact") ||
                            title.lowercase().contains(slashQuery.removePrefix("/"))
                    }
                }
                AnimatedVisibility(
                    visible = slashSuggestions.isNotEmpty(),
                    enter = expandVertically(spring(dampingRatio = 0.80f, stiffness = 420f)) + fadeIn(tween(140)),
                    exit = shrinkVertically(spring(dampingRatio = 0.85f, stiffness = 460f)) + fadeOut(tween(110))
                ) {
                    Column(
                        Modifier.fillMaxWidth()
                            .padding(bottom = 7.dp)
                            .heightIn(max = 220.dp)
                            .clip(RoundedCornerShape(16.dp))
                            .background(c.fill.copy(alpha = 0.96f))
                            .border(0.6.dp, c.accent.copy(alpha = 0.35f), RoundedCornerShape(16.dp))
                            .verticalScroll(rememberScrollState())
                            .padding(vertical = 4.dp, horizontal = 6.dp),
                        verticalArrangement = Arrangement.spacedBy(2.dp)
                    ) {
                        slashSuggestions.forEach { (cmd, title, desc) ->
                            Row(
                                Modifier.fillMaxWidth()
                                    .clip(RoundedCornerShape(10.dp))
                                    .clickable {
                                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                        if (cmd.endsWith(" ")) {
                                            model.setDraft(cmd)
                                        } else {
                                            model.setDraft("")
                                            model.sendQuickPrompt(cmd)
                                        }
                                    }
                                    .padding(horizontal = 10.dp, vertical = 7.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text(
                                    cmd.trim(),
                                    color = c.accent,
                                    fontSize = 13.5.sp,
                                    fontWeight = FontWeight.Bold,
                                    fontFamily = FontFamily.Monospace,
                                    modifier = Modifier.widthIn(min = 78.dp)
                                )
                                Text(
                                    title,
                                    color = c.ink,
                                    fontSize = 13.sp,
                                    fontWeight = FontWeight.SemiBold,
                                    modifier = Modifier.padding(start = 6.dp)
                                )
                                Spacer(Modifier.weight(1f))
                                Text(
                                    desc,
                                    color = c.muted,
                                    fontSize = 11.5.sp,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                    modifier = Modifier.padding(start = 8.dp).widthIn(max = 170.dp)
                                )
                            }
                        }
                    }
                }

                // 8. Main Telegram iOS Composer Row: [📎 Attach] + [Input Capsule with Sticker Button] + [Morphing Send/Stop Circle]
                Row(
                    Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.Bottom,
                    horizontalArrangement = Arrangement.spacedBy(7.dp)
                ) {
                    // Left 1: Modern Attach Toggle Pill (Opens Photo / File Split Tray)
                    Box(
                        Modifier.padding(bottom = 2.dp)
                            .size(35.dp)
                            .clip(CircleShape)
                            .background(if (showAttachPicker) c.accent.copy(alpha = 0.16f) else c.fill.copy(alpha = 0.82f))
                            .iosPressScale(0.92f) {
                                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                focus.clearFocus()
                                showComposerTray = false
                                showStickerDrawer = false
                                showAttachPicker = !showAttachPicker
                            }
                            .semantics { contentDescription = "发送图片或附件" },
                        contentAlignment = Alignment.Center
                    ) {
                        PulseIcon("attach", if (showAttachPicker) c.accent else c.muted, Modifier.size(18.5.dp))
                    }

                    // Left 2: Workspace / Action Sheet Button
                    Box(
                        Modifier.padding(bottom = 2.dp)
                            .size(35.dp)
                            .clip(CircleShape)
                            .background(if (showComposerTray) c.accent.copy(alpha = 0.16f) else c.fill.copy(alpha = 0.82f))
                            .iosPressScale(0.92f) {
                                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                focus.clearFocus()
                                showAttachPicker = false
                                model.actionsMenu()
                            }
                            .semantics { contentDescription = "工作台与工具" },
                        contentAlignment = Alignment.Center
                    ) {
                        PulseIcon("plus", if (showComposerTray) c.accent else c.muted, Modifier.size(19.dp))
                    }

                    // Center: Telegram Rounded Input Capsule + Embedded Sticker Icon
                    Row(
                        Modifier.weight(1f)
                            .clip(RoundedCornerShape(20.dp))
                            .background(c.fill.copy(alpha = 0.88f))
                            .border(0.5.dp, c.line.copy(alpha = 0.55f), RoundedCornerShape(20.dp))
                            .padding(start = 13.dp, end = 6.dp, top = 5.dp, bottom = 5.dp),
                        verticalAlignment = Alignment.Bottom
                    ) {
                        BasicTextField(
                            s.draft,
                            { if (it.length <= 12000) model.setDraft(it) },
                            Modifier.weight(1f)
                                .heightIn(min = 28.dp, max = 128.dp)
                                .padding(vertical = 3.dp, horizontal = 2.dp)
                                .focusRequester(requester)
                                .semantics { contentDescription = "聊天消息" },
                            textStyle = TextStyle(color = c.ink, fontSize = 16.sp, lineHeight = 21.sp),
                            cursorBrush = SolidColor(c.accent),
                            maxLines = 6,
                            keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Sentences),
                            decorationBox = { inner ->
                                Box {
                                    if (s.draft.isEmpty()) Text("消息…", color = c.faint, fontSize = 16.sp, maxLines = 1)
                                    inner()
                                }
                            }
                        )
                        if (enabledPack != null) {
                            Box(
                                Modifier.padding(bottom = 1.dp, end = 2.dp)
                                    .size(28.dp)
                                    .clip(CircleShape)
                                    .iosPressScale(0.90f) {
                                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                        showStickerDrawer = !showStickerDrawer
                                    },
                                contentAlignment = Alignment.Center
                            ) {
                                PulseIcon(
                                    "sticker",
                                    if (showStickerDrawer) c.accent else c.muted,
                                    Modifier.size(19.dp)
                                )
                            }
                        }
                    }

                    // Right: Telegram Circular Send / Stop Button with Spring Morph Animation
                    val reduceMotion = LocalReduceMotion.current
                    val showStopOnly = executing && !hasDraft
                    val buttonEnabled = (!s.uploading && hasDraft) || showStopOnly
                    val btnScale by animateFloatAsState(
                        targetValue = if (buttonEnabled) 1f else 0.90f,
                        animationSpec = iosInteractiveSpring(reduceMotion),
                        label = "send_btn_scale"
                    )
                    val btnColor by animateColorAsState(
                        targetValue = when {
                            showStopOnly -> c.warn
                            buttonEnabled -> c.accent
                            else -> c.fill
                        },
                        animationSpec = tween(150),
                        label = "send_btn_color"
                    )
                    Box(
                        Modifier.padding(bottom = 1.dp)
                            .size(38.dp)
                            .graphicsLayer {
                                scaleX = btnScale
                                scaleY = btnScale
                            }
                            .clip(CircleShape)
                            .background(btnColor)
                            .iosPressScale(0.90f, enabled = buttonEnabled) {
                                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                if (showStopOnly) model.stop()
                                else if (s.editingMessage != null) model.editMessageAndResend(s.editingMessage, s.draft)
                                else model.send()
                            }
                            .semantics { contentDescription = if (showStopOnly) "停止执行" else "发送消息" },
                        contentAlignment = Alignment.Center
                    ) {
                        AnimatedContent(
                            targetState = showStopOnly,
                            transitionSpec = {
                                if (reduceMotion) {
                                    fadeIn(tween(100)) togetherWith fadeOut(tween(80))
                                } else {
                                    (scaleIn(initialScale = 0.82f, animationSpec = spring(0.75f, 520f)) + fadeIn(tween(120))) togetherWith
                                        (scaleOut(targetScale = 0.82f) + fadeOut(tween(90)))
                                }
                            },
                            label = "send_stop_glyph"
                        ) { stopping ->
                            if (stopping) {
                                Box(Modifier.size(11.dp).clip(RoundedCornerShape(2.5.dp)).background(Color.White))
                            } else {
                                PulseIcon(
                                    "send",
                                    if (buttonEnabled) Color.White else c.faint,
                                    Modifier.size(17.dp)
                                )
                            }
                        }
                    }
                }
            }
        }
    }

    val activePreview = previewThumb
    if (activePreview != null) {
        Dialog(onDismissRequest = { previewThumb = null }) {
            Box(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(18.dp))
                    .background(Color.Black.copy(alpha = 0.92f))
                    .clickable { previewThumb = null }
                    .padding(10.dp),
                contentAlignment = Alignment.Center
            ) {
                Image(
                    bitmap = activePreview,
                    contentDescription = "附件预览",
                    modifier = Modifier.fillMaxWidth().heightIn(max = 420.dp).clip(RoundedCornerShape(12.dp)),
                    contentScale = ContentScale.Fit
                )
                Box(
                    Modifier.align(Alignment.TopEnd)
                        .padding(6.dp)
                        .size(28.dp)
                        .clip(CircleShape)
                        .background(Color.Black.copy(alpha = 0.55f))
                        .clickable { previewThumb = null },
                    contentAlignment = Alignment.Center
                ) {
                    PulseIcon("close", Color.White, Modifier.size(14.dp))
                }
            }
        }
    }
}

private fun formatCompactBytes(bytes: Long): String {
    if (bytes <= 0L) return ""
    return when {
        bytes < 1024L -> "${bytes} B"
        bytes < 1024L * 1024L -> "${(bytes / 1024.0).roundToInt()} KB"
        else -> String.format(java.util.Locale.US, "%.1f MB", bytes / (1024.0 * 1024.0))
    }
}

private fun extBadgeStyle(name: String): Pair<String, Color> {
    val ext = name.substringAfterLast('.', "").uppercase(java.util.Locale.ROOT).take(4)
    val label = ext.ifBlank { "FILE" }
    val color = when (ext) {
        "PDF" -> Color(0xFFFF453A)
        "ZIP", "RAR", "7Z", "GZ", "TAR" -> Color(0xFFFF9F0A)
        "PY", "KT", "JS", "TS", "MJS", "JAVA", "JSON", "YML", "YAML", "MD" -> Color(0xFF5E5CE6)
        "XLS", "XLSX", "CSV" -> Color(0xFF30D158)
        "DOC", "DOCX", "TXT", "RTF" -> Color(0xFF0A84FF)
        else -> Color(0xFF8E8E93)
    }
    return label to color
}

@Composable
private fun ModernAttachSourceButton(
    icon: String,
    title: String,
    subtitle: String,
    tint: Color,
    modifier: Modifier = Modifier,
    onClick: () -> Unit
) {
    val c = LocalPulsePalette.current
    Row(
        modifier
            .clip(RoundedCornerShape(14.dp))
            .background(c.fill.copy(alpha = 0.88f))
            .border(0.6.dp, c.line.copy(alpha = 0.65f), RoundedCornerShape(14.dp))
            .iosPressScale(0.96f, onClick = onClick)
            .padding(horizontal = 11.dp, vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(9.dp)
    ) {
        Box(
            Modifier.size(32.dp)
                .clip(RoundedCornerShape(9.dp))
                .background(tint.copy(alpha = 0.14f)),
            contentAlignment = Alignment.Center
        ) {
            PulseIcon(icon, tint, Modifier.size(17.dp))
        }
        Column(Modifier.weight(1f)) {
            Text(title, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, color = c.ink, maxLines = 1)
            Text(subtitle, fontSize = 10.5.sp, color = c.muted, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
    }
}

@Composable
private fun ModernAttachmentStrip(
    attachments: List<PulseAttachment>,
    pendingUploads: List<PulsePendingUpload>,
    attachmentThumbs: Map<String, ImageBitmap>,
    onPreviewThumb: (ImageBitmap) -> Unit,
    onRemoveAttachment: (PulseAttachment) -> Unit,
    onRetryPending: (String) -> Unit,
    onDismissPending: (String) -> Unit,
    onClearAll: () -> Unit
) {
    val c = LocalPulsePalette.current
    val totalCount = attachments.size + pendingUploads.size
    Column(Modifier.fillMaxWidth().padding(bottom = 8.dp)) {
        LazyRow(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            items(pendingUploads, key = { "p_${it.id}" }) { item ->
                if (item.isImage) {
                    AttachmentImageTile(
                        name = item.name,
                        thumb = item.thumb,
                        uploading = item.error.isEmpty(),
                        progress = item.progress,
                        error = item.error,
                        onClick = {
                            if (item.error.isNotEmpty()) onRetryPending(item.id)
                            else item.thumb?.let(onPreviewThumb)
                        },
                        onRemove = { onDismissPending(item.id) }
                    )
                } else {
                    AttachmentFileTile(
                        name = item.name,
                        bytes = item.bytes,
                        uploading = item.error.isEmpty(),
                        progress = item.progress,
                        error = item.error,
                        onClick = { if (item.error.isNotEmpty()) onRetryPending(item.id) },
                        onRemove = { onDismissPending(item.id) }
                    )
                }
            }
            items(attachments, key = { "a_${it.source}_${it.path}" }) { att ->
                val isImg = CloudFiles.isImageFilename(att.name) || attachmentThumbs.containsKey(att.path)
                val thumb = attachmentThumbs[att.path]
                if (isImg) {
                    AttachmentImageTile(
                        name = att.name,
                        thumb = thumb,
                        uploading = false,
                        progress = 1f,
                        error = "",
                        onClick = { thumb?.let(onPreviewThumb) },
                        onRemove = { onRemoveAttachment(att) }
                    )
                } else {
                    AttachmentFileTile(
                        name = att.name,
                        bytes = att.bytes,
                        uploading = false,
                        progress = 1f,
                        error = "",
                        onClick = {},
                        onRemove = { onRemoveAttachment(att) }
                    )
                }
            }
            if (totalCount >= 2 && pendingUploads.none { it.error.isEmpty() }) {
                item(key = "clear_all_chip") {
                    Column(
                        Modifier.height(64.dp)
                            .clip(RoundedCornerShape(14.dp))
                            .background(c.fill.copy(alpha = 0.70f))
                            .border(0.6.dp, c.line.copy(alpha = 0.6f), RoundedCornerShape(14.dp))
                            .iosPressScale(0.95f, onClick = onClearAll)
                            .padding(horizontal = 12.dp),
                        verticalArrangement = Arrangement.Center,
                        horizontalAlignment = Alignment.CenterHorizontally
                    ) {
                        PulseIcon("broom", c.muted, Modifier.size(16.dp))
                        Text("全清", Modifier.padding(top = 3.dp), fontSize = 11.sp, color = c.muted, fontWeight = FontWeight.Medium)
                    }
                }
            }
        }
    }
}

@Composable
private fun AttachmentImageTile(
    name: String,
    thumb: ImageBitmap?,
    uploading: Boolean,
    progress: Float,
    error: String,
    onClick: () -> Unit,
    onRemove: () -> Unit
) {
    val c = LocalPulsePalette.current
    val smoothProgress by animateFloatAsState(
        targetValue = progress.coerceIn(0f, 1f),
        animationSpec = tween(160),
        label = "img_upload_progress"
    )
    val borderColor = if (error.isNotEmpty()) c.warn.copy(alpha = 0.85f) else c.line.copy(alpha = 0.75f)
    Box(
        Modifier.size(64.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(c.fill)
            .border(0.8.dp, borderColor, RoundedCornerShape(14.dp))
            .clickable(onClick = onClick)
    ) {
        if (thumb != null) {
            Image(
                bitmap = thumb,
                contentDescription = name,
                modifier = Modifier.fillMaxSize(),
                contentScale = ContentScale.Crop
            )
        } else {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                PulseIcon("image", c.accent, Modifier.size(22.dp))
            }
        }
        if (uploading) {
            Box(
                Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.45f)),
                contentAlignment = Alignment.Center
            ) {
                CircularProgressIndicator(
                    progress = { smoothProgress },
                    modifier = Modifier.size(26.dp),
                    color = Color.White,
                    trackColor = Color.White.copy(alpha = 0.25f),
                    strokeWidth = 2.4.dp
                )
                Text(
                    "${(smoothProgress * 100).roundToInt()}%",
                    color = Color.White,
                    fontSize = 9.sp,
                    fontWeight = FontWeight.Bold
                )
            }
        } else if (error.isNotEmpty()) {
            Box(
                Modifier.fillMaxSize().background(c.warn.copy(alpha = 0.35f)),
                contentAlignment = Alignment.Center
            ) {
                PulseIcon("refresh", Color.White, Modifier.size(20.dp))
            }
        }
        Box(
            Modifier.align(Alignment.TopEnd)
                .padding(3.dp)
                .size(18.dp)
                .clip(CircleShape)
                .background(Color.Black.copy(alpha = 0.62f))
                .clickable(onClick = onRemove),
            contentAlignment = Alignment.Center
        ) {
            PulseIcon("close", Color.White, Modifier.size(10.dp))
        }
    }
}

@Composable
private fun AttachmentFileTile(
    name: String,
    bytes: Long,
    uploading: Boolean,
    progress: Float,
    error: String,
    onClick: () -> Unit,
    onRemove: () -> Unit
) {
    val c = LocalPulsePalette.current
    val (extLabel, extColor) = remember(name) { extBadgeStyle(name) }
    val smoothProgress by animateFloatAsState(
        targetValue = progress.coerceIn(0f, 1f),
        animationSpec = tween(160),
        label = "file_upload_progress"
    )
    val sizeTxt = remember(bytes) { formatCompactBytes(bytes) }
    val subTxt = when {
        error.isNotEmpty() -> "上传失败 · 点击重试"
        uploading -> if (sizeTxt.isNotEmpty()) "${(smoothProgress * 100).roundToInt()}% · $sizeTxt" else "正在上传 ${(smoothProgress * 100).roundToInt()}%"
        sizeTxt.isNotEmpty() -> "$sizeTxt · 已就绪"
        else -> "已就绪"
    }
    Box(
        Modifier.widthIn(min = 152.dp, max = 204.dp)
            .height(64.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(if (error.isNotEmpty()) c.warn.copy(alpha = 0.08f) else c.fill.copy(alpha = 0.92f))
            .border(0.8.dp, if (error.isNotEmpty()) c.warn.copy(alpha = 0.65f) else c.line.copy(alpha = 0.72f), RoundedCornerShape(14.dp))
            .clickable(onClick = onClick)
    ) {
        Row(
            Modifier.fillMaxSize().padding(start = 9.dp, end = 24.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(9.dp)
        ) {
            Box(
                Modifier.size(36.dp)
                    .clip(RoundedCornerShape(10.dp))
                    .background(extColor.copy(alpha = 0.16f)),
                contentAlignment = Alignment.Center
            ) {
                if (error.isNotEmpty()) {
                    PulseIcon("refresh", c.warn, Modifier.size(16.dp))
                } else {
                    Text(extLabel, color = extColor, fontSize = 10.sp, fontWeight = FontWeight.Bold, maxLines = 1)
                }
            }
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.Center) {
                Text(name, color = c.ink, fontSize = 12.5.sp, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(
                    subTxt,
                    color = if (error.isNotEmpty()) c.warn else c.muted,
                    fontSize = 10.5.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
        }
        if (uploading) {
            Box(
                Modifier.align(Alignment.BottomStart)
                    .fillMaxWidth()
                    .height(2.5.dp)
                    .background(c.line.copy(alpha = 0.35f))
            ) {
                Box(
                    Modifier.fillMaxHeight()
                        .fillMaxWidth(smoothProgress)
                        .background(c.accent)
                )
            }
        }
        Box(
            Modifier.align(Alignment.TopEnd)
                .padding(4.dp)
                .size(18.dp)
                .clip(CircleShape)
                .background(c.ink.copy(alpha = 0.12f))
                .clickable(onClick = onRemove),
            contentAlignment = Alignment.Center
        ) {
            PulseIcon("close", c.muted, Modifier.size(10.dp))
        }
    }
}
