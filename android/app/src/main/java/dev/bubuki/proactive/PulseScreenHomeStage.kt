package dev.bubuki.proactive

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import kotlin.math.abs

/**
 * [Module] PulseScreenHomeStage
 * [Responsibility]
 * Minimalist Apple-HIG Hero Bot Stage home screen (`NativeHome`): enlarged living customizable
 * avatar (shape/color/blinking/gaze tracking), horizontal parallax bot switcher, single Settings
 * icon consolidating all secondary tools, and shared floating oval quick composer.
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
internal fun NativeHome(
    model: NativePulseModel,
    modifier: Modifier,
    phoneState: RootPhoneState,
    selectedBotId: String? = null
) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    val focus = LocalFocusManager.current
    val reduceMotion = rememberReducedMotion()
    val homeScope = rememberCoroutineScope()
    val allBots = remember(s.bots, s.messages, s.workingBot, s.streamBotId, s.busyBot) {
        model.visibleBots()
    }
    val initialIdx = remember(allBots, selectedBotId, s.botId) {
        val targetId = selectedBotId ?: s.botId
        allBots.indexOfFirst { it.id == targetId }.takeIf { it >= 0 } ?: 0
    }
    var curIdx by rememberSaveable { mutableIntStateOf(initialIdx) }
    val safeIdx = curIdx.coerceIn(0, (allBots.size - 1).coerceAtLeast(0))
    val activeBot = allBots.getOrNull(safeIdx)
    val prevBot = if (safeIdx > 0) allBots.getOrNull(safeIdx - 1) else null
    val nextBot = if (safeIdx < allBots.lastIndex) allBots.getOrNull(safeIdx + 1) else null
    val dragX = remember { Animatable(0f) }
    val dragY = remember { Animatable(0f) }
    var quickInput by rememberSaveable { mutableStateOf("") }
    val apiReady = s.hasApiKey && s.defaultModel.isNotBlank()
    val dateHeader = remember {
        java.text.SimpleDateFormat("M月d日 E", java.util.Locale.CHINA).apply {
            timeZone = java.util.TimeZone.getTimeZone("Asia/Shanghai")
        }.format(java.util.Date())
    }

    Column(
        modifier
            .background(c.bg)
            .padding(horizontal = 16.dp)
    ) {
        // 1. Minimalist Apple Top Bar: Date + Single Settings Icon
        Row(
            Modifier.fillMaxWidth().padding(top = 12.dp, bottom = 6.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(Modifier.weight(1f)) {
                Text(
                    dateHeader,
                    color = c.muted,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Medium
                )
            }
            if (!apiReady) {
                Box(
                    Modifier.padding(end = 8.dp)
                        .clip(CircleShape)
                        .background(c.accent)
                        .iosPressScale(0.94f) { model.openSettings() }
                        .padding(horizontal = 12.dp, vertical = 7.dp)
                ) {
                    Text("配置 API", color = c.userInk, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                }
            }
            Box(
                Modifier.size(40.dp)
                    .clip(CircleShape)
                    .background(c.surface)
                    .iosPressScale(0.92f) {
                        focus.clearFocus()
                        model.openSettings()
                    }
                    .semantics { contentDescription = "设置与工具" },
                contentAlignment = Alignment.Center
            ) {
                PulseIcon("settings", c.ink, Modifier.size(19.dp))
            }
        }

        // 2. Hero Living Avatar Stage
        BoxWithConstraints(
            Modifier.weight(1f)
                .fillMaxWidth()
                .pointerInput(allBots.size, safeIdx) {
                    if (allBots.isEmpty()) return@pointerInput
                    detectDragGestures(
                        onDragEnd = {
                            val dx = dragX.value
                            val dy = dragY.value
                            homeScope.launch {
                                if (abs(dx) > abs(dy) && abs(dx) > 72.dp.toPx()) {
                                    if (dx < 0 && safeIdx < allBots.lastIndex) {
                                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                        curIdx = safeIdx + 1
                                        dragX.snapTo(120f)
                                    } else if (dx > 0 && safeIdx > 0) {
                                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                        curIdx = safeIdx - 1
                                        dragX.snapTo(-120f)
                                    }
                                } else if (dy < -68.dp.toPx() && abs(dy) > abs(dx)) {
                                    haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                    val openTarget = activeBot
                                    if (openTarget != null) model.open(openTarget.id)
                                }
                                launch { dragX.animateTo(0f, iosFluidSpring()) }
                                launch { dragY.animateTo(0f, iosFluidSpring()) }
                            }
                        },
                        onDragCancel = {
                            homeScope.launch {
                                launch { dragX.animateTo(0f, iosFluidSpring()) }
                                launch { dragY.animateTo(0f, iosFluidSpring()) }
                            }
                        },
                        onDrag = { change, dragAmount ->
                            change.consume()
                            homeScope.launch {
                                val maxX = size.width * 0.48f
                                val nextX = (dragX.value + dragAmount.x).coerceIn(-maxX, maxX)
                                val nextY = (dragY.value + dragAmount.y).coerceIn(-220f, 90f)
                                dragX.snapTo(nextX)
                                dragY.snapTo(nextY)
                            }
                        }
                    )
                },
            contentAlignment = Alignment.Center
        ) {
            val stageW = constraints.maxWidth.toFloat().coerceAtLeast(1f)
            val normDx = (dragX.value / (stageW * 0.45f)).coerceIn(-1f, 1f)
            val normUp = (-dragY.value / 240f).coerceIn(0f, 1f)

            if (activeBot != null) {
                val latestMsg = remember(s.messages, activeBot.id) {
                    s.messages.lastOrNull { it.botId == activeBot.id && it.kind != "compact_summary" }
                }
                val isBusy = s.streamBotId == activeBot.id || s.workingBot == activeBot.id || s.busyBot == activeBot.id
                val unreadCount = remember(s.messages, s.unread, activeBot.id) {
                    s.messages.count { it.botId == activeBot.id && it.kind != "compact_summary" && it.id in s.unread }
                }

                if (prevBot != null) {
                    Box(
                        Modifier.align(Alignment.CenterStart)
                            .graphicsLayer {
                                val p = normDx.coerceAtLeast(0f)
                                translationX = -stageW * 0.28f + dragX.value * 0.65f
                                val sc = 0.56f + 0.34f * p
                                scaleX = sc
                                scaleY = sc
                                alpha = (0.28f + 0.58f * p).coerceIn(0f, 1f)
                            }
                            .iosPressScale(0.94f) {
                                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                curIdx = safeIdx - 1
                            }
                    ) {
                        PulseAvatar(prevBot, s, diameter = 128.dp, showStatusDot = false, ringColor = c.bg)
                    }
                }
                if (nextBot != null) {
                    Box(
                        Modifier.align(Alignment.CenterEnd)
                            .graphicsLayer {
                                val p = (-normDx).coerceAtLeast(0f)
                                translationX = stageW * 0.28f + dragX.value * 0.65f
                                val sc = 0.56f + 0.34f * p
                                scaleX = sc
                                scaleY = sc
                                alpha = (0.28f + 0.58f * p).coerceIn(0f, 1f)
                            }
                            .iosPressScale(0.94f) {
                                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                curIdx = safeIdx + 1
                            }
                    ) {
                        PulseAvatar(nextBot, s, diameter = 128.dp, showStatusDot = false, ringColor = c.bg)
                    }
                }

                Column(
                    Modifier.fillMaxWidth()
                        .graphicsLayer {
                            val heroScale = (1f - abs(normDx) * 0.16f - normUp * 0.18f).coerceIn(0.72f, 1.04f)
                            scaleX = heroScale
                            scaleY = heroScale
                            translationX = dragX.value * 0.52f
                            translationY = dragY.value * 0.55f
                            rotationY = normDx * 14f
                            cameraDistance = 14f * density
                            alpha = (1f - abs(normDx) * 0.35f).coerceIn(0.55f, 1f)
                        },
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    Box(
                        Modifier.iosPressScale(0.92f) {
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            focus.clearFocus()
                            model.open(activeBot.id)
                        },
                        contentAlignment = Alignment.Center
                    ) {
                        PulseAvatar(
                            bot = activeBot,
                            state = s,
                            diameter = 172.dp,
                            showStatusDot = false,
                            ringColor = c.bg,
                            gazeShiftX = normDx * 7.5f,
                            enableLiveBlink = true
                        )
                        if (unreadCount > 0) {
                            Box(
                                Modifier.align(Alignment.TopEnd)
                                    .padding(top = 6.dp, end = 6.dp)
                                    .defaultMinSize(minWidth = 24.dp, minHeight = 24.dp)
                                    .clip(CircleShape)
                                    .background(c.accent)
                                    .padding(horizontal = 7.dp, vertical = 2.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text(
                                    unreadCount.toString(),
                                    color = c.userInk,
                                    fontSize = 12.sp,
                                    fontWeight = FontWeight.Bold,
                                    style = TextStyle(fontFeatureSettings = "tnum")
                                )
                            }
                        }
                    }

                    Spacer(Modifier.height(22.dp))

                    Text(
                        activeBot.name,
                        color = c.ink,
                        fontSize = 26.sp,
                        fontWeight = FontWeight.Bold,
                        letterSpacing = (-0.5).sp
                    )

                    Spacer(Modifier.height(8.dp))

                    val subtitleLine = when {
                        isBusy -> s.streamTool.takeIf { it.isNotBlank() }?.let { "正在${humanizeToolName(it, null)}…" }
                            ?: "正在思考…"
                        latestMsg != null -> latestMsg.text.replace('\n', ' ').trim().take(68)
                        else -> activeBot.description.ifBlank { "轻触头像或直接发消息开始对话" }
                    }
                    Text(
                        subtitleLine,
                        Modifier.widthIn(max = 290.dp)
                            .clip(RoundedCornerShape(14.dp))
                            .iosPressScale(0.97f) { model.open(activeBot.id) }
                            .padding(horizontal = 12.dp, vertical = 4.dp),
                        color = if (isBusy) c.accent else c.muted,
                        fontSize = 14.sp,
                        lineHeight = 20.sp,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                        textAlign = TextAlign.Center
                    )

                    if (allBots.size > 1) {
                        Spacer(Modifier.height(20.dp))
                        Row(
                            horizontalArrangement = Arrangement.spacedBy(7.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            allBots.forEachIndexed { idx, _ ->
                                val isCur = idx == safeIdx
                                val dotW by animateDpAsState(
                                    targetValue = if (isCur) 20.dp else 6.dp,
                                    animationSpec = iosInteractiveSpring(reduceMotion),
                                    label = "home_dot_w"
                                )
                                Box(
                                    Modifier.height(6.dp)
                                        .width(dotW)
                                        .clip(CircleShape)
                                        .background(if (isCur) c.accent else c.faint.copy(alpha = 0.45f))
                                        .clickable(
                                            interactionSource = remember { MutableInteractionSource() },
                                            indication = null
                                        ) {
                                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                            curIdx = idx
                                        }
                                )
                            }
                        }
                    }
                }
            }
        }

        // 3. Shared Floating Oval Quick Composer
        if (activeBot != null) {
            val canQuickSend = quickInput.isNotBlank() && !s.uploading
            Row(
                Modifier.fillMaxWidth()
                    .padding(bottom = 14.dp)
                    .clip(RoundedCornerShape(28.dp))
                    .background(c.surface)
                    .border(0.6.dp, c.line.copy(alpha = 0.72f), RoundedCornerShape(28.dp))
                    .padding(horizontal = 7.dp, vertical = 5.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Box(
                    Modifier.size(38.dp)
                        .clip(CircleShape)
                        .background(c.fill.copy(alpha = 0.8f))
                        .iosPressScale(0.90f) {
                            focus.clearFocus()
                            model.open(activeBot.id)
                        },
                    contentAlignment = Alignment.Center
                ) {
                    PulseIcon("plus", c.ink.copy(alpha = 0.78f), Modifier.size(18.dp))
                }
                BasicTextField(
                    value = quickInput,
                    onValueChange = { if (it.length <= 4000) quickInput = it },
                    modifier = Modifier.weight(1f).padding(vertical = 8.dp, horizontal = 2.dp),
                    textStyle = TextStyle(color = c.ink, fontSize = 15.5.sp, lineHeight = 20.sp),
                    cursorBrush = SolidColor(c.accent),
                    singleLine = true,
                    decorationBox = { inner ->
                        Box(contentAlignment = Alignment.CenterStart) {
                            if (quickInput.isEmpty()) {
                                Text("给 ${activeBot.name} 发消息…", color = c.faint, fontSize = 15.5.sp, maxLines = 1)
                            }
                            inner()
                        }
                    }
                )
                Box(
                    Modifier.size(38.dp)
                        .clip(CircleShape)
                        .background(if (canQuickSend) c.accent else c.fill)
                        .iosPressScale(0.88f, enabled = canQuickSend) {
                            val toSend = quickInput.trim()
                            quickInput = ""
                            focus.clearFocus()
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            model.sendFromHome(activeBot.id, toSend)
                        },
                    contentAlignment = Alignment.Center
                ) {
                    PulseIcon("send", if (canQuickSend) c.userInk else c.faint, Modifier.size(16.dp))
                }
            }
        }
    }
}
