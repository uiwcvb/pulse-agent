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
import androidx.compose.ui.text.input.TextFieldValue
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
    val reduceMotion = rememberReducedMotion()
    val requester = remember { FocusRequester() }
    val executing = s.workingBot == bot.id || s.busyBot == bot.id || s.streamBotId == bot.id
    val hasDraft = !s.uploading && (s.draft.isNotBlank() || s.attachments.isNotEmpty())
    var showPlusHub by remember { mutableStateOf(false) }
    var previewThumb by remember { mutableStateOf<ImageBitmap?>(null) }
    var showStickerDrawer by remember { mutableStateOf(false) }
    var selectedPackIdx by rememberSaveable { mutableIntStateOf(0) }
    var confirmClearRoom by remember { mutableStateOf(false) }
    var textFieldVal by remember(bot.id) { mutableStateOf(TextFieldValue(s.draft, TextRange(s.draft.length))) }
    LaunchedEffect(s.draft) {
        if (textFieldVal.text != s.draft) {
            textFieldVal = TextFieldValue(s.draft, TextRange(s.draft.length))
        }
    }

    val enabledPacks = remember(s.stickerPacks) {
        s.stickerPacks.filter { it.optBoolean("enabled", true) && (it.optJSONArray("stickers")?.length() ?: 0) > 0 }
    }
    val safePackIdx = selectedPackIdx.coerceIn(0, (enabledPacks.size - 1).coerceAtLeast(0))
    val enabledPack = enabledPacks.getOrNull(safePackIdx)

    Surface(
        Modifier.fillMaxWidth()
            .onSizeChanged { onHeight(it.height) }
            .pointerInput(bot.id) {
                val horizThresh = 42.dp.toPx()
                val vertThresh = 28.dp.toPx()
                var dx = 0f
                var dy = 0f
                awaitEachGesture {
                    awaitFirstDown(requireUnconsumed = false)
                    dx = 0f
                    dy = 0f
                    var childConsumed = false
                    do {
                        val event = awaitPointerEvent()
                        val change = event.changes.firstOrNull() ?: break
                        if (change.isConsumed) {
                            childConsumed = true
                            break
                        }
                        dx += change.position.x - change.previousPosition.x
                        dy += change.position.y - change.previousPosition.y
                    } while (event.changes.any { it.pressed })
                    if (!childConsumed) {
                        if (abs(dx) > abs(dy) && abs(dx) > horizThresh) {
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            if (dx < 0) model.switchAdjacentBot(1) else model.switchAdjacentBot(-1)
                        } else if (abs(dy) > abs(dx) && abs(dy) > vertThresh) {
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            if (dy < 0) {
                                showPlusHub = true
                            } else {
                                showPlusHub = false
                                showStickerDrawer = false
                                focus.clearFocus()
                            }
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
                // 1. Unified Apple-HIG Attach & Workspace Hub (Single `+` Drawer)
                AnimatedVisibility(
                    visible = showPlusHub,
                    enter = if (reduceMotion) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(150)),
                    exit = if (reduceMotion) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(110))
                ) {
                    Column(
                        Modifier.fillMaxWidth()
                            .padding(bottom = 8.dp)
                            .clip(RoundedCornerShape(16.dp))
                            .background(c.fill.copy(alpha = 0.72f))
                            .padding(7.dp),
                        verticalArrangement = Arrangement.spacedBy(7.dp)
                    ) {
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(7.dp)) {
                            ModernAttachSourceButton(
                                icon = "image",
                                title = "快速拍照",
                                subtitle = "秒拍直传 AI",
                                tint = Color(0xFF34C759),
                                modifier = Modifier.weight(1f),
                                onClick = {
                                    showPlusHub = false
                                    model.captureQuickPhoto()
                                }
                            )
                            ModernAttachSourceButton(
                                icon = "image",
                                title = "相册照片",
                                subtitle = "多图直传",
                                tint = c.accent,
                                modifier = Modifier.weight(1f),
                                onClick = {
                                    showPlusHub = false
                                    model.chooseMediaGallery()
                                }
                            )
                            ModernAttachSourceButton(
                                icon = "file",
                                title = "浏览文件",
                                subtitle = "PDF / 文档",
                                tint = Color(0xFF5E5CE6),
                                modifier = Modifier.weight(1f),
                                onClick = {
                                    showPlusHub = false
                                    model.chooseDocuments()
                                }
                            )
                        }
                        Row(
                            Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                            horizontalArrangement = Arrangement.spacedBy(6.dp)
                        ) {
                            listOf(
                                Triple("虚拟屏", c.ink) { showPlusHub = false; model.launchFloatingVirtualDisplay() },
                                Triple("云桌面", c.ink) { showPlusHub = false; model.openCloudDesktop() },
                                Triple("压缩上下文", c.accent) { showPlusHub = false; model.compactBotContext(bot.id) },
                                Triple("个人主页", c.ink) { showPlusHub = false; model.openBotEditor(bot.id) },
                                Triple("全部工作台…", c.muted) { showPlusHub = false; model.actionsMenu() }
                            ).forEach { (label, col, act) ->
                                Text(
                                    label,
                                    Modifier.heightIn(min = 36.dp)
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
                }

                // 2. Spring-Animated Optional Sticker Plugin Drawer (Multi-Pack Tabs + Direct Send)
                AnimatedVisibility(
                    visible = showStickerDrawer && enabledPack != null,
                    enter = if (reduceMotion) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(150)),
                    exit = if (reduceMotion) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(110))
                ) {
                    val packId = enabledPack?.optString("id", "fubuki") ?: "fubuki"
                    val packName = enabledPack?.optString("name", "表情贴纸") ?: "表情贴纸"
                    val stickersArr = enabledPack?.optJSONArray("stickers") ?: JSONArray()
                    val stickers = remember(stickersArr) { (0 until stickersArr.length()).mapNotNull { runCatching { stickersArr.getJSONObject(it) }.getOrNull() } }
                    Column(
                        Modifier.fillMaxWidth()
                            .padding(bottom = 8.dp)
                            .clip(RoundedCornerShape(18.dp))
                            .background(c.fill.copy(alpha = 0.72f))
                            .padding(8.dp),
                        verticalArrangement = Arrangement.spacedBy(6.dp)
                    ) {
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Text("$packName (点按发送表情)", color = c.muted, fontSize = 12.sp, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f).padding(start = 2.dp))
                            Box(
                                Modifier.heightIn(min = 40.dp)
                                    .clip(RoundedCornerShape(8.dp))
                                    .iosPressScale(0.94f) { showStickerDrawer = false }
                                    .padding(horizontal = 10.dp, vertical = 8.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text("收起", color = c.accent, fontSize = 12.sp, fontWeight = FontWeight.Medium)
                            }
                        }
                        if (enabledPacks.size > 1) {
                            LazyRow(
                                horizontalArrangement = Arrangement.spacedBy(6.dp),
                                modifier = Modifier.fillMaxWidth()
                            ) {
                                items(enabledPacks.size, key = { idx -> enabledPacks[idx].optString("id", "pack_$idx") }) { idx ->
                                    val p = enabledPacks[idx]
                                    val isSel = idx == safePackIdx
                                    Text(
                                        p.optString("name", p.optString("id", "贴纸包")),
                                        Modifier.clip(RoundedCornerShape(10.dp))
                                            .background(if (isSel) c.accent else c.surface)
                                            .iosPressScale(0.95f) { selectedPackIdx = idx }
                                            .padding(horizontal = 10.dp, vertical = 6.dp),
                                        color = if (isSel) Color.White else c.ink,
                                        fontSize = 11.5.sp,
                                        fontWeight = if (isSel) FontWeight.SemiBold else FontWeight.Medium
                                    )
                                }
                            }
                        }
                        LazyRow(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                            items(stickers, key = { st -> "${packId}_${st.optString("code").ifBlank { st.optString("file") }}" }) { st ->
                                val fname = st.optString("file")
                                val scode = st.optString("code").ifBlank { fname.substringBeforeLast('.') }
                                val slabel = st.optString("label", fname)
                                val url = st.optString("url", "/stickers/$packId/$fname")
                                Column(
                                    Modifier.clip(RoundedCornerShape(12.dp))
                                        .background(c.surface)
                                        .iosPressScale(0.93f) {
                                            showStickerDrawer = false
                                            model.sendSticker(scode, url)
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
                AnimatedVisibility(
                    visible = executing && hasDraft,
                    enter = fadeIn(tween(110)),
                    exit = fadeOut(tween(90))
                ) {
                    Row(Modifier.fillMaxWidth().padding(bottom = 6.dp), horizontalArrangement = Arrangement.End) {
                        Row(
                            Modifier.heightIn(min = 38.dp)
                                .clip(CircleShape)
                                .background(c.warn.copy(alpha = 0.14f))
                                .iosPressScale(0.94f) { model.stop() }
                                .padding(horizontal = 13.dp, vertical = 6.dp),
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
                    enter = if (reduceMotion) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(150)),
                    exit = if (reduceMotion) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(110))
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

                // 6. Telegram-Style Edit Message Bar (Spring Animated)
                AnimatedVisibility(
                    visible = s.editingMessage != null,
                    enter = if (reduceMotion) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(150)),
                    exit = if (reduceMotion) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(110))
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
                    enter = if (reduceMotion) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(150)),
                    exit = if (reduceMotion) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(110))
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
                    if (trimmed.startsWith("/") && !trimmed.contains('\n') && trimmed.length <= 28) {
                        trimmed.lowercase()
                    } else null
                }
                val slashSuggestions = remember(slashQuery) {
                    if (slashQuery == null) emptyList()
                    else listOf(
                        Triple("/goal ", "深度目标闭环…", "开启长程死磕模式：自动拆解并持续执行直至目标 100% 完成"),
                        Triple("/compact", "压缩上下文", "归档较早对话至记忆库与 FTS5，释放 Token 窗口"),
                        Triple("/memory reorganize", "深度整理记忆", "瞬发 8 阶段全自动整理子任务，可点进二级页看进度"),
                        Triple("/memory rollback", "回滚记忆整理", "一键还原到最近一次整理前快照"),
                        Triple("/memory gardener", "记忆园丁维护", "清理过期临时事实、合并重复并滚卷时间金字塔"),
                        Triple("/subtasks", "查看子任务状态", "列出当前并发运行与近期的子任务及报错"),
                        Triple("/mcp", "MCP 与工具矩阵", "查看已挂载的 MCP 扩展工具、技能包与跨端工具"),
                        Triple("/memory", "记忆库概览", "查看长期记忆库索引与个人印象档案"),
                        Triple("/models", "查看可用模型", "列出当前配置的所有可用大模型"),
                        Triple("/model ", "切换指定模型…", "填入 /model 前缀以输入目标模型 ID"),
                        Triple("/sessions", "多端会话与工作区", "查看四端 Bot 房间及 Windows DSH 工作区会话"),
                        Triple("/undo", "撤销上一轮对话", "撤回当前房间最后一轮问答（可用 /redo 恢复）"),
                        Triple("/redo", "重做已撤销对话", "恢复最近一次通过 /undo 撤回的问答"),
                        Triple("/status", "上下文与缓存状态", "查看 Token 预算、Prefix KV 缓存命中率与在线状态"),
                        Triple("/doctor", "Harness 通道自检", "诊断多模态视觉、图片通道、记忆库与跨端桥接"),
                        Triple("/pins", "查看置顶记忆", "查看全部置顶记忆（或用 /pin <内容> 新增）"),
                        Triple("/pin ", "新增置顶记忆…", "填入 /pin 前缀以输入要永久钉住的规则"),
                        Triple("/unpin ", "移除置顶记忆…", "填入 /unpin 前缀以指定关键词或 ID 移除"),
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
                    enter = if (reduceMotion) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(140)),
                    exit = if (reduceMotion) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(110))
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
                                            textFieldVal = TextFieldValue(cmd, TextRange(cmd.length))
                                            model.setDraft(cmd)
                                        } else if (cmd == "/clear") {
                                            confirmClearRoom = true
                                        } else {
                                            textFieldVal = TextFieldValue("", TextRange(0))
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

                // 8. Clean Apple-HIG Composer Row: [Unified + Trigger] + [Input Capsule with Sticker Button] + [Morphing Send/Stop Circle]
                Row(
                    Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.Bottom,
                    horizontalArrangement = Arrangement.spacedBy(7.dp)
                ) {
                    val plusActive = showPlusHub || s.menu != null
                    val plusRot by animateFloatAsState(
                        targetValue = if (showPlusHub) 45f else 0f,
                        animationSpec = iosInteractiveSpring(reduceMotion),
                        label = "plus_rot"
                    )
                    Box(
                        Modifier.padding(bottom = 1.dp)
                            .size(40.dp)
                            .clip(CircleShape)
                            .background(if (plusActive) c.accent.copy(alpha = 0.16f) else c.fill.copy(alpha = 0.82f))
                            .iosPressScale(0.92f) {
                                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                focus.clearFocus()
                                showStickerDrawer = false
                                showPlusHub = !showPlusHub
                            }
                            .semantics { contentDescription = "附件与工作台工具" },
                        contentAlignment = Alignment.Center
                    ) {
                        PulseIcon(
                            "plus",
                            if (plusActive) c.accent else c.muted,
                            Modifier.size(19.dp).graphicsLayer { rotationZ = plusRot }
                        )
                    }

                    // Center: Telegram Rounded Input Capsule + Embedded Sticker Icon
                    Row(
                        Modifier.weight(1f)
                            .clip(RoundedCornerShape(20.dp))
                            .background(c.fill.copy(alpha = 0.88f))
                            .border(0.5.dp, c.line.copy(alpha = 0.55f), RoundedCornerShape(20.dp))
                            .padding(start = 13.dp, end = 4.dp, top = 4.dp, bottom = 4.dp),
                        verticalAlignment = Alignment.Bottom
                    ) {
                        BasicTextField(
                            value = textFieldVal,
                            onValueChange = { next ->
                                if (next.text.length <= 12000) {
                                    textFieldVal = next
                                    model.setDraft(next.text)
                                }
                            },
                            Modifier.weight(1f)
                                .heightIn(min = 28.dp, max = 128.dp)
                                .padding(vertical = 4.dp, horizontal = 2.dp)
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
                                Modifier.size(34.dp)
                                    .clip(CircleShape)
                                    .iosPressScale(0.90f) {
                                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                        showStickerDrawer = !showStickerDrawer
                                    }
                                    .semantics { contentDescription = "表情贴纸面板" },
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
                        animationSpec = if (reduceMotion) snap() else tween(150),
                        label = "send_btn_color"
                    )
                    Box(
                        Modifier.padding(bottom = 1.dp)
                            .size(40.dp)
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
                                    (scaleIn(initialScale = 0.90f, animationSpec = iosInteractiveSpring()) + fadeIn(tween(120))) togetherWith
                                        (scaleOut(targetScale = 0.90f) + fadeOut(tween(90)))
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

    if (confirmClearRoom) {
        AlertDialog(
            onDismissRequest = { confirmClearRoom = false },
            containerColor = c.surface,
            titleContentColor = c.ink,
            textContentColor = c.muted,
            title = { Text("归档并清空当前屏？", fontSize = 16.5.sp, fontWeight = FontWeight.SemiBold) },
            text = { Text("当前房间历史将被归档至记忆库 FTS5 并开启空白上下文，确定执行吗？", fontSize = 13.5.sp) },
            confirmButton = {
                TextButton(onClick = {
                    confirmClearRoom = false
                    textFieldVal = TextFieldValue("", TextRange(0))
                    model.setDraft("")
                    model.sendQuickPrompt("/clear")
                }) {
                    Text("确定清空", color = c.warn)
                }
            },
            dismissButton = {
                TextButton(onClick = { confirmClearRoom = false }) {
                    Text("取消", color = c.accent)
                }
            }
        )
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
                    modifier = Modifier.fillMaxWidth().heightIn(max = 420.dp).clip(RoundedCornerShape(8.dp)),
                    contentScale = ContentScale.Fit
                )
                Box(
                    Modifier.align(Alignment.TopEnd)
                        .size(40.dp)
                        .iosPressScale(0.90f) { previewThumb = null }
                        .semantics { contentDescription = "关闭附件预览" },
                    contentAlignment = Alignment.Center
                ) {
                    Box(
                        Modifier.size(28.dp)
                            .clip(CircleShape)
                            .background(Color.Black.copy(alpha = 0.55f)),
                        contentAlignment = Alignment.Center
                    ) {
                        PulseIcon("close", Color.White, Modifier.size(14.dp))
                    }
                }
            }
        }
    }
}
