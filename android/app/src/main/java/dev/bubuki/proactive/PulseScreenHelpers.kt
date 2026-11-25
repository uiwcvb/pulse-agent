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
 * [Module] PulseScreenHelpers
 * [Responsibility]
 * 視覺主題調色板、通用基礎組件與格式化工具領域模塊：封裝 Apple HIG + Telegram 主題配色 (PulsePalette, LocalPulsePalette, pulsePalette)、
 * 按壓縮放動效 (Modifier.iosPressScale)、首頁 Bot 卡片行 (NativeBotRow)、快捷入口磁貼 (WorkspaceQuickTile)、
 * 底部彈出菜單 (NativeMenuContent)、工具名稱人性化映射 (humanizeToolName)、Wiki 雙鏈轉換 (
ativeWikiMarkdown)，
 * 以及通用輸入框與按鈕組件 (PulseInputField, PulseRoundButton, PulseAction, SettingsCategoryRow, TelegramProfileActionTile)。
 *
 * [Public API]
 * - PulsePalette, LocalPulsePalette, pulsePalette(dark: Boolean), Modifier.iosPressScale(...)
 * - NativeBotRow(...), WorkspaceQuickTile(...), NativeMenuContent(...)
 * - humanizeToolName(...), 
ativeWikiMarkdown(...)
 * - PulseInputField(...), PulseRoundButton(...), PulseAction(...), SettingsCategoryRow(...), TelegramProfileActionTile(...)
 */

internal data class PulsePalette(
    val bg: Color,
    val chatBg: Color,
    val surface: Color,
    val elevated: Color,
    val pinnedRowBg: Color,
    val aiBubble: Color,
    val fill: Color,
    val line: Color,
    val ink: Color,
    val muted: Color,
    val faint: Color,
    val accent: Color,
    val green: Color,
    val amber: Color,
    val warn: Color,
    val userBubble: Color,
    val userInk: Color,
    val userMeta: Color,
    val codeBg: Color,
    val codeHeader: Color,
    val isDark: Boolean
)

internal val LocalPulsePalette = compositionLocalOf { pulsePalette(true) }

internal fun pulsePalette(dark: Boolean) = if (dark) PulsePalette(
    bg = Color(0xFF0A0A0C),
    chatBg = Color(0xFF0A0A0C),
    surface = Color(0xFF141418),
    elevated = Color(0xFF1C1C21),
    pinnedRowBg = Color(0xFF141418),
    aiBubble = Color(0xFF1A1A20),
    fill = Color(0xFF23232A),
    line = Color(0x1EFFFFFF),
    ink = Color(0xFFF5F5F7),
    muted = Color(0xFF8E8E99),
    faint = Color(0xFF5D5D66),
    accent = Color(0xFFF4F4F6),
    green = Color(0xFF30D158),
    amber = Color(0xFFFF9F0A),
    warn = Color(0xFFFF453A),
    userBubble = Color(0xFFF4F4F6),
    userInk = Color(0xFF111114),
    userMeta = Color(0x99111114),
    codeBg = Color(0xFF111116),
    codeHeader = Color(0xFF1A1A21),
    isDark = true
) else PulsePalette(
    bg = Color(0xFFF5F5F7),
    chatBg = Color(0xFFF5F5F7),
    surface = Color(0xFFFFFFFF),
    elevated = Color(0xFFEFEFF3),
    pinnedRowBg = Color(0xFFF5F5F7),
    aiBubble = Color(0xFFFFFFFF),
    fill = Color(0xFFEAEAEF),
    line = Color(0x14000000),
    ink = Color(0xFF141417),
    muted = Color(0xFF6C6C75),
    faint = Color(0xFF9A9AA3),
    accent = Color(0xFF18181B),
    green = Color(0xFF34C759),
    amber = Color(0xFFFF9500),
    warn = Color(0xFFFF3B30),
    userBubble = Color(0xFF18181B),
    userInk = Color(0xFFFFFFFF),
    userMeta = Color(0xBBFFFFFF),
    codeBg = Color(0xFFF2F2F6),
    codeHeader = Color(0xFFE8E8EE),
    isDark = false
)

/**
 * Apple HIG Spring Press-Scale Modifier:
 * Smoothly compresses interactive controls to `scaleDown` on touch-down and rebounds with iOS spring physics.
 */
internal fun Modifier.iosPressScale(
    scaleDown: Float = 0.96f,
    enabled: Boolean = true,
    onClick: (() -> Unit)? = null
): Modifier = composed {
    val reduceMotion = LocalReduceMotion.current
    val interactionSource = remember { MutableInteractionSource() }
    val isPressed by interactionSource.collectIsPressedAsState()
    val pressSpec: AnimationSpec<Float> = if (reduceMotion) tween(90) else spring(dampingRatio = 0.74f, stiffness = 560f)
    val scale by animateFloatAsState(
        targetValue = if (isPressed && enabled && !reduceMotion) scaleDown else 1f,
        animationSpec = pressSpec,
        label = "ios_press_scale"
    )
    val pressAlpha by animateFloatAsState(
        targetValue = if (isPressed && enabled) 0.84f else 1f,
        animationSpec = tween(80),
        label = "ios_press_alpha"
    )
    this.graphicsLayer {
        scaleX = scale
        scaleY = scale
        alpha = pressAlpha
    }.then(
        if (onClick != null) {
            Modifier.clickable(
                interactionSource = interactionSource,
                indication = null,
                enabled = enabled,
                onClick = onClick
            )
        } else Modifier
    )
}

@Composable
internal fun NativeMenuContent(menu: PulseMenu, closing: Boolean, close: () -> Unit, select: (PulseMenuItem) -> Unit) {
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    Column(
        Modifier.fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .navigationBarsPadding()
            .imePadding()
            .padding(horizontal = 18.dp)
            .padding(top = 8.dp, bottom = 28.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Box(Modifier.padding(bottom = 12.dp).width(36.dp).height(4.5.dp).clip(CircleShape).background(c.faint.copy(alpha = 0.38f)))
        Row(Modifier.fillMaxWidth().padding(horizontal = 4.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(menu.title, Modifier.weight(1f), color = c.ink, fontSize = 19.sp, fontWeight = FontWeight.SemiBold, letterSpacing = (-0.4).sp)
            PulseRoundButton("close", "关闭面板", close, background = true)
        }
        Column(
            Modifier.fillMaxWidth().padding(top = 8.dp).clip(RoundedCornerShape(16.dp)).background(c.fill.copy(alpha = 0.65f))
        ) {
            menu.items.forEachIndexed { idx, item ->
                if (idx > 0) {
                    HorizontalDivider(Modifier.padding(start = 56.dp), color = c.line.copy(alpha = 0.65f), thickness = 0.5.dp)
                }
                Row(
                    Modifier.fillMaxWidth()
                        .iosPressScale(0.98f, enabled = !closing) {
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            select(item)
                        }
                        .padding(vertical = 14.dp, horizontal = 14.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Box(
                        Modifier.size(32.dp).clip(RoundedCornerShape(8.dp)).background(c.accent.copy(alpha = 0.14f)),
                        contentAlignment = Alignment.Center
                    ) {
                        PulseIcon(item.icon, c.accent, Modifier.size(17.dp))
                    }
                    Column(Modifier.padding(start = 14.dp).weight(1f)) {
                        Text(item.title, fontSize = 16.sp, color = c.ink, fontWeight = FontWeight.Medium)
                        if (item.description.isNotEmpty()) Text(item.description, Modifier.padding(top = 2.dp), fontSize = 13.sp, color = c.muted)
                    }
                    PulseIcon("next", c.faint, Modifier.size(15.dp))
                }
            }
        }
    }
}

@Composable
internal fun WorkspaceQuickTile(
    icon: String,
    title: String,
    subtitle: String,
    tint: Color,
    modifier: Modifier = Modifier,
    onClick: () -> Unit
) {
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    Row(
        modifier
            .clip(RoundedCornerShape(10.dp))
            .background(c.surface)
            .iosPressScale(0.96f) {
                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                onClick()
            }
            .padding(horizontal = 11.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(
            Modifier.size(34.dp).clip(CircleShape).background(tint.copy(alpha = 0.14f)),
            contentAlignment = Alignment.Center
        ) {
            PulseIcon(icon, tint, Modifier.size(18.dp))
        }
        Column(Modifier.padding(start = 10.dp).weight(1f)) {
            Text(title, color = c.ink, fontSize = 13.5.sp, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(subtitle, color = c.muted, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis, style = TextStyle(fontFeatureSettings = "tnum"))
        }
    }
}

private val RE_BOT_ROW_WS = Regex("\\s+")
private val RE_BOT_ROW_QUOTE = Regex("^> [^\\n]+\\n\\n")
private val RE_BOT_ROW_ATT = Regex("\n\n附件（工作区相对路径[\\s\\S]*")

/**
 * True Telegram iOS Contact Row with Physical Finger-Tracking Swipe Reveal:
 * - Swipe Right (`offsetX > 0`): Smoothly tracks finger, reveals Telegram Blue `📌 置顶 / 取消置顶` action on the left,
 *   scales icon with spring pop + haptic tick at threshold (`72.dp`), and springs back on release!
 * - Swipe Left (`offsetX < 0`): Smoothly slides row left to reveal full-height Telegram colored action tiles:
 *   `👤 档案` (Blue), `🧹 压缩` (Orange), `⏹ 停止` (Red), snapping open to `-204.dp` or springing shut!
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
internal fun NativeBotRow(model: NativePulseModel, bot: PulseBot, isSelected: Boolean = false) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    val density = LocalDensity.current
    val rowScope = rememberCoroutineScope()
    val reduceMotion = rememberReducedMotion()

    val latest = s.messages.lastOrNull { it.botId == bot.id && it.kind != "compact_summary" }
    val unread = s.messages.count { it.botId == bot.id && it.kind != "compact_summary" && it.id in s.unread }
    val isStreamingHere = s.streamBotId == bot.id
    val isWorkingHere = isStreamingHere || s.workingBot == bot.id || s.busyBot == bot.id
    val unsentDraft = model.draftOf(bot.id).trim()

    val drawerWidthPx = with(density) { 204.dp.toPx() }
    val pinThresholdPx = with(density) { 74.dp.toPx() }
    val maxRightPullPx = with(density) { 128.dp.toPx() }
    val offsetX = remember(bot.id) { Animatable(0f) }
    var pinThresholdCrossed by remember(bot.id) { mutableStateOf(false) }
    val showPinReveal by remember(bot.id) { derivedStateOf { offsetX.value > 0.5f } }
    val pinThresholdReached by remember(bot.id, pinThresholdPx) { derivedStateOf { offsetX.value >= pinThresholdPx } }
    val showActionsReveal by remember(bot.id) { derivedStateOf { offsetX.value < -0.5f } }

    val rowBgColor = when {
        isSelected -> lerp(c.surface, c.accent, if (c.isDark) 0.16f else 0.09f)
        bot.pinned -> c.pinnedRowBg
        else -> c.surface
    }

    Box(
        Modifier.fillMaxWidth()
            .height(IntrinsicSize.Min)
            .clipToBounds()
            .background(rowBgColor)
    ) {
        // 1. Background Left Reveal (Swipe Right -> Telegram Blue Pin / Unpin)
        if (showPinReveal) {
            val iconScale by animateFloatAsState(
                targetValue = if (pinThresholdReached) 1.16f else 0.90f,
                animationSpec = if (reduceMotion) snap() else spring(dampingRatio = 0.72f, stiffness = 500f),
                label = "pin_scale"
            )
            Box(
                Modifier.fillMaxSize()
                    .background(if (bot.pinned) Color(0xFF636366) else c.accent)
                    .padding(start = 20.dp),
                contentAlignment = Alignment.CenterStart
            ) {
                Row(
                    Modifier.graphicsLayer {
                        val dragFrac = (offsetX.value / pinThresholdPx).coerceIn(0f, 1.25f)
                        val dragScale = if (pinThresholdReached) iconScale else (0.65f + 0.35f * dragFrac.coerceAtMost(1f))
                        scaleX = dragScale
                        scaleY = dragScale
                    },
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    PulseIcon("pin", Color.White, Modifier.size(20.dp))
                    Text(
                        if (bot.pinned) "取消置顶" else "置顶对话",
                        color = Color.White,
                        fontSize = 13.5.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                }
            }
        }

        // 2. Background Right Reveal (Swipe Left -> Full-Height Telegram Colored Action Tiles)
        if (showActionsReveal) {
            Row(
                Modifier.fillMaxHeight()
                    .width(204.dp)
                    .align(Alignment.CenterEnd),
                verticalAlignment = Alignment.CenterVertically
            ) {
                // Tile 1: Profile (Telegram Blue)
                Column(
                    Modifier.weight(1f).fillMaxHeight()
                        .background(c.accent)
                        .clickable {
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            rowScope.launch { if (reduceMotion) offsetX.snapTo(0f) else offsetX.animateTo(0f, spring(dampingRatio = 0.82f, stiffness = 460f)) }
                            model.openBotEditor(bot.id)
                        },
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.Center
                ) {
                    PulseIcon("user", Color.White, Modifier.size(19.dp))
                    Text("档案", color = Color.White, fontSize = 11.5.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(top = 3.dp))
                }
                // Tile 2: Compact Context (Telegram Orange)
                Column(
                    Modifier.weight(1f).fillMaxHeight()
                        .background(c.amber)
                        .clickable {
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            rowScope.launch { if (reduceMotion) offsetX.snapTo(0f) else offsetX.animateTo(0f, spring(dampingRatio = 0.82f, stiffness = 460f)) }
                            model.compactBotContext(bot.id)
                        },
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.Center
                ) {
                    PulseIcon("spark", Color.White, Modifier.size(19.dp))
                    Text("压缩", color = Color.White, fontSize = 11.5.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(top = 3.dp))
                }
                // Tile 3: Stop (Red) or More (Slate)
                Column(
                    Modifier.weight(1f).fillMaxHeight()
                        .background(if (isWorkingHere) c.warn else Color(0xFF636366))
                        .clickable {
                            haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                            rowScope.launch { if (reduceMotion) offsetX.snapTo(0f) else offsetX.animateTo(0f, spring(dampingRatio = 0.82f, stiffness = 460f)) }
                            if (isWorkingHere) model.stopBotById(bot.id) else model.botMenu(bot.id)
                        },
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.Center
                ) {
                    PulseIcon(if (isWorkingHere) "stop" else "more", Color.White, Modifier.size(19.dp))
                    Text(if (isWorkingHere) "停止" else "更多", color = Color.White, fontSize = 11.5.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(top = 3.dp))
                }
            }
        }

        // 3. Foreground Contact Row (Tracks Finger with Rubber-Band Physics)
        Row(
            Modifier.fillMaxWidth()
                .graphicsLayer { translationX = offsetX.value }
                .background(rowBgColor)
                .drawBehind {
                    if (isSelected) {
                        val barW = 3.dp.toPx()
                        val barPad = 12.dp.toPx()
                        drawRoundRect(
                            color = c.accent,
                            topLeft = androidx.compose.ui.geometry.Offset(0f, barPad),
                            size = androidx.compose.ui.geometry.Size(barW, (size.height - barPad * 2f).coerceAtLeast(16.dp.toPx())),
                            cornerRadius = androidx.compose.ui.geometry.CornerRadius(barW, barW)
                        )
                    }
                }
                .pointerInput(bot.id, bot.pinned) {
                    detectHorizontalDragGestures(
                        onDragStart = { pinThresholdCrossed = false },
                        onHorizontalDrag = { change, dragAmount ->
                            change.consume()
                            val cur = offsetX.value
                            val next = if (cur + dragAmount > 0f) {
                                // Rubber-band resistance when swiping right to Pin/Unpin
                                val raw = cur + dragAmount * 0.72f
                                raw.coerceAtMost(maxRightPullPx)
                            } else {
                                // Smooth slide left up to drawer width with light overscroll
                                val factor = if (cur < -drawerWidthPx) 0.25f else 0.92f
                                (cur + dragAmount * factor).coerceAtLeast(-drawerWidthPx * 1.18f)
                            }
                            if (!pinThresholdCrossed && next >= pinThresholdPx) {
                                pinThresholdCrossed = true
                                haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                            } else if (pinThresholdCrossed && next < pinThresholdPx * 0.85f) {
                                pinThresholdCrossed = false
                            }
                            rowScope.launch { offsetX.snapTo(next) }
                        },
                        onDragEnd = {
                            val finalX = offsetX.value
                            rowScope.launch {
                                when {
                                    finalX >= pinThresholdPx -> {
                                        if (reduceMotion) offsetX.snapTo(0f) else offsetX.animateTo(0f, spring(dampingRatio = 0.76f, stiffness = 420f))
                                        model.togglePinBot(bot.id)
                                    }
                                    finalX < -drawerWidthPx * 0.34f -> {
                                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                        if (reduceMotion) offsetX.snapTo(-drawerWidthPx) else offsetX.animateTo(-drawerWidthPx, spring(dampingRatio = 0.80f, stiffness = 430f))
                                    }
                                    else -> {
                                        if (reduceMotion) offsetX.snapTo(0f) else offsetX.animateTo(0f, spring(dampingRatio = 0.80f, stiffness = 460f))
                                    }
                                }
                            }
                        },
                        onDragCancel = {
                            rowScope.launch { if (reduceMotion) offsetX.snapTo(0f) else offsetX.animateTo(0f, spring(dampingRatio = 0.82f, stiffness = 450f)) }
                        }
                    )
                }
                .combinedClickable(
                    onClick = {
                        if (abs(offsetX.value) > 8f) {
                            rowScope.launch { if (reduceMotion) offsetX.snapTo(0f) else offsetX.animateTo(0f, spring(dampingRatio = 0.82f, stiffness = 460f)) }
                        } else {
                            model.open(bot.id)
                        }
                    },
                    onLongClick = {
                        haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                        model.botMenu(bot.id)
                    },
                    onLongClickLabel = "对话选项"
                )
                .semantics { contentDescription = "打开${bot.name}的对话" }
                .padding(horizontal = 14.dp, vertical = 11.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            PulseAvatar(bot, s, diameter = 54.dp, showStatusDot = true, ringColor = rowBgColor)

            Column(Modifier.weight(1f).padding(start = 14.dp)) {
                // Top Line: Bot Name + Badges + Outgoing Checkmarks + Timestamp
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        bot.name,
                        color = c.ink,
                        fontWeight = FontWeight.SemiBold,
                        fontSize = 16.5.sp,
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
                    Spacer(Modifier.weight(1f))
                    if (latest != null) {
                        if (latest.role == "user" && latest.kind != "bot_message") {
                            PulseIcon(
                                if (latest.optimistic) "check" else "check_double",
                                c.accent,
                                Modifier.padding(end = 3.dp).size(14.dp)
                            )
                        }
                        Text(
                            model.timeSeparator(latest.created),
                            fontSize = 12.5.sp,
                            color = if (unread > 0) c.accent else c.faint,
                            style = TextStyle(fontFeatureSettings = "tnum")
                        )
                    }
                }

                // Bottom Line: 2-Line Telegram Preview (Draft in Red / Sender Prefix in Ink / Sticker Tag in Blue) + Unread / Pin Badge
                Row(
                    Modifier.padding(top = 3.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    val annotatedPreview = buildAnnotatedString {
                        when {
                            isStreamingHere && s.streamTool.isNotBlank() -> {
                                withStyle(SpanStyle(color = c.accent, fontWeight = FontWeight.Medium)) {
                                    append("正在${humanizeToolName(s.streamTool, null)}…")
                                }
                            }
                            isStreamingHere && s.streamText.isNotBlank() -> {
                                withStyle(SpanStyle(color = c.accent, fontWeight = FontWeight.Medium)) {
                                    append("${s.streamSpeakerName.ifBlank { bot.name }}: ")
                                }
                                withStyle(SpanStyle(color = c.ink)) {
                                    append(stripMarkdownForPreview(s.streamText))
                                }
                            }
                            s.workingBot == bot.id -> {
                                withStyle(SpanStyle(color = c.accent, fontWeight = FontWeight.Medium)) {
                                    append("● ${s.workingText.ifBlank { "正在思考与执行…" }}")
                                }
                            }
                            unsentDraft.isNotEmpty() -> {
                                withStyle(SpanStyle(color = c.warn, fontWeight = FontWeight.SemiBold)) {
                                    append("草稿: ")
                                }
                                withStyle(SpanStyle(color = c.muted)) {
                                    append(unsentDraft.replace(RE_BOT_ROW_WS, " "))
                                }
                            }
                            latest != null -> {
                                when {
                                    latest.fromBotName.isNotBlank() && latest.fromBotId != bot.id -> {
                                        withStyle(SpanStyle(color = c.ink, fontWeight = FontWeight.Medium)) {
                                            append("${latest.fromBotName}: ")
                                        }
                                    }
                                    latest.role == "user" && latest.kind != "bot_message" -> {
                                        withStyle(SpanStyle(color = c.ink, fontWeight = FontWeight.Medium)) {
                                            append("我: ")
                                        }
                                    }
                                }
                                if (latest.sticker.isNotBlank() && latest.text.isBlank()) {
                                    withStyle(SpanStyle(color = c.accent)) {
                                        append(if (latest.sticker.contains("/stickers/")) "[表情贴纸]" else "[图片]")
                                    }
                                } else {
                                    val cleanPreview = stripMarkdownForPreview(
                                        latest.text
                                            .replace(RE_BOT_ROW_QUOTE, "↩ ")
                                            .replace(RE_BOT_ROW_ATT, " [图片/附件]")
                                    )
                                    withStyle(
                                        SpanStyle(
                                            color = when {
                                                latest.kind == "error" -> c.warn
                                                unread > 0 -> c.ink
                                                else -> c.muted
                                            }
                                        )
                                    ) {
                                        append(cleanPreview)
                                    }
                                }
                            }
                            else -> {
                                withStyle(SpanStyle(color = c.muted)) {
                                    append(bot.description)
                                }
                            }
                        }
                    }

                    Text(
                        annotatedPreview,
                        Modifier.weight(1f),
                        fontSize = 14.5.sp,
                        lineHeight = 19.sp,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis
                    )

                    if (unread > 0) {
                        Box(
                            Modifier.padding(start = 8.dp)
                                .defaultMinSize(minWidth = 21.dp, minHeight = 21.dp)
                                .clip(CircleShape)
                                .background(c.accent)
                                .padding(horizontal = 6.dp, vertical = 2.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                unread.toString(),
                                color = Color.White,
                                fontSize = 11.5.sp,
                                fontWeight = FontWeight.Bold,
                                style = TextStyle(fontFeatureSettings = "tnum")
                            )
                        }
                    } else if (bot.pinned) {
                        PulseIcon(
                            "pin",
                            c.faint,
                            Modifier.padding(start = 8.dp).size(15.dp)
                        )
                    }
                }
            }
        }
    }
}

@Composable
internal fun SettingsCategoryRow(
    icon: String,
    title: String,
    subtitle: String,
    value: String,
    onClick: () -> Unit
) {
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    Row(
        Modifier.fillMaxWidth()
            .iosPressScale(0.98f) {
                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                onClick()
            }
            .padding(horizontal = 14.dp, vertical = 13.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Box(
            Modifier.size(32.dp).clip(RoundedCornerShape(8.dp)).background(c.accent.copy(alpha = 0.14f)),
            contentAlignment = Alignment.Center
        ) {
            PulseIcon(icon, c.accent, Modifier.size(17.dp))
        }
        Column(Modifier.padding(start = 12.dp).weight(1f)) {
            Text(title, color = c.ink, fontSize = 15.sp, fontWeight = FontWeight.Medium)
            if (subtitle.isNotBlank()) {
                Text(subtitle, color = c.muted, fontSize = 11.5.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        }
        if (value.isNotBlank()) {
            Text(
                value,
                color = c.muted,
                fontSize = 12.5.sp,
                modifier = Modifier.padding(horizontal = 6.dp),
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                style = TextStyle(fontFeatureSettings = "tnum")
            )
        }
        PulseIcon("next", c.faint, Modifier.size(15.dp))
    }
}

@Composable
internal fun TelegramProfileActionTile(icon: String, label: String, modifier: Modifier = Modifier, onClick: () -> Unit) {
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    Column(
        modifier
            .clip(RoundedCornerShape(12.dp))
            .background(c.fill.copy(alpha = 0.75f))
            .iosPressScale(0.94f) {
                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                onClick()
            }
            .padding(vertical = 10.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(4.dp)
    ) {
        PulseIcon(icon, c.accent, Modifier.size(19.dp))
        Text(label, color = c.accent, fontSize = 12.sp, fontWeight = FontWeight.Medium)
    }
}

@Composable
internal fun PulseInputField(
    label: String,
    value: String,
    placeholder: String,
    singleLine: Boolean = true,
    onChange: (String) -> Unit
) {
    val c = LocalPulsePalette.current
    Column(Modifier.fillMaxWidth()) {
        Text(
            label,
            color = c.muted,
            fontSize = 12.5.sp,
            fontWeight = FontWeight.Medium,
            modifier = Modifier.padding(bottom = 5.dp)
        )
        BasicTextField(
            value = value,
            onValueChange = onChange,
            singleLine = singleLine,
            textStyle = TextStyle(color = c.ink, fontSize = 15.5.sp, lineHeight = 21.sp),
            cursorBrush = SolidColor(c.accent),
            modifier = Modifier.fillMaxWidth()
                .clip(RoundedCornerShape(11.dp))
                .background(c.fill.copy(alpha = 0.78f))
                .padding(horizontal = 13.dp, vertical = 11.dp),
            decorationBox = { inner ->
                Box(Modifier.heightIn(min = if (singleLine) 21.dp else 88.dp)) {
                    if (value.isEmpty()) Text(placeholder, color = c.faint, fontSize = 15.5.sp)
                    inner()
                }
            }
        )
    }
}

@Composable
internal fun PulseRoundButton(icon: String, label: String, click: () -> Unit, background: Boolean = true) {
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    Box(
        Modifier.size(40.dp)
            .iosPressScale(0.90f) {
                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                click()
            }
            .semantics { contentDescription = label },
        contentAlignment = Alignment.Center
    ) {
        Box(
            Modifier.size(34.dp)
                .clip(CircleShape)
                .then(if (background) Modifier.background(c.fill.copy(alpha = 0.76f)) else Modifier),
            contentAlignment = Alignment.Center
        ) {
            PulseIcon(icon, if (background) c.ink else c.accent, Modifier.size(18.dp))
        }
    }
}

