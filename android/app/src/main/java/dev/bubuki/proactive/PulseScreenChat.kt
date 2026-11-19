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
    val otherUnreadCount = remember(s.unread, s.messages, bot.id) {
        s.messages.count { it.botId != bot.id && it.id in s.unread }
    }

    Column(modifier.background(c.chatBg)) {
        // 1. Telegram iOS Navigation Bar (Swipe Left/Right to switch Bot, Swipe Down or Tap Avatar to open Contact Info)
        Surface(
            color = c.surface.copy(alpha = 0.96f),
            tonalElevation = 0.dp,
            modifier = Modifier.pointerInput(bot.id) {
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
                    if (abs(dx) > abs(dy) && abs(dx) > 70f) {
                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                        if (dx < 0) model.switchAdjacentBot(1) else model.switchAdjacentBot(-1)
                    } else if (dy > 60f && abs(dy) > abs(dx)) {
                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                        focus.clearFocus()
                        model.openBotEditor(bot.id)
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

                    // Center: Bot Avatar + Name + 3-Bot Switcher Dots + Live Status / Token Cache Subtitle
                    val effectiveModel = bot.model.ifBlank { s.defaultModel.ifBlank { "默认模型" } }
                    val isLiveHere = s.streamBotId == bot.id || s.workingBot == bot.id || s.busyBot == bot.id
                    Row(
                        Modifier.padding(start = 2.dp)
                            .weight(1f)
                            .clip(RoundedCornerShape(12.dp))
                            .iosPressScale(0.97f) {
                                focus.clearFocus()
                                model.openBotEditor(bot.id)
                            }
                            .padding(horizontal = 8.dp, vertical = 4.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        PulseAvatar(bot, s, diameter = 38.dp, showStatusDot = true, ringColor = c.surface)
                        Column(Modifier.padding(start = 10.dp).weight(1f)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    bot.name,
                                    color = c.ink,
                                    fontSize = 16.5.sp,
                                    fontWeight = FontWeight.SemiBold,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                    letterSpacing = (-0.2).sp
                                )
                                if (s.primary == bot.id) {
                                    Text(
                                        "主控",
                                        Modifier.padding(start = 6.dp)
                                            .clip(RoundedCornerShape(4.dp))
                                            .background(c.accent.copy(alpha = 0.14f))
                                            .padding(horizontal = 5.dp, vertical = 1.dp),
                                        color = c.accent,
                                        fontSize = 10.sp,
                                        fontWeight = FontWeight.SemiBold
                                    )
                                }
                                // 3-Bot Switcher Indicator Dots (Swipe Left/Right hint)
                                if (s.bots.size > 1) {
                                    Row(
                                        Modifier.padding(start = 8.dp),
                                        horizontalArrangement = Arrangement.spacedBy(3.5.dp),
                                        verticalAlignment = Alignment.CenterVertically
                                    ) {
                                        s.bots.forEach { b ->
                                            val active = b.id == bot.id
                                            Box(
                                                Modifier.size(if (active) 5.5.dp else 4.dp)
                                                    .clip(CircleShape)
                                                    .background(if (active) c.accent else c.faint.copy(alpha = 0.45f))
                                            )
                                        }
                                    }
                                }
                            }
                            val (hdrCurTok, _, _) = model.contextStatsFor(bot.id)
                            val (hdrCachedTok, hdrCachePct) = model.contextCacheFor(bot.id)
                            val (hdrTtft, hdrTps, _) = model.contextPerfFor(bot.id)
                            val ttftStr = if (hdrTtft > 0) {
                                if (hdrTtft >= 1000) String.format(java.util.Locale.US, " · 首字%.1fs", hdrTtft / 1000.0)
                                else " · 首字${hdrTtft}ms"
                            } else ""
                            val tpsStr = if (hdrTps > 0.0) String.format(java.util.Locale.US, " · %.0ft/s", hdrTps) else ""
                            val ctxBadge = if (hdrCurTok > 0) {
                                (if (hdrCachedTok > 0) " · ${model.formatTokenCount(hdrCurTok)} ($hdrCachePct%)"
                                else " · ${model.formatTokenCount(hdrCurTok)}") + ttftStr + tpsStr
                            } else ""
                            Text(
                                if (isLiveHere) {
                                    (s.streamTool.takeIf { it.isNotBlank() }?.let { "正在${humanizeToolName(it, null)}…" }
                                        ?: "${s.streamSpeakerName.ifBlank { bot.name }} 正在输入…") + ctxBadge
                                } else {
                                    effectiveModel + ctxBadge
                                },
                                color = if (isLiveHere) c.accent else c.muted,
                                fontSize = 11.5.sp,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                                style = TextStyle(fontFeatureSettings = "tnum")
                            )
                        }
                    }

                    // Right: Live Connection & Virtual Display Health Pill + More Options
                    val overlayMode by FloatingVirtualDisplay.overlayModeFlow.collectAsState()
                    val phoneSt by RootPhoneHarness.changes.collectAsState()
                    val dotColor = when {
                        !s.online -> c.muted
                        else -> Color(0xFF34C759)
                    }
                    Row(
                        Modifier.clip(RoundedCornerShape(12.dp))
                            .background(c.bg.copy(alpha = 0.65f))
                            .border(0.5.dp, c.line.copy(alpha = 0.7f), RoundedCornerShape(12.dp))
                            .iosPressScale(0.95f) {
                                focus.clearFocus()
                                model.openHealthDialog()
                            }
                            .semantics { contentDescription = "连接与小窗实时自检" }
                            .padding(horizontal = 8.dp, vertical = 5.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(5.dp)
                    ) {
                        Box(Modifier.size(7.dp).clip(CircleShape).background(dotColor))
                        if (phoneSt.displayCreated && overlayMode != OverlayMode.HIDDEN) {
                            Text(
                                overlayMode.label,
                                color = c.accent,
                                fontSize = 10.5.sp,
                                fontWeight = FontWeight.Medium,
                                maxLines = 1
                            )
                        }
                    }
                    PulseRoundButton("more", "选项", { focus.clearFocus(); model.botMenu(bot.id) }, background = false)
                }

                HorizontalDivider(color = c.line.copy(alpha = 0.65f), thickness = 0.5.dp)

                // 2. Telegram-Style Pinned Memory Bar (`📌 置顶记忆`)
                AnimatedVisibility(
                    visible = s.pinnedMemories.isNotEmpty(),
                    enter = expandVertically(spring(dampingRatio = 0.82f, stiffness = 400f)) + fadeIn(),
                    exit = shrinkVertically(spring(dampingRatio = 0.85f, stiffness = 450f)) + fadeOut()
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
                                animationSpec = spring(dampingRatio = 0.78f, stiffness = 420f),
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
                            enter = expandVertically(spring(dampingRatio = 0.80f, stiffness = 400f)) + fadeIn(),
                            exit = shrinkVertically(spring(dampingRatio = 0.85f, stiffness = 440f)) + fadeOut()
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
                                            .padding(horizontal = 10.dp, vertical = 7.dp),
                                        verticalAlignment = Alignment.CenterVertically
                                    ) {
                                        Text(
                                            ptext,
                                            color = c.ink,
                                            fontSize = 12.5.sp,
                                            lineHeight = 17.sp,
                                            modifier = Modifier.weight(1f)
                                        )
                                        Text(
                                            "移除",
                                            Modifier.padding(start = 8.dp)
                                                .clip(RoundedCornerShape(6.dp))
                                                .background(c.surface)
                                                .clickable { model.removeMemoryPin(pid) }
                                                .padding(horizontal = 8.dp, vertical = 3.dp),
                                            color = c.warn,
                                            fontSize = 11.sp,
                                            fontWeight = FontWeight.Medium
                                        )
                                    }
                                }
                            }
                        }
                        HorizontalDivider(color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
                    }
                }
                val chatVdState by RootPhoneHarness.changes.collectAsState()
                AppleLiveActivityPill(
                    visible = chatVdState.displayCreated,
                    displayId = chatVdState.displayId,
                    onExpand = { model.launchFloatingVirtualDisplay() },
                    onDestroy = { model.destroyVirtualDisplay() }
                )
            }
        }

        // 3. Directional AnimatedContent for Bot Switching (Glides smoothly left/right when swiping between Bots!)
        AnimatedContent(
            targetState = bot,
            modifier = Modifier.weight(1f).fillMaxWidth(),
            transitionSpec = {
                val dir = s.lastSwitchDir
                (slideInHorizontally(spring(dampingRatio = 0.84f, stiffness = 400f)) { fullWidth -> dir * (fullWidth / 3) } +
                    fadeIn(tween(180))) togetherWith
                    (slideOutHorizontally(spring(dampingRatio = 0.86f, stiffness = 420f)) { fullWidth -> -dir * (fullWidth / 3) } +
                        fadeOut(tween(130)))
            },
            contentKey = { it.id },
            label = "bot_room_switch"
        ) { activeBot ->
            NativeChatRoomViewport(model, activeBot)
        }

        // 4. Telegram iOS Bottom Composer
        NativeComposer(model, bot, onComposerHeight)
    }
}

