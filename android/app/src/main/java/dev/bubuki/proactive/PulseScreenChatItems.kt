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
 * [Module] PulseScreenChatItems
 * [Responsibility]
 * 消息氣泡與富媒體卡片渲染領域模塊：負責單條消息氣泡渲染、思考折疊塊、多代理委派卡片、性能指標膠囊 (NativeMessage)、
 * 工具調用軌跡折疊組 (NativeToolTraceGroup)、貼紙表情包縮略圖與大圖渲染 (PulseStickerThumb, PulseStickerImage)，
 * 以及快捷動作膠囊 (TelegramActionChip)。
 *
 * [Public API]
 * - NativeMessage(...)
 * - NativeToolTraceGroup(model: NativePulseModel, messageId: String, trace: List<org.json.JSONObject>)
 * - PulseStickerThumb(...), PulseStickerImage(...), TelegramActionChip(...)
 */

@OptIn(ExperimentalFoundationApi::class)
@Composable
internal fun NativeMessage(
    model: NativePulseModel,
    bot: PulseBot,
    message: PulseMessage,
    groupedWithPrev: Boolean = false,
    groupedWithNext: Boolean = false,
    isLatestMessage: Boolean = false
) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    val density = LocalDensity.current
    val msgScope = rememberCoroutineScope()

    val isBotToBotIn = message.kind == "bot_message"
    val isBotToBotReply = message.kind == "bot_reply"
    val isError = message.kind == "error"
    val isNotification = message.kind == "notification"
    val user = message.role == "user" && !isBotToBotIn
    var showActions by rememberSaveable(message.id) { mutableStateOf(false) }

    // Telegram Interactive Finger-Tracking Left-Swipe:
    // 1:1 linear reveal of telemetry on light swipe, haptic quote-reply past threshold
    val swipeOffset = remember(message.id) { Animatable(0f) }
    val replyTriggerPx = with(density) { 58.dp.toPx() }
    val revealWidthPx = with(density) { 40.dp.toPx() }
    val maxSwipeLeftPx = with(density) { 104.dp.toPx() }
    var replyThresholdCrossed by remember(message.id) { mutableStateOf(false) }

    if (message.kind == "compact_summary") {
        val meta = remember(message.decision) { runCatching { org.json.JSONObject(message.decision) }.getOrNull() }
        val beforeTok = meta?.optInt("before_tokens", 0) ?: 0
        val afterTok = meta?.optInt("after_tokens", 0) ?: 0
        val msgCnt = meta?.optInt("compacted_messages", 0) ?: 0
        val vaultPath = (meta?.optString("vault_path", "") ?: "").ifBlank { "INDEX.md" }
        val ctxId = meta?.optString("ctx_id", "").orEmpty()
        val modeLabel = (meta?.optString("mode", "上下文压缩归档") ?: "").ifBlank { "上下文压缩归档" }
        var summaryExpanded by rememberSaveable(message.id) { mutableStateOf(false) }

        Column(
            Modifier.fillMaxWidth()
                .clip(RoundedCornerShape(16.dp))
                .background(c.surface)
                .border(0.6.dp, c.line.copy(alpha = 0.72f), RoundedCornerShape(16.dp))
                .padding(horizontal = 14.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Box(
                    Modifier.clip(RoundedCornerShape(7.dp))
                        .background(c.accent.copy(alpha = 0.14f))
                        .padding(horizontal = 7.dp, vertical = 3.dp)
                ) {
                    Text(modeLabel, color = c.accent, fontSize = 11.5.sp, fontWeight = FontWeight.SemiBold)
                }
                Spacer(Modifier.weight(1f))
                Text(model.time(message.created), color = c.faint, fontSize = 11.sp, style = TextStyle(fontFeatureSettings = "tnum"))
            }
            Text(
                if (beforeTok > 0 || afterTok > 0) {
                    "上下文已浓缩：${model.formatTokenCount(beforeTok)} → ${model.formatTokenCount(afterTok)} Tokens（共浓缩 $msgCnt 条对话）"
                } else {
                    "上下文已浓缩并归档至记忆库"
                },
                color = c.ink,
                fontSize = 14.sp,
                fontWeight = FontWeight.SemiBold,
                style = TextStyle(fontFeatureSettings = "tnum")
            )
            Column(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(10.dp))
                    .background(c.fill.copy(alpha = 0.72f))
                    .padding(horizontal = 11.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(5.dp)
            ) {
                Text("压缩记忆存储去向：", color = c.muted, fontSize = 11.5.sp, fontWeight = FontWeight.SemiBold)
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "1. 记忆库归档：memory_vault/$vaultPath",
                        Modifier.weight(1f),
                        color = c.ink.copy(alpha = 0.9f),
                        fontSize = 12.sp,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                    Text(
                        "查看归档记忆 ›",
                        Modifier.padding(start = 6.dp)
                            .clip(RoundedCornerShape(6.dp))
                            .background(c.accent.copy(alpha = 0.14f))
                            .clickable { model.openVault(vaultPath) }
                            .padding(horizontal = 7.dp, vertical = 3.dp),
                        color = c.accent,
                        fontSize = 11.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                }
                Text(
                    "2. 时间线与检索库：memory_vault/timeline · SQLite FTS5" + (if (ctxId.isNotBlank()) " ($ctxId)" else ""),
                    color = c.muted,
                    fontSize = 11.5.sp
                )
            }
            Row(
                Modifier.fillMaxWidth()
                    .clickable {
                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                        summaryExpanded = !summaryExpanded
                    }
                    .padding(top = 2.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    if (summaryExpanded) "收起浓缩摘要正文 ▴" else "展开查看浓缩摘要正文 ▾",
                    color = c.accent,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Medium
                )
            }
            AnimatedVisibility(
                visible = summaryExpanded,
                enter = expandVertically(spring(dampingRatio = 0.80f, stiffness = 400f)) + fadeIn(),
                exit = shrinkVertically(spring(dampingRatio = 0.85f, stiffness = 450f)) + fadeOut()
            ) {
                SelectionContainer {
                    MarkdownText(
                        nativeWikiMarkdown(message.text),
                        TextStyle(color = c.ink.copy(alpha = 0.9f), fontSize = 13.5.sp, lineHeight = 20.sp),
                        streaming = false,
                        onOpenUri = model::openLink
                    )
                }
            }
        }
        return
    }

    // One-shot settle entrance only for newly arrived latest messages (< 2s old)
    val reduceMotion = LocalReduceMotion.current
    val isBrandNew = remember(message.id) {
        isLatestMessage && !reduceMotion && message.created > 0.0 &&
            ((System.currentTimeMillis() / 1000.0) - message.created) in -5.0..2.0
    }
    val arriveAnim = remember(message.id) { Animatable(if (isBrandNew) 0f else 1f) }
    LaunchedEffect(message.id, isBrandNew) {
        if (isBrandNew && arriveAnim.value < 0.99f) {
            arriveAnim.animateTo(1f, spring(dampingRatio = 0.82f, stiffness = 380f))
        }
    }
    val arriveShiftPx = with(density) { 8.dp.toPx() }

    // Shared Finger-Tracking Left-Swipe Gesture Modifier
    val swipeGestureModifier = Modifier.pointerInput(message.id) {
        detectHorizontalDragGestures(
            onDragStart = { replyThresholdCrossed = false },
            onHorizontalDrag = { change, dragAmount ->
                if (dragAmount < 0f || swipeOffset.value < 0f) {
                    change.consume()
                    val cur = swipeOffset.value
                    val resistance = if (abs(cur) > replyTriggerPx) 0.34f else 0.78f
                    val next = (cur + dragAmount * resistance).coerceIn(-maxSwipeLeftPx, 0f)
                    if (!replyThresholdCrossed && abs(next) >= replyTriggerPx) {
                        replyThresholdCrossed = true
                        haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                    } else if (replyThresholdCrossed && abs(next) < replyTriggerPx * 0.82f) {
                        replyThresholdCrossed = false
                    }
                    msgScope.launch { swipeOffset.snapTo(next) }
                }
            },
            onDragEnd = {
                val triggered = abs(swipeOffset.value) >= replyTriggerPx
                msgScope.launch {
                    swipeOffset.animateTo(0f, spring(dampingRatio = 0.70f, stiffness = 480f))
                }
                if (triggered) {
                    model.reply(message)
                }
            },
            onDragCancel = {
                msgScope.launch {
                    swipeOffset.animateTo(0f, spring(dampingRatio = 0.75f, stiffness = 460f))
                }
            }
        )
    }

    val swipeRevealFraction = (abs(swipeOffset.value) / revealWidthPx).coerceIn(0f, 1f)
    val replyIconFraction = (abs(swipeOffset.value) / replyTriggerPx).coerceIn(0f, 1.2f)
    val replyIconScale by animateFloatAsState(
        targetValue = if (abs(swipeOffset.value) >= replyTriggerPx) 1.12f else (0.5f + 0.5f * replyIconFraction.coerceAtMost(1f)),
        animationSpec = tween(durationMillis = 120, easing = LinearEasing),
        label = "reply_icon_scale"
    )

    if (user) {
        val rawText = message.text
        val hasQuote = rawText.startsWith("> ") && rawText.contains("\n\n")
        val quotePart = if (hasQuote) rawText.substringBefore("\n\n").lines().joinToString(" ") { it.removePrefix("> ").trim() } else ""
        val rawBody = if (hasQuote) rawText.substringAfter("\n\n") else rawText
        val attachMarker = "\n\n附件（工作区相对路径"
        val hasAttachBlock = rawBody.contains(attachMarker)
        val cleanBody = if (hasAttachBlock) rawBody.substringBefore(attachMarker).trim() else rawBody
        val attachedPaths = if (hasAttachBlock) {
            rawBody.substringAfter(attachMarker).substringAfter("：\n", "")
                .lines().map { it.trim().removePrefix("workspace/").trimStart('/') }.filter { it.isNotBlank() }
        } else emptyList()
        val combinedPaths = remember(attachedPaths, message.attachments) {
            (attachedPaths + message.attachments.map { it.path }).filter { it.isNotBlank() }.distinct()
        }
        val userImagePaths = remember(combinedPaths) { combinedPaths.filter { CloudFiles.isImageFilename(it) } }
        val userDocPaths = remember(combinedPaths) { combinedPaths.filter { !CloudFiles.isImageFilename(it) } }
        val bodyPart = if (cleanBody == "请查看这些附件。" && combinedPaths.isNotEmpty()) "" else cleanBody

        val userBubbleShape = RoundedCornerShape(
            topStart = 18.dp,
            topEnd = if (groupedWithPrev) 6.dp else 18.dp,
            bottomEnd = if (groupedWithNext) 6.dp else 4.dp,
            bottomStart = 18.dp
        )

        Box(
            Modifier.fillMaxWidth().then(swipeGestureModifier),
            contentAlignment = Alignment.CenterEnd
        ) {
            // Emerging Reply Arrow Icon on Right during Left-Swipe
            if (swipeOffset.value < -2f) {
                Box(
                    Modifier.padding(end = 8.dp)
                        .size(30.dp)
                        .graphicsLayer {
                            alpha = replyIconFraction.coerceIn(0f, 1f)
                            scaleX = replyIconScale
                            scaleY = replyIconScale
                        }
                        .clip(CircleShape)
                        .background(c.surface.copy(alpha = 0.90f)),
                    contentAlignment = Alignment.Center
                ) {
                    PulseIcon("reply", c.accent, Modifier.size(16.dp))
                }
            }

            Column(
                Modifier.fillMaxWidth()
                    .padding(start = 54.dp)
                    .graphicsLayer {
                        val p = arriveAnim.value
                        translationX = swipeOffset.value
                        translationY = (1f - p) * arriveShiftPx
                        alpha = 0.86f + 0.14f * p
                        scaleX = 0.975f + 0.025f * p
                        scaleY = 0.975f + 0.025f * p
                    },
                horizontalAlignment = Alignment.End,
                verticalArrangement = Arrangement.spacedBy(5.dp)
            ) {
                if (userImagePaths.isNotEmpty()) {
                    PulseUserMediaGrid(model = model, paths = userImagePaths, alignEnd = true)
                }
                userDocPaths.forEach { docRel ->
                    val attMeta = message.attachments.firstOrNull { it.path == docRel }
                    PulseFileAttachmentCard(
                        model = model,
                        path = docRel,
                        displayName = attMeta?.name ?: "",
                        fileSize = attMeta?.size ?: 0L
                    )
                }

                val hasTextOrQuoteBubble = bodyPart.isNotBlank() || quotePart.isNotBlank() || combinedPaths.isEmpty()
                if (hasTextOrQuoteBubble) {
                    Column(
                        Modifier.clip(userBubbleShape)
                            .background(c.userBubble)
                            .combinedClickable(
                                onClick = {
                                    haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                    showActions = !showActions
                                },
                                onDoubleClick = {
                                    haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                                    model.reply(message)
                                },
                                onLongClick = {
                                    haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                                    showActions = true
                                }
                            )
                            .padding(horizontal = 13.dp, vertical = 8.dp)
                    ) {
                        if (quotePart.isNotBlank()) {
                            Row(
                                Modifier.padding(bottom = 5.dp)
                                    .clip(RoundedCornerShape(8.dp))
                                    .background(Color.White.copy(alpha = 0.15f))
                                    .padding(horizontal = 8.dp, vertical = 5.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Box(
                                    Modifier.width(2.5.dp).height(24.dp)
                                        .clip(RoundedCornerShape(1.5.dp))
                                        .background(Color.White.copy(alpha = 0.9f))
                                )
                                Column(Modifier.padding(start = 7.dp)) {
                                    Text("引用消息", color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
                                    Text(
                                        quotePart,
                                        color = Color.White.copy(alpha = 0.88f),
                                        fontSize = 12.sp,
                                        maxLines = 2,
                                        overflow = TextOverflow.Ellipsis
                                    )
                                }
                            }
                        }
                        if (bodyPart.isNotBlank()) {
                            Text(bodyPart, color = c.userInk, fontSize = 16.sp, lineHeight = 21.5.sp)
                        }
                        Row(
                            Modifier.align(Alignment.End).padding(top = 2.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(3.dp)
                        ) {
                            Text(
                                if (message.optimistic) "发送中" else model.time(message.created),
                                color = c.userMeta,
                                fontSize = 10.5.sp,
                                style = TextStyle(fontFeatureSettings = "tnum")
                            )
                            PulseIcon(
                                if (message.optimistic) "check" else "check_double",
                                c.userMeta,
                                Modifier.size(13.dp)
                            )
                        }
                    }
                } else {
                    Row(
                        Modifier
                            .clip(RoundedCornerShape(10.dp))
                            .combinedClickable(
                                onClick = {
                                    haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                    showActions = !showActions
                                },
                                onDoubleClick = {
                                    haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                                    model.reply(message)
                                },
                                onLongClick = {
                                    haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                                    showActions = true
                                }
                            )
                            .padding(horizontal = 6.dp, vertical = 2.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(3.dp)
                    ) {
                        Text(
                            if (message.optimistic) "发送中" else model.time(message.created),
                            color = c.muted,
                            fontSize = 10.5.sp,
                            style = TextStyle(fontFeatureSettings = "tnum")
                        )
                        PulseIcon(
                            if (message.optimistic) "check" else "check_double",
                            c.muted,
                            Modifier.size(13.dp)
                        )
                    }
                }

                // Spring-Animated Telegram Action Pill Menu
                AnimatedVisibility(
                    visible = showActions,
                    enter = expandVertically(spring(dampingRatio = 0.75f, stiffness = 440f)) +
                        scaleIn(spring(dampingRatio = 0.75f, stiffness = 440f), initialScale = 0.88f) +
                        fadeIn(tween(140)),
                    exit = shrinkVertically(spring(dampingRatio = 0.85f, stiffness = 480f)) +
                        scaleOut(targetScale = 0.90f) +
                        fadeOut(tween(110))
                ) {
                    Row(
                        Modifier.padding(top = 5.dp)
                            .clip(RoundedCornerShape(12.dp))
                            .background(c.surface)
                            .border(0.5.dp, c.line.copy(alpha = 0.7f), RoundedCornerShape(12.dp))
                            .padding(horizontal = 12.dp, vertical = 7.dp),
                        horizontalArrangement = Arrangement.spacedBy(14.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        TelegramActionChip("copy", "复制", c.ink) { model.copy(bodyPart.ifBlank { cleanBody.ifBlank { rawText } }); showActions = false }
                        TelegramActionChip("reply", "引用", c.ink) { model.reply(message); showActions = false }
                        TelegramActionChip("edit", "编辑", c.accent) { model.startEditingMessage(message); showActions = false }
                        TelegramActionChip("spark", "重发", c.muted) { model.retryMessage(message); showActions = false }
                    }
                }
            }
        }
        return
    }

    val speakerBot = message.fromBotId.takeIf { it.isNotBlank() }?.let { model.bot(it) } ?: bot
    val isOtherBot = speakerBot.id != bot.id || isBotToBotIn || isBotToBotReply
    val speakerColor = remember(speakerBot.color) {
        runCatching { Color(android.graphics.Color.parseColor(speakerBot.color)) }.getOrDefault(c.accent)
    }
    val trace = remember(message.trace) { runCatching { JSONArray(message.trace).objects() }.getOrElse { emptyList() } }
    val turnSteps = remember(message.turnSteps) { runCatching { JSONArray(message.turnSteps).objects() }.getOrElse { emptyList() } }
    val timelineItems = remember(message.timeline) { runCatching { JSONArray(message.timeline).objects() }.getOrElse { emptyList() } }

    val aiBubbleShape = RoundedCornerShape(
        topStart = if (groupedWithPrev) 6.dp else 18.dp,
        topEnd = 18.dp,
        bottomEnd = 18.dp,
        bottomStart = if (groupedWithNext) 6.dp else 4.dp
    )

    val telemetryRow1 = buildList {
        if (message.cacheHitPct > 0) add("缓存${message.cacheHitPct}%")
        if (message.ttftMs > 0) {
            add(
                if (message.ttftMs >= 1000) String.format(java.util.Locale.US, "首字%.1fs", message.ttftMs / 1000.0)
                else "首字${message.ttftMs}ms"
            )
        }
    }.joinToString(" · ")
    val telemetryRow2 = buildList {
        if (message.tps > 0.0) add(String.format(java.util.Locale.US, "%.0ft/s", message.tps))
        if (message.apiCalls > 1) add("${message.apiCalls}轮")
        if (message.created > 0.0) add(model.time(message.created))
    }.joinToString(" · ")

    Box(
        Modifier.fillMaxWidth().then(swipeGestureModifier),
        contentAlignment = Alignment.CenterStart
    ) {
        // 1:1 linear iMessage-style right reveal (telemetry + quote reply cue) on left-swipe
        if (swipeOffset.value < -1.5f) {
            val slideShiftPx = with(density) { 14.dp.toPx() } * (1f - swipeRevealFraction)
            Row(
                Modifier.align(Alignment.CenterEnd)
                    .padding(end = 4.dp)
                    .graphicsLayer {
                        alpha = swipeRevealFraction
                        translationX = slideShiftPx
                    },
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                Column(horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(1.5.dp)) {
                    if (telemetryRow1.isNotEmpty()) {
                        Text(
                            telemetryRow1,
                            color = c.muted,
                            fontSize = 10.sp,
                            style = TextStyle(fontFeatureSettings = "tnum")
                        )
                    }
                    if (telemetryRow2.isNotEmpty()) {
                        Text(
                            telemetryRow2,
                            color = c.faint,
                            fontSize = 10.sp,
                            style = TextStyle(fontFeatureSettings = "tnum")
                        )
                    }
                }
                if (abs(swipeOffset.value) >= replyTriggerPx * 0.65f) {
                    Box(
                        Modifier.size(26.dp)
                            .graphicsLayer {
                                scaleX = replyIconScale
                                scaleY = replyIconScale
                            }
                            .clip(CircleShape)
                            .background(c.surface.copy(alpha = 0.92f)),
                        contentAlignment = Alignment.Center
                    ) {
                        PulseIcon("reply", c.accent, Modifier.size(14.dp))
                    }
                }
            }
        }

        Column(
            Modifier.fillMaxWidth()
                .padding(end = 42.dp)
                .graphicsLayer {
                    val p = arriveAnim.value
                    translationX = swipeOffset.value
                    translationY = (1f - p) * arriveShiftPx
                    alpha = 0.86f + 0.14f * p
                    scaleX = 0.975f + 0.025f * p
                    scaleY = 0.975f + 0.025f * p
                },
            verticalArrangement = Arrangement.spacedBy(5.dp)
        ) {
            val cleanThought = message.thought.trim().takeIf { it.isNotEmpty() && !it.startsWith("正在生成 ") && !it.startsWith("正在准备调用 ") }.orEmpty()

            // 2. Sticker / Returned Image rendered cleanly
            if (message.sticker.isNotBlank() && !message.sticker.endsWith("/路径")) {
                PulseStickerImage(model, message.sticker)
            }
            val inlineMdImages = remember(message.text) {
                Regex("""!\[[^\]]*\]\(([^)\s]+)\)""")
                    .findAll(message.text)
                    .map { it.groupValues[1].trim() }
                    .filter { u ->
                        u.isNotBlank() &&
                            !u.endsWith("/路径") &&
                            (u.startsWith("/workspace-media/") || u.startsWith("http://") || u.startsWith("https://") || CloudFiles.isImageFilename(u))
                    }
                    .toList()
            }
            val aiAttImages = remember(message.attachments, message.sticker, inlineMdImages) {
                (message.attachments.map { it.path }.filter { CloudFiles.isImageFilename(it) && it != message.sticker } +
                    inlineMdImages.filter { it != message.sticker })
                    .distinct()
            }
            val aiAttDocs = remember(message.attachments) {
                message.attachments.filter { !CloudFiles.isImageFilename(it.path) }
            }
            if (aiAttImages.isNotEmpty()) {
                PulseUserMediaGrid(model = model, paths = aiAttImages, alignEnd = false)
            }

            // 4. Framed Assistant Card Bubble (distinct surface + crisp 0.8dp hairline border)
            val isCollabBubble = isBotToBotIn || isBotToBotReply
            val collabPurple = Color(0xFFB568F7)
            val aiCleanText = remember(message.text, inlineMdImages) {
                if (inlineMdImages.isEmpty()) message.text
                else message.text
                    .replace(Regex("""!\[[^\]]*\]\(([^)\s]+)\)""")) { m ->
                        val target = m.groupValues[1].trim()
                        if (target in inlineMdImages) "" else m.value
                    }
                    .replace(Regex("""\n{3,}"""), "\n\n")
                    .trim()
            }
            val hasBubbleContent = aiCleanText.isNotBlank() || isError || timelineItems.isNotEmpty() || trace.isNotEmpty() || cleanThought.isNotBlank()
            if (hasBubbleContent) {
                val containerBg = when {
                    isCollabBubble -> c.elevated.copy(alpha = 0.96f)
                    isError -> c.warn.copy(alpha = 0.08f)
                    else -> c.aiBubble
                }
                val bubbleBorder = when {
                    isCollabBubble -> collabPurple.copy(alpha = 0.38f)
                    isError -> c.warn.copy(alpha = 0.42f)
                    else -> if (s.dark) Color(0x28FFFFFF) else Color(0x1E000000)
                }
                Column(
                    Modifier.clip(aiBubbleShape)
                        .background(containerBg)
                        .border(0.8.dp, bubbleBorder, aiBubbleShape)
                        .combinedClickable(
                            onClick = {
                                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                showActions = !showActions
                            },
                            onDoubleClick = {
                                haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                                model.reply(message)
                            },
                            onLongClick = {
                                haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                                showActions = true
                            }
                        )
                        .padding(horizontal = 14.dp, vertical = 10.dp),
                    verticalArrangement = Arrangement.spacedBy(5.dp)
                ) {
                    if (isCollabBubble || ((isOtherBot || isNotification || message.kind == "curiosity") && !groupedWithPrev)) {
                        val headerTitle = when {
                            isBotToBotIn && message.direction == "outbound" ->
                                "🟣 委派给 ${message.toBotName.ifBlank { speakerBot.name }} · 协作派单"
                            isBotToBotIn ->
                                "🟣 来自 ${message.fromBotName.ifBlank { speakerBot.name }} · 协作委托"
                            isBotToBotReply && message.direction == "outbound" ->
                                "🟣 回复 ${message.toBotName.ifBlank { "主助理" }} · 协作结论"
                            isBotToBotReply ->
                                "🟣 来自 ${message.fromBotName.ifBlank { speakerBot.name }} · 协作回信"
                            else ->
                                message.fromBotName.ifBlank { speakerBot.name }
                        }
                        if (isCollabBubble) {
                            Row(
                                Modifier.clip(RoundedCornerShape(8.dp))
                                    .background(collabPurple.copy(alpha = 0.16f))
                                    .border(0.6.dp, collabPurple.copy(alpha = 0.38f), RoundedCornerShape(8.dp))
                                    .padding(horizontal = 8.dp, vertical = 3.5.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text(
                                    headerTitle,
                                    fontSize = 12.sp,
                                    color = collabPurple,
                                    fontWeight = FontWeight.SemiBold
                                )
                            }
                        } else {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    headerTitle,
                                    fontSize = 13.sp,
                                    color = if (isOtherBot) speakerColor else c.accent,
                                    fontWeight = FontWeight.SemiBold
                                )
                                if (message.kind == "curiosity") {
                                    Text(" · 主动关心", fontSize = 11.sp, color = c.accent)
                                }
                            }
                        }
                    }
                    val isTypingThis = s.activeTypewriterMsgId == message.id && s.activeTypewriterChars < aiCleanText.length
                    UnifiedTimelineBubbleContent(
                        model = model,
                        cardKey = message.id,
                        timeline = timelineItems,
                        fallbackTrace = trace,
                        fallbackText = aiCleanText,
                        thought = cleanThought,
                        isLive = false,
                        isError = isError,
                        typewriterCharBudget = if (isTypingThis) s.activeTypewriterChars else Int.MAX_VALUE,
                        inlineStripRegex = Regex("""!\[[^\]]*\]\(([^)\s]+)\)"""),
                        inlineStripSet = inlineMdImages,
                    )
                    Row(
                        Modifier.align(Alignment.End),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        if (trace.any {
                                it.optString("tool").startsWith("phone") &&
                                    it.optJSONObject("arguments")?.optString("op") !in listOf("notifications", "get_notifications", "recent_notifications", "notifs", "notification")
                            }
                        ) {
                            Text(
                                "虚拟屏 ›",
                                Modifier.clickable { model.launchFloatingVirtualDisplay() },
                                color = c.accent,
                                fontSize = 11.sp,
                                fontWeight = FontWeight.Medium
                            )
                        }
                        Text(
                            model.time(message.created),
                            color = c.faint,
                            fontSize = 10.5.sp,
                            style = TextStyle(fontFeatureSettings = "tnum")
                        )
                    }
                }
            }

            if (aiAttDocs.isNotEmpty()) {
                Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
                    aiAttDocs.forEach { att ->
                        PulseFileAttachmentCard(
                            model = model,
                            path = att.path,
                            displayName = att.name,
                            fileSize = att.size
                        )
                    }
                }
            }

            if (isError) {
                PulseAction("让主助理自动修复模型配置并重试") { model.askPrimaryToHealError(message) }
            }

            // 5. Telegram Inline Keyboard Buttons
            if (isLatestMessage && message.id !in s.consumedChoiceIds && message.choices.isNotEmpty()) {
                Column(
                    Modifier.fillMaxWidth()
                        .heightIn(max = 220.dp)
                        .verticalScroll(rememberScrollState())
                        .padding(top = 2.dp),
                    verticalArrangement = Arrangement.spacedBy(5.dp)
                ) {
                    message.choices.forEach { choice ->
                        Box(
                            Modifier.fillMaxWidth()
                                .clip(RoundedCornerShape(11.dp))
                                .background(c.surface.copy(alpha = 0.90f))
                                .iosPressScale(0.97f) {
                                    haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                    model.consumeChoiceAndSend(message.id, choice.value)
                                }
                                .padding(horizontal = 14.dp, vertical = 9.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                "✦ ${choice.label}",
                                modifier = Modifier.fillMaxWidth().basicMarquee(iterations = Int.MAX_VALUE),
                                color = c.accent,
                                fontSize = 13.5.sp,
                                fontWeight = FontWeight.SemiBold,
                                maxLines = 1,
                                textAlign = TextAlign.Center
                            )
                        }
                    }
                }
            }

            if (message.variants.size > 1) {
                val totalV = message.variants.size
                val curV = message.activeVariantIdx.coerceIn(0, totalV - 1)
                val prevV = if (curV > 0) curV - 1 else totalV - 1
                val nextV = (curV + 1) % totalV
                Row(
                    Modifier.padding(top = 2.dp)
                        .clip(RoundedCornerShape(10.dp))
                        .background(c.surface.copy(alpha = 0.85f))
                        .border(0.5.dp, c.line.copy(alpha = 0.6f), RoundedCornerShape(10.dp))
                        .padding(horizontal = 6.dp, vertical = 3.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(3.dp)
                ) {
                    Text(
                        "‹",
                        Modifier.clip(RoundedCornerShape(6.dp))
                            .clickable { model.switchMessageVariant(message, prevV) }
                            .padding(horizontal = 6.dp, vertical = 2.dp),
                        color = c.muted,
                        fontSize = 13.sp,
                        fontWeight = FontWeight.Bold
                    )
                    if (totalV <= 6) {
                        for (vIdx in 0 until totalV) {
                            val isSel = vIdx == curV
                            Box(
                                Modifier.clip(RoundedCornerShape(6.dp))
                                    .background(if (isSel) c.accent else Color.Transparent)
                                    .clickable { if (!isSel) model.switchMessageVariant(message, vIdx) }
                                    .padding(horizontal = 7.dp, vertical = 2.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text(
                                    "${vIdx + 1}",
                                    color = if (isSel) Color.White else c.muted,
                                    fontSize = 11.5.sp,
                                    fontWeight = if (isSel) FontWeight.Bold else FontWeight.Medium,
                                    style = TextStyle(fontFeatureSettings = "tnum")
                                )
                            }
                        }
                    } else {
                        Text(
                            "${curV + 1} / $totalV",
                            Modifier.padding(horizontal = 6.dp),
                            color = c.ink,
                            fontSize = 11.5.sp,
                            fontWeight = FontWeight.SemiBold,
                            style = TextStyle(fontFeatureSettings = "tnum")
                        )
                    }
                    Text(
                        "›",
                        Modifier.clip(RoundedCornerShape(6.dp))
                            .clickable { model.switchMessageVariant(message, nextV) }
                            .padding(horizontal = 6.dp, vertical = 2.dp),
                        color = c.muted,
                        fontSize = 13.sp,
                        fontWeight = FontWeight.Bold
                    )
                }
            }

            // 6. Linear-Animated Floating Action Pill Menu + Detailed Telemetry Inset
            AnimatedVisibility(
                visible = showActions,
                enter = expandVertically(tween(200, easing = LinearOutSlowInEasing)) +
                    scaleIn(tween(180, easing = LinearEasing), initialScale = 0.94f) +
                    fadeIn(tween(150, easing = LinearEasing)),
                exit = shrinkVertically(tween(150, easing = FastOutLinearInEasing)) +
                    scaleOut(tween(130, easing = LinearEasing), targetScale = 0.94f) +
                    fadeOut(tween(110, easing = LinearEasing))
            ) {
                Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
                    val fullTelemetry = listOfNotNull(
                        message.engine.takeIf { it.isNotBlank() },
                        telemetryRow1.takeIf { it.isNotBlank() },
                        telemetryRow2.takeIf { it.isNotBlank() }
                    ).joinToString(" · ")
                    if (fullTelemetry.isNotBlank()) {
                        Text(
                            fullTelemetry,
                            color = c.muted,
                            fontSize = 11.sp,
                            style = TextStyle(fontFeatureSettings = "tnum"),
                            modifier = Modifier.padding(horizontal = 4.dp)
                        )
                    }
                    Row(
                        Modifier.clip(RoundedCornerShape(12.dp))
                            .background(c.surface)
                            .border(0.5.dp, c.line.copy(alpha = 0.7f), RoundedCornerShape(12.dp))
                            .padding(horizontal = 12.dp, vertical = 7.dp),
                        horizontalArrangement = Arrangement.spacedBy(14.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        TelegramActionChip("copy", "复制", c.ink) { model.copy(message.text); showActions = false }
                        TelegramActionChip("reply", "引用", c.ink) { model.reply(message); showActions = false }
                        TelegramActionChip("spark", "重生成", c.ink) { model.rerollMessage(message); showActions = false }
                        TelegramActionChip("pin", "钉住记忆", c.accent) {
                            val t = message.text.trim()
                            val clipped = if (t.length <= 280) t else {
                                val cut = if (t[279].isHighSurrogate()) 279 else 280
                                t.substring(0, cut) + "…"
                            }
                            model.addMemoryPin(clipped)
                            showActions = false
                        }
                    }
                }
            }
        }
    }
}

