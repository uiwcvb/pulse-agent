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


@Composable
internal fun NativeChat(model: NativePulseModel, bot: PulseBot, modifier: Modifier, onComposerHeight: (Int) -> Unit) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val focus = LocalFocusManager.current
    val haptics = LocalHapticFeedback.current
    val chatReduceMotion = rememberReducedMotion()
    val otherUnreadCount = remember(s.unread, s.messages, bot.id) {
        s.messages.count { it.botId != bot.id && it.kind != "compact_summary" && it.id in s.unread }
    }
    var chatScrolled by remember(bot.id) { mutableStateOf(false) }
    val phoneSt by RootPhoneHarness.changes.collectAsState()

    Column(modifier.background(c.chatBg)) {
        // 1. Telegram iOS Navigation Bar (Swipe Left/Right to switch Bot, Swipe Down or Tap Avatar to open Contact Info)
    Surface(
            color = c.surface.copy(alpha = 0.96f),
            tonalElevation = 0.dp,
            modifier = Modifier.pointerInput(bot.id) {
                val horizThresh = 38.dp.toPx()
                val vertThresh = 32.dp.toPx()
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
                        } else if (dy > vertThresh && abs(dy) > abs(dx)) {
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            focus.clearFocus()
                            model.openBotEditor(bot.id)
                        }
                    }
                }
            }
        ) {
            Column {
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 6.dp, vertical = 6.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    // Left: Telegram iOS Back Button with Unread Counter Pill
                    Row(
                        Modifier.heightIn(min = 40.dp)
                            .clip(RoundedCornerShape(12.dp))
                            .iosPressScale(0.95f) {
                                focus.clearFocus()
                                model.back()
                            }
                            .semantics { contentDescription = "返回助手列表" }
                            .padding(horizontal = 6.dp, vertical = 7.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        PulseIcon("back", c.accent, Modifier.size(22.dp))
                        if (otherUnreadCount > 0) {
                            Box(
                                Modifier.padding(start = 2.dp)
                                    .defaultMinSize(minWidth = 19.dp, minHeight = 19.dp)
                                    .clip(CircleShape)
                                    .background(c.accent)
                                    .padding(horizontal = 5.dp, vertical = 1.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text(
                                    otherUnreadCount.toString(),
                                    color = Color.White,
                                    fontSize = 11.sp,
                                    fontWeight = FontWeight.Bold,
                                    style = TextStyle(fontFeatureSettings = "tnum")
                                )
                            }
                        } else {
                            Text(
                                "对话",
                                color = c.accent,
                                fontSize = 16.5.sp,
                                fontWeight = FontWeight.Normal,
                                modifier = Modifier.padding(start = 1.dp)
                            )
                        }
                    }

                    // Center: Compact Apple-HIG Bot Avatar + Name + Minimal Live Status (Telemetry moved to Bot Secondary Page)
                    val isLiveHere = s.streamBotId == bot.id || s.workingBot == bot.id || s.busyBot == bot.id
                    Row(
                        Modifier.padding(start = 2.dp)
                            .weight(1f)
                            .clip(RoundedCornerShape(14.dp))
                            .iosPressScale(0.97f) {
                                focus.clearFocus()
                                model.openBotEditor(bot.id)
                            }
                            .padding(horizontal = 6.dp, vertical = 3.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        PulseAvatar(bot, s, diameter = 34.dp, showStatusDot = true, ringColor = c.surface)
                        Column(Modifier.padding(start = 9.dp).weight(1f)) {
                            Text(
                                bot.name,
                                color = c.ink,
                                fontSize = 16.sp,
                                fontWeight = FontWeight.SemiBold,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                                letterSpacing = (-0.2).sp
                            )
                            val liveHint = when {
                                isLiveHere -> s.streamTool.takeIf { it.isNotBlank() }?.let { "正在${humanizeToolName(it, null)}…" }
                                    ?: "正在思考…"
                                !s.online -> "离线"
                                else -> ""
                            }
                            if (liveHint.isNotEmpty()) {
                                Text(
                                    liveHint,
                                    color = if (isLiveHere) c.accent else c.muted,
                                    fontSize = 11.5.sp,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis
                                )
                            }
                        }
                    }

                    // Right: Active Subtask Pill (only when this bot has running subtasks) + Connection Health Dot
                    val botRunningSubtasks = remember(s.subtasks, bot.id) {
                        s.subtasks.filter { it.parentBotId == bot.id && it.status == "running" }
                    }
                    if (botRunningSubtasks.isNotEmpty()) {
                        val leadSub = botRunningSubtasks.first()
                        Row(
                            Modifier.heightIn(min = 30.dp)
                                .clip(RoundedCornerShape(14.dp))
                                .background(c.accent.copy(alpha = 0.14f))
                                .border(0.5.dp, c.accent.copy(alpha = 0.45f), RoundedCornerShape(14.dp))
                                .iosPressScale(0.94f) {
                                    focus.clearFocus()
                                    model.openSubtaskPanel(leadSub.taskId)
                                }
                                .padding(horizontal = 9.dp, vertical = 4.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(5.dp)
                        ) {
                            Box(Modifier.size(6.dp).clip(CircleShape).background(c.accent))
                            Text(
                                if (botRunningSubtasks.size == 1) leadSub.title.take(8) else "子任务 ${botRunningSubtasks.size}",
                                color = c.accent,
                                fontSize = 11.5.sp,
                                fontWeight = FontWeight.SemiBold,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis
                            )
                        }
                    }
                    val dotColor = when {
                        !s.online -> c.muted
                        else -> Color(0xFF34C759)
                    }
                    Box(
                        Modifier.size(40.dp)
                            .iosPressScale(0.92f) {
                                focus.clearFocus()
                                model.openHealthDialog()
                            }
                            .semantics { contentDescription = "连接与小窗实时自检" },
                        contentAlignment = Alignment.Center
                    ) {
                        Box(
                            Modifier.clip(CircleShape)
                                .background(c.bg.copy(alpha = 0.65f))
                                .border(0.5.dp, c.line.copy(alpha = 0.7f), CircleShape)
                                .padding(7.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Box(Modifier.size(7.dp).clip(CircleShape).background(dotColor))
                        }
                    }
                    PulseRoundButton("more", "选项", { focus.clearFocus(); model.botMenu(bot.id) }, background = false)
                }

                val hdrDivAlpha by animateFloatAsState(
                    targetValue = if (chatScrolled) 0.55f else 0f,
                    animationSpec = if (chatReduceMotion) snap() else tween(160),
                    label = "chat_hdr_div"
                )
                HorizontalDivider(color = c.line.copy(alpha = hdrDivAlpha), thickness = 0.5.dp)

                // Pinned memories moved into Bot Secondary Page (`NativeBotEditorPanel`) to keep chat pristine.
                AnimatedVisibility(
                    visible = false,
                    enter = fadeIn(tween(110)),
                    exit = fadeOut(tween(90))
                ) {
                    var pinsExpanded by remember { mutableStateOf(false) }
                    val latestPin = s.pinnedMemories.lastOrNull()?.optString("text").orEmpty()
                    Column(
                        Modifier.fillMaxWidth()
                            .background(c.surface.copy(alpha = 0.96f))
                            .clickable {
                                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                pinsExpanded = !pinsExpanded
                            }
                    ) {
                        Row(
                            Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 7.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Box(
                                Modifier.width(3.dp).height(28.dp)
                                    .clip(RoundedCornerShape(2.dp))
                                    .background(c.accent)
                            )
                            Column(Modifier.padding(start = 10.dp).weight(1f)) {
                                Text(
                                    "置顶记忆 #${s.pinnedMemories.size}",
                                    color = c.accent,
                                    fontSize = 11.5.sp,
                                    fontWeight = FontWeight.SemiBold,
                                    style = TextStyle(fontFeatureSettings = "tnum")
                                )
                                Text(
                                    latestPin,
                                    color = c.ink,
                                    fontSize = 13.sp,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis
                                )
                            }
                            val chevronRot by animateFloatAsState(
                                targetValue = if (pinsExpanded) 180f else 0f,
                                animationSpec = if (chatReduceMotion) snap() else iosGentleSpring(),
                                label = "pin_chevron_rot"
                            )
                            PulseIcon(
                                "chevron_down",
                                c.faint,
                                Modifier.padding(start = 8.dp)
                                    .size(16.dp)
                                    .graphicsLayer { rotationZ = chevronRot }
                            )
                        }
                        AnimatedVisibility(
                            visible = pinsExpanded,
                            enter = if (chatReduceMotion) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(150)),
                            exit = if (chatReduceMotion) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(110))
                        ) {
                            Column(
                                Modifier.fillMaxWidth().padding(start = 16.dp, end = 14.dp, bottom = 10.dp),
                                verticalArrangement = Arrangement.spacedBy(6.dp)
                            ) {
                                s.pinnedMemories.forEach { p ->
                                    val pid = p.optString("id")
                                    val ptext = p.optString("text")
                                    Row(
                                        Modifier.fillMaxWidth()
                                            .clip(RoundedCornerShape(10.dp))
                                            .background(c.fill.copy(alpha = 0.72f))
                                            .padding(start = 10.dp, end = 4.dp, top = 4.dp, bottom = 4.dp),
                                        verticalAlignment = Alignment.CenterVertically
                                    ) {
                                        Text(
                                            ptext,
                                            color = c.ink,
                                            fontSize = 12.5.sp,
                                            lineHeight = 17.sp,
                                            modifier = Modifier.weight(1f)
                                        )
                                        Box(
                                            Modifier.padding(start = 4.dp)
                                                .heightIn(min = 40.dp)
                                                .clip(RoundedCornerShape(8.dp))
                                                .iosPressScale(0.95f) { model.removeMemoryPin(pid) }
                                                .padding(horizontal = 8.dp, vertical = 6.dp),
                                            contentAlignment = Alignment.Center
                                        ) {
                                            Text(
                                                "移除",
                                                Modifier.clip(RoundedCornerShape(6.dp))
                                                    .background(c.surface)
                                                    .padding(horizontal = 8.dp, vertical = 3.dp),
                                                color = c.warn,
                                                fontSize = 11.sp,
                                                fontWeight = FontWeight.Medium
                                            )
                                        }
                                    }
                                }
                            }
                        }
                        HorizontalDivider(color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
                    }
                }
                AppleLiveActivityPill(
                    visible = phoneSt.displayCreated,
                    displayId = phoneSt.displayId,
                    onExpand = { model.launchFloatingVirtualDisplay() },
                    onDestroy = { model.destroyVirtualDisplay() }
                )
            }
        }

        // 3. Directional AnimatedContent for Bot Switching
        AnimatedContent(
            targetState = bot,
            modifier = Modifier.weight(1f).fillMaxWidth(),
            transitionSpec = {
                if (chatReduceMotion) {
                    fadeIn(tween(110)) togetherWith fadeOut(tween(90))
                } else {
                    val dir = s.lastSwitchDir
                    (slideInHorizontally(spring(dampingRatio = 0.84f, stiffness = 400f)) { fullWidth -> dir * (fullWidth / 3) } +
                        fadeIn(tween(180))) togetherWith
                        (slideOutHorizontally(spring(dampingRatio = 0.86f, stiffness = 420f)) { fullWidth -> -dir * (fullWidth / 3) } +
                            fadeOut(tween(110)))
                }
            },
            contentKey = { it.id },
            label = "bot_room_switch"
        ) { activeBot ->
            NativeChatRoomViewport(model, activeBot) { chatScrolled = it }
        }

        // 4. Telegram iOS Bottom Composer
        NativeComposer(model, bot, onComposerHeight)
    }
}

