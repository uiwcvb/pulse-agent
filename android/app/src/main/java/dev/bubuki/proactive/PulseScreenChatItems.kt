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
 * 貼紙表情包縮略圖與大圖渲染 (PulseStickerThumb, PulseStickerImage)，以及快捷動作膠囊 (TelegramActionChip)。
 *
 * [Public API]
 * - NativeMessage(...)
 */

private val RE_PURE_STICKER_TAG = Regex("""^\[(sticker|表情|贴纸)\s*[:：][^\]]+\]$""", RegexOption.IGNORE_CASE)
private val RE_INLINE_MD_IMAGE = Regex("""!\[[^\]]*\]\(([^)\s]+)\)""")
private val RE_ITEM_MULTI_NL = Regex("""\n{3,}""")

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

    // All compaction summaries are completely hidden from the chat conversation stream.
    if (message.kind == "compact_summary") return

    // Fluid launch entrance for newly sent / arrived messages (bubble rises from composer with spring physics)
    val reduceMotion = rememberReducedMotion()
    val isBrandNew = remember(message.id) {
        !reduceMotion && (
            message.optimistic || (
                isLatestMessage && message.created > 0.0 &&
                    ((System.currentTimeMillis() / 1000.0) - message.created) in -5.0..3.0
                )
            )
    }
    val arriveAnim = remember(message.id) { Animatable(if (isBrandNew) 0f else 1f) }
    LaunchedEffect(message.id, isBrandNew) {
        if (isBrandNew && arriveAnim.value < 0.99f) {
            arriveAnim.animateTo(1f, spring(dampingRatio = 0.78f, stiffness = 360f))
        }
    }
    val arriveShiftPx = with(density) { (if (user) 38.dp else 18.dp).toPx() }

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
                    if (reduceMotion) swipeOffset.snapTo(0f) else swipeOffset.animateTo(0f, spring(dampingRatio = 0.74f, stiffness = 480f))
                }
                if (triggered) {
                    model.reply(message)
                }
            },
            onDragCancel = {
                msgScope.launch {
                    if (reduceMotion) swipeOffset.snapTo(0f) else swipeOffset.animateTo(0f, spring(dampingRatio = 0.78f, stiffness = 460f))
                }
            }
        )
    }

    val showSwipeReveal by remember(message.id) { derivedStateOf { swipeOffset.value < -1.5f } }
    val showUserSwipeIcon by remember(message.id) { derivedStateOf { swipeOffset.value < -2f } }
    val showReplyCueIcon by remember(message.id, replyTriggerPx) { derivedStateOf { abs(swipeOffset.value) >= replyTriggerPx * 0.65f } }
    val replyTriggered by remember(message.id, replyTriggerPx) { derivedStateOf { abs(swipeOffset.value) >= replyTriggerPx } }
    val replyCueTargetScale by animateFloatAsState(
        targetValue = if (replyTriggered) 1.12f else 0.90f,
        animationSpec = if (reduceMotion) snap() else iosInteractiveSpring(),
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
            if (showUserSwipeIcon) {
                Box(
                    Modifier.padding(end = 8.dp)
                        .size(30.dp)
                        .graphicsLayer {
                            val iconFrac = (abs(swipeOffset.value) / replyTriggerPx).coerceIn(0f, 1.2f)
                            val sc = if (replyTriggered) replyCueTargetScale else (0.5f + 0.5f * iconFrac.coerceAtMost(1f))
                            alpha = iconFrac.coerceIn(0f, 1f)
                            scaleX = sc
                            scaleY = sc
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
                val hasUserSticker = message.sticker.contains("/stickers/")
                val isPureUserStickerText = hasUserSticker &&
                    RE_PURE_STICKER_TAG.matches(bodyPart.trim())
                val effectiveUserBody = if (isPureUserStickerText) "" else bodyPart
                if (hasUserSticker) {
                    PulseStickerImage(model, message.sticker)
                }
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

                val hasTextOrQuoteBubble = effectiveUserBody.isNotBlank() || quotePart.isNotBlank() || (combinedPaths.isEmpty() && !hasUserSticker)
                if (hasTextOrQuoteBubble) {
                    Column(
                        Modifier.clip(userBubbleShape)
                            .background(c.userBubble)
                            .combinedClickable(
                                onClick = {},
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
                                Modifier.padding(bottom = 6.dp)
                                    .clip(RoundedCornerShape(10.dp))
                                    .background(c.userInk.copy(alpha = 0.12f))
                                    .padding(horizontal = 8.dp, vertical = 5.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Box(
                                    Modifier.width(2.5.dp).height(24.dp)
                                        .clip(RoundedCornerShape(1.5.dp))
                                        .background(c.userInk.copy(alpha = 0.78f))
                                )
                                Column(Modifier.padding(start = 7.dp)) {
                                    Text("引用消息", color = c.userInk, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
                                    Text(
                                        quotePart,
                                        color = c.userInk.copy(alpha = 0.82f),
                                        fontSize = 12.sp,
                                        maxLines = 2,
                                        overflow = TextOverflow.Ellipsis
                                    )
                                }
                            }
                        }
                        if (effectiveUserBody.isNotBlank()) {
                            Text(effectiveUserBody, color = c.userInk, fontSize = 15.5.sp, lineHeight = 22.sp)
                        }
                        if (message.optimistic) {
                            Row(
                                Modifier.align(Alignment.End).padding(top = 2.dp),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(3.dp)
                            ) {
                                Text(
                                    "发送中",
                                    color = c.userMeta,
                                    fontSize = 10.5.sp,
                                    style = TextStyle(fontFeatureSettings = "tnum")
                                )
                                PulseIcon("check", c.userMeta, Modifier.size(12.dp))
                            }
                        }
                    }
                } else {
                    Row(
                        Modifier
                            .clip(RoundedCornerShape(10.dp))
                            .combinedClickable(
                                onClick = {},
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

            }
            val userPreviewText = bodyPart.ifBlank { cleanBody.ifBlank { rawText } }.trim()
            AppleMessageContextOverlay(
                visible = showActions,
                previewText = userPreviewText,
                telemetry = if (message.optimistic) "正在发送" else model.time(message.created),
                actions = listOf(
                    MessageContextAction("copy", "复制内容", c.ink) { model.copy(userPreviewText); showActions = false },
                    MessageContextAction("reply", "引用回复", c.ink) { model.reply(message); showActions = false },
                    MessageContextAction("edit", "编辑重发", c.accent) { model.startEditingMessage(message); showActions = false },
                    MessageContextAction("refresh", "重新发送", c.muted) { model.retryMessage(message); showActions = false }
                ),
                onDismiss = { showActions = false }
            )
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
        if (showSwipeReveal) {
            val baseSlidePx = with(density) { 14.dp.toPx() }
            Row(
                Modifier.align(Alignment.CenterEnd)
                    .padding(end = 4.dp)
                    .graphicsLayer {
                        val frac = (abs(swipeOffset.value) / revealWidthPx).coerceIn(0f, 1f)
                        alpha = frac
                        translationX = baseSlidePx * (1f - frac)
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
                if (showReplyCueIcon) {
                    Box(
                        Modifier.size(26.dp)
                            .graphicsLayer {
                                val iconFrac = (abs(swipeOffset.value) / replyTriggerPx).coerceIn(0f, 1.2f)
                                val sc = if (replyTriggered) replyCueTargetScale else (0.5f + 0.5f * iconFrac.coerceAtMost(1f))
                                scaleX = sc
                                scaleY = sc
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
                RE_INLINE_MD_IMAGE
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
                    .replace(RE_INLINE_MD_IMAGE) { m ->
                        val target = m.groupValues[1].trim()
                        if (target in inlineMdImages) "" else m.value
                    }
                    .replace(RE_ITEM_MULTI_NL, "\n\n")
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
                    else -> c.line.copy(alpha = 0.72f)
                }
                Column(
                    Modifier.clip(aiBubbleShape)
                        .background(containerBg)
                        .border(0.5.dp, bubbleBorder, aiBubbleShape)
                        .combinedClickable(
                            onClick = {},
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
                                "委派给 ${message.toBotName.ifBlank { speakerBot.name }} · 协作派单"
                            isBotToBotIn ->
                                "来自 ${message.fromBotName.ifBlank { speakerBot.name }} · 协作委托"
                            isBotToBotReply && message.direction == "outbound" ->
                                "回复 ${message.toBotName.ifBlank { "主助理" }} · 协作结论"
                            isBotToBotReply ->
                                "来自 ${message.fromBotName.ifBlank { speakerBot.name }} · 协作回信"
                            else ->
                                message.fromBotName.ifBlank { speakerBot.name }
                        }
                        if (isCollabBubble) {
                            Row(
                                Modifier.clip(RoundedCornerShape(8.dp))
                                    .background(collabPurple.copy(alpha = 0.14f))
                                    .border(0.5.dp, collabPurple.copy(alpha = 0.34f), RoundedCornerShape(8.dp))
                                    .padding(horizontal = 8.dp, vertical = 4.dp),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(5.dp)
                            ) {
                                PulseIcon("share", collabPurple, Modifier.size(12.5.dp))
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
                    UnifiedTimelineBubbleContent(
                        model = model,
                        cardKey = message.id,
                        timeline = timelineItems,
                        fallbackTrace = trace,
                        fallbackText = aiCleanText,
                        thought = cleanThought,
                        isLive = false,
                        isError = isError,
                        turnId = message.turnId,
                        inlineStripRegex = RE_INLINE_MD_IMAGE,
                        inlineStripSet = inlineMdImages,
                    )
                    Row(
                        Modifier.align(Alignment.End),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        if (trace.any {
                                val tn = it.optString("tool")
                                tn == "cloud" || tn == "cloud_desktop" || tn == "cloud_computer"
                            }
                        ) {
                            Box(
                                Modifier.heightIn(min = 34.dp)
                                    .clip(RoundedCornerShape(10.dp))
                                    .background(c.accent.copy(alpha = 0.12f))
                                    .iosPressScale(0.95f) { model.openCloudDesktop() }
                                    .padding(horizontal = 10.dp, vertical = 5.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text("查看电脑 ›", color = c.accent, fontSize = 11.5.sp, fontWeight = FontWeight.SemiBold)
                            }
                        }
                        if (trace.any {
                                it.optString("tool").startsWith("phone") &&
                                    it.optJSONObject("arguments")?.optString("op") !in listOf("notifications", "get_notifications", "recent_notifications", "notifs", "notification")
                            }
                        ) {
                            Box(
                                Modifier.heightIn(min = 36.dp)
                                    .clip(RoundedCornerShape(6.dp))
                                    .iosPressScale(0.95f) { model.launchFloatingVirtualDisplay() }
                                    .padding(horizontal = 6.dp, vertical = 4.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text("虚拟屏 ›", color = c.accent, fontSize = 11.sp, fontWeight = FontWeight.Medium)
                            }
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
                                .heightIn(min = 40.dp)
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
                                modifier = Modifier.fillMaxWidth().then(
                                    if (reduceMotion) Modifier else Modifier.basicMarquee(iterations = 2)
                                ),
                                color = c.accent,
                                fontSize = 13.5.sp,
                                fontWeight = FontWeight.SemiBold,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
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
                        .padding(horizontal = 2.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(2.dp)
                ) {
                    Box(
                        Modifier.defaultMinSize(minWidth = 44.dp, minHeight = 44.dp)
                            .clip(RoundedCornerShape(8.dp))
                            .iosPressScale(0.92f) {
                                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                model.switchMessageVariant(message, prevV)
                            },
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            "‹",
                            color = c.muted,
                            fontSize = 14.sp,
                            fontWeight = FontWeight.Bold
                        )
                    }
                    if (totalV <= 6) {
                        for (vIdx in 0 until totalV) {
                            val isSel = vIdx == curV
                            Box(
                                Modifier.defaultMinSize(minWidth = 44.dp, minHeight = 44.dp)
                                    .clip(RoundedCornerShape(8.dp))
                                    .iosPressScale(0.92f, enabled = !isSel) {
                                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                        model.switchMessageVariant(message, vIdx)
                                    },
                                contentAlignment = Alignment.Center
                            ) {
                                Box(
                                    Modifier.clip(RoundedCornerShape(6.dp))
                                        .background(if (isSel) c.accent else Color.Transparent)
                                        .padding(horizontal = 9.dp, vertical = 3.dp),
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
                    Box(
                        Modifier.defaultMinSize(minWidth = 44.dp, minHeight = 44.dp)
                            .clip(RoundedCornerShape(8.dp))
                            .iosPressScale(0.92f) { model.switchMessageVariant(message, nextV) },
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            "›",
                            color = c.muted,
                            fontSize = 14.sp,
                            fontWeight = FontWeight.Bold
                        )
                    }
                }
            }

            val fullTelemetry = listOfNotNull(
                message.engine.takeIf { it.isNotBlank() },
                telemetryRow1.takeIf { it.isNotBlank() },
                telemetryRow2.takeIf { it.isNotBlank() }
            ).joinToString(" · ")
            AppleMessageContextOverlay(
                visible = showActions,
                previewText = aiCleanText.ifBlank { message.text }.trim(),
                telemetry = fullTelemetry,
                actions = listOf(
                    MessageContextAction("copy", "复制回复", c.ink) { model.copy(message.text); showActions = false },
                    MessageContextAction("reply", "引用回复", c.ink) { model.reply(message); showActions = false },
                    MessageContextAction("refresh", "重新生成", c.ink) { model.rerollMessage(message); showActions = false },
                    MessageContextAction("pin", "钉住核心记忆", c.accent) {
                        val t = message.text.trim()
                        val clipped = if (t.length <= 280) t else {
                            val cut = if (t[279].isHighSurrogate()) 279 else 280
                            t.substring(0, cut) + "…"
                        }
                        model.addMemoryPin(clipped)
                        showActions = false
                    }
                ),
                onDismiss = { showActions = false }
            )
        }
    }
}

