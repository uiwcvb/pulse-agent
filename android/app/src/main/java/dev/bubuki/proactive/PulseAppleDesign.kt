package dev.bubuki.proactive

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.*
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * [Module] PulseAppleDesign
 * [Responsibility]
 * Apple HIG & Fluid Interfaces design system components and spring motion specs:
 * - Motion presets (`iosInteractiveSpring`, `iosFluidSpring`, `iosGentleSpring`, `iosPlacementSpring`)
 * - Uncrowded top navigation header (`AppleHomeHeader`) with max 3 trailing controls and 40dp+ hit targets
 * - Authentic iOS search bar (`AppleSearchField`) with animated Cancel reveal
 * - Control-Center style quick hub (`AppleWorkspaceHub`) with concentric radii and Day/Night toggle
 * - Dynamic-Island style Live Activity capsule (`AppleLiveActivityPill`) for Virtual Display
 */

internal fun <T> iosInteractiveSpring(reduceMotion: Boolean = false): FiniteAnimationSpec<T> =
    if (reduceMotion) snap() else spring(dampingRatio = 0.76f, stiffness = 520f)

internal fun <T> iosFluidSpring(reduceMotion: Boolean = false): FiniteAnimationSpec<T> =
    if (reduceMotion) snap() else spring(dampingRatio = 0.84f, stiffness = 390f)

internal fun <T> iosGentleSpring(reduceMotion: Boolean = false): FiniteAnimationSpec<T> =
    if (reduceMotion) snap() else spring(dampingRatio = 0.86f, stiffness = 340f)

internal fun iosPlacementSpring(reduceMotion: Boolean = false): FiniteAnimationSpec<IntOffset> =
    if (reduceMotion) snap() else spring(dampingRatio = 0.82f, stiffness = 420f)

@Composable
internal fun AppleNavIconButton(
    icon: String,
    label: String,
    active: Boolean = false,
    onClick: () -> Unit
) {
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    val reduceMotion = LocalReduceMotion.current
    val bgCol by animateColorAsState(
        targetValue = if (active) c.accent.copy(alpha = 0.16f) else c.fill.copy(alpha = 0.76f),
        animationSpec = tween(150),
        label = "apple_nav_bg"
    )
    val iconTint by animateColorAsState(
        targetValue = if (active) c.accent else c.ink,
        animationSpec = tween(150),
        label = "apple_nav_tint"
    )

    Box(
        Modifier
            .size(40.dp)
            .iosPressScale(0.90f) {
                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                onClick()
            }
            .semantics { contentDescription = label },
        contentAlignment = Alignment.Center
    ) {
        Box(
            Modifier
                .size(34.dp)
                .clip(CircleShape)
                .background(bgCol),
            contentAlignment = Alignment.Center
        ) {
            AnimatedContent(
                targetState = icon,
                transitionSpec = {
                    if (reduceMotion) {
                        fadeIn(tween(100)) togetherWith fadeOut(tween(80))
                    } else {
                        (scaleIn(initialScale = 0.84f, animationSpec = spring(0.75f, 520f)) + fadeIn(tween(120))) togetherWith
                            (scaleOut(targetScale = 0.84f) + fadeOut(tween(90)))
                    }
                },
                label = "apple_nav_icon"
            ) { currentIcon ->
                PulseIcon(currentIcon, iconTint, Modifier.size(17.5.dp))
            }
        }
    }
}

@Composable
internal fun AppleHomeHeader(
    model: NativePulseModel,
    drawerOpen: Boolean,
    onToggleDrawer: () -> Unit
) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    val isBusy = s.workingBot != null || s.streamBotId != null
    val statusColor by animateColorAsState(
        targetValue = when {
            !s.online -> c.warn
            isBusy -> c.accent
            else -> c.green
        },
        animationSpec = tween(200),
        label = "header_status_color"
    )

    Row(
        Modifier
            .fillMaxWidth()
            .padding(start = 16.dp, end = 10.dp, top = 8.dp, bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Row(
            Modifier
                .clip(RoundedCornerShape(10.dp))
                .iosPressScale(0.96f) {
                    haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                    onToggleDrawer()
                }
                .padding(end = 6.dp, top = 4.dp, bottom = 4.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                "DSB",
                color = c.ink,
                fontSize = 23.sp,
                fontWeight = FontWeight.Bold,
                letterSpacing = (-0.5).sp,
                maxLines = 1
            )
            Box(
                Modifier
                    .padding(start = 7.dp)
                    .size(7.dp)
                    .clip(CircleShape)
                    .background(statusColor)
            )
            if (!s.online) {
                Text(
                    "离线",
                    Modifier
                        .padding(start = 6.dp)
                        .clip(CircleShape)
                        .background(c.warn.copy(alpha = 0.14f))
                        .padding(horizontal = 6.dp, vertical = 1.dp),
                    color = c.warn,
                    fontSize = 10.5.sp,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 1
                )
            }
        }

        Spacer(Modifier.weight(1f))

        Row(
            horizontalArrangement = Arrangement.spacedBy(2.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            AppleNavIconButton(
                icon = if (drawerOpen) "close" else "tools",
                label = "工作台",
                active = drawerOpen,
                onClick = onToggleDrawer
            )
            AppleNavIconButton(
                icon = "settings",
                label = "设置中心",
                onClick = { model.openSettings() }
            )
        }
    }
}

@Composable
internal fun AppleSearchField(
    query: String,
    onQueryChange: (String?) -> Unit,
    modifier: Modifier = Modifier
) {
    val c = LocalPulsePalette.current
    val focus = LocalFocusManager.current
    val reduceMotion = LocalReduceMotion.current
    var isFocused by remember { mutableStateOf(false) }

    Row(
        modifier = modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Row(
            Modifier
                .weight(1f)
                .height(36.dp)
                .clip(RoundedCornerShape(10.dp))
                .background(c.fill.copy(alpha = 0.78f))
                .padding(horizontal = 10.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            PulseIcon("search", c.faint, Modifier.size(15.dp))
            BasicTextField(
                value = query,
                onValueChange = { onQueryChange(it.ifEmpty { null }) },
                modifier = Modifier
                    .weight(1f)
                    .padding(horizontal = 7.dp)
                    .onFocusChanged { isFocused = it.isFocused }
                    .semantics { contentDescription = "搜索助手或对话" },
                textStyle = TextStyle(color = c.ink, fontSize = 15.sp),
                singleLine = true,
                cursorBrush = SolidColor(c.accent),
                decorationBox = { inner ->
                    Box(contentAlignment = Alignment.CenterStart) {
                        if (query.isBlank()) {
                            Text("搜索", color = c.faint, fontSize = 15.sp, maxLines = 1)
                        }
                        inner()
                    }
                }
            )
            if (query.isNotEmpty()) {
                Box(
                    Modifier
                        .size(18.dp)
                        .clip(CircleShape)
                        .background(c.faint.copy(alpha = 0.36f))
                        .clickable { onQueryChange(null) },
                    contentAlignment = Alignment.Center
                ) {
                    PulseIcon("close", c.surface, Modifier.size(10.dp))
                }
            }
        }

        AnimatedVisibility(
            visible = isFocused || query.isNotEmpty(),
            enter = if (reduceMotion) fadeIn(tween(100)) else expandHorizontally(iosInteractiveSpring()) + fadeIn(tween(140)),
            exit = if (reduceMotion) fadeOut(tween(80)) else shrinkHorizontally(iosFluidSpring()) + fadeOut(tween(100))
        ) {
            Text(
                "取消",
                color = c.accent,
                fontSize = 15.sp,
                fontWeight = FontWeight.Medium,
                maxLines = 1,
                modifier = Modifier
                    .padding(start = 8.dp)
                    .clip(RoundedCornerShape(8.dp))
                    .clickable {
                        focus.clearFocus()
                        onQueryChange(null)
                    }
                    .padding(horizontal = 4.dp, vertical = 6.dp)
            )
        }
    }
}

@Composable
internal fun AppleWorkspaceHub(
    visible: Boolean,
    model: NativePulseModel,
    phoneState: RootPhoneState,
    onDismiss: () -> Unit
) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val reduceMotion = LocalReduceMotion.current
    val haptics = LocalHapticFeedback.current

    AnimatedVisibility(
        visible = visible,
        enter = if (reduceMotion) {
            fadeIn(tween(120))
        } else {
            expandVertically(spring(dampingRatio = 0.82f, stiffness = 400f)) +
                fadeIn(tween(160)) +
                scaleIn(initialScale = 0.96f, animationSpec = spring(dampingRatio = 0.82f, stiffness = 420f))
        },
        exit = if (reduceMotion) {
            fadeOut(tween(100))
        } else {
            shrinkVertically(spring(dampingRatio = 0.86f, stiffness = 440f)) + fadeOut(tween(120))
        }
    ) {
        Column(
            Modifier
                .fillMaxWidth()
                .padding(top = 10.dp)
                .clip(RoundedCornerShape(18.dp))
                .background(c.fill.copy(alpha = 0.56f))
                .border(0.5.dp, c.line.copy(alpha = 0.55f), RoundedCornerShape(18.dp))
                .padding(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                WorkspaceQuickTile(
                    icon = "screen",
                    title = "手机虚拟屏",
                    subtitle = if (phoneState.displayCreated) "运行中 #${phoneState.displayId}" else "独立小窗",
                    tint = c.green,
                    modifier = Modifier.weight(1f)
                ) { onDismiss(); model.launchFloatingVirtualDisplay() }

                WorkspaceQuickTile(
                    icon = "cloud",
                    title = "云端电脑",
                    subtitle = "桌面与终端",
                    tint = c.accent,
                    modifier = Modifier.weight(1f)
                ) { onDismiss(); model.openCloudDesktop() }
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                WorkspaceQuickTile(
                    icon = "vault",
                    title = "透明记忆库",
                    subtitle = "知识与笔记",
                    tint = c.accent,
                    modifier = Modifier.weight(1f)
                ) { onDismiss(); model.openVault("INDEX.md") }

                WorkspaceQuickTile(
                    icon = "plus",
                    title = "新建助理",
                    subtitle = "定制性格与引擎",
                    tint = c.amber,
                    modifier = Modifier.weight(1f)
                ) { onDismiss(); model.openBotEditor(null) }
            }

            // Quiet bottom status & segmented appearance switch (replaces top-bar clutter)
            Row(
                Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(c.surface.copy(alpha = 0.88f))
                    .padding(horizontal = 12.dp, vertical = 7.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Box(
                    Modifier
                        .size(6.5.dp)
                        .clip(CircleShape)
                        .background(if (s.online) c.green else c.warn)
                )
                Text(
                    if (s.online) "已连接 · ${s.latency} ms" else "未连接到服务端",
                    Modifier.padding(start = 7.dp).weight(1f),
                    color = c.muted,
                    fontSize = 12.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    style = TextStyle(fontFeatureSettings = "tnum")
                )
                Row(
                    Modifier
                        .clip(RoundedCornerShape(9.dp))
                        .background(c.fill.copy(alpha = 0.85f))
                        .padding(2.dp)
                        .iosPressScale(0.95f) {
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            model.toggleAppearanceQuick()
                        }
                        .semantics { contentDescription = if (s.dark) "切换日间模式" else "切换夜间模式" },
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    listOf(false to "日间", true to "夜间").forEach { (isDarkOpt, title) ->
                        val selected = s.dark == isDarkOpt
                        Box(
                            Modifier
                                .clip(RoundedCornerShape(7.dp))
                                .background(if (selected) c.surface else Color.Transparent)
                                .padding(horizontal = 9.dp, vertical = 3.5.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                title,
                                color = if (selected) c.ink else c.muted,
                                fontSize = 11.5.sp,
                                fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Medium,
                                maxLines = 1
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
internal fun AppleLiveActivityPill(
    visible: Boolean,
    displayId: Int,
    onExpand: () -> Unit,
    onDestroy: () -> Unit,
    modifier: Modifier = Modifier
) {
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    val reduceMotion = LocalReduceMotion.current

    AnimatedVisibility(
        visible = visible,
        enter = if (reduceMotion) fadeIn(tween(120)) else expandVertically(iosFluidSpring()) + fadeIn(tween(160)),
        exit = if (reduceMotion) fadeOut(tween(100)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(120)),
        modifier = modifier
    ) {
        Row(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 14.dp, vertical = 6.dp)
                .clip(RoundedCornerShape(14.dp))
                .background(c.elevated)
                .border(0.5.dp, c.green.copy(alpha = 0.28f), RoundedCornerShape(14.dp))
                .padding(start = 12.dp, end = 6.dp, top = 6.dp, bottom = 6.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Row(
                Modifier
                    .weight(1f)
                    .clip(RoundedCornerShape(10.dp))
                    .iosPressScale(0.97f) {
                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                        onExpand()
                    }
                    .padding(vertical = 3.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Box(
                    Modifier
                        .size(7.dp)
                        .clip(CircleShape)
                        .background(c.green)
                )
                Text(
                    "虚拟屏 #$displayId · 轻点展开小窗",
                    Modifier.weight(1f).padding(start = 8.dp, end = 6.dp),
                    color = c.ink,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Medium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    style = TextStyle(fontFeatureSettings = "tnum")
                )
            }
            Box(
                Modifier
                    .clip(RoundedCornerShape(10.dp))
                    .background(c.warn.copy(alpha = 0.14f))
                    .iosPressScale(0.93f) {
                        haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                        onDestroy()
                    }
                    .padding(horizontal = 10.dp, vertical = 5.dp),
                contentAlignment = Alignment.Center
            ) {
                Text(
                    "结束",
                    color = c.warn,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.SemiBold,
                    maxLines = 1
                )
            }
        }
    }
}
