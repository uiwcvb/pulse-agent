package dev.bubuki.proactive

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.gestures.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.*
import androidx.compose.foundation.text.*
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.*
import androidx.compose.ui.draw.*
import androidx.compose.ui.graphics.*
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.*
import androidx.compose.ui.platform.*
import androidx.compose.ui.semantics.*
import androidx.compose.ui.text.*
import androidx.compose.ui.text.font.*
import androidx.compose.ui.text.style.*
import androidx.compose.ui.unit.*

/**
 * [Module] PulseScreenCloudPanel
 * [Responsibility]
 * 云端桌面实时画面与真人工接管控制面板 (NativeCloudDesktopPanel)：
 * 绑定 `ui.cloudDesktopUserControl` 双向状态机（`/takeover`、`/handback`、`/stop`）、
 * 实时展示 `active_window` / `url` / 服务配额、支持 Dock 应用一键拉起并立刻刷帧，
 * 以及纯输入不强制回车的远程文本发送与独立快捷键。
 */

@Composable
internal fun NativeCloudDesktopPanel(model: NativePulseModel, modifier: Modifier) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val reduceMotion = rememberReducedMotion()
    val focus = LocalFocusManager.current
    val botName = remember(s.cloudDesktopLiveBotId, s.botId, s.bots) {
        model.bot(s.cloudDesktopLiveBotId ?: s.botId)?.name ?: "助理"
    }
    var urlInput by remember(s.cloudDesktopUrl) { mutableStateOf(s.cloudDesktopUrl) }
    var typeInput by remember { mutableStateOf("") }
    var tapMode by remember { mutableStateOf("click") } // click | double_click | right_click
    var zoomScale by remember { mutableFloatStateOf(1f) }
    var panX by remember { mutableFloatStateOf(0f) }
    var panY by remember { mutableFloatStateOf(0f) }
    val bezelFill = c.elevated
    val bezelStroke = c.line.copy(alpha = 0.7f)
    val dockFill = c.surface
    val dockBorder = c.line.copy(alpha = 0.6f)
    val quotaLabel = s.cloudDesktopQuota.ifBlank { "2 vCPU · 2.5 GB RAM · Cloud OS" }
    val activeWinText = s.cloudDesktopActiveWindow.ifBlank { s.cloudDesktopTitle }
    val statusDetail = listOf(activeWinText, s.cloudDesktopUrl).filter { it.isNotBlank() }.joinToString(" · ").ifBlank { quotaLabel }
    val isAiBusy = s.cloudDesktopTaskRunning || s.workingBot != null || s.streamBotId != null
    val sendTextAction: (Boolean) -> Unit = { submit ->
        val txt = typeInput
        if (txt.isNotEmpty()) {
            typeInput = ""
            focus.clearFocus()
            model.cloudDesktopType(txt, submit = submit)
        }
    }

    Column(modifier.background(c.bg).imePadding(), horizontalAlignment = Alignment.CenterHorizontally) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            Box(
                Modifier.size(40.dp).clip(CircleShape).background(c.surface).border(0.5.dp, c.line, CircleShape)
                    .iosPressScale(0.92f) { focus.clearFocus(); model.openCloudDesktop() }
                    .semantics { contentDescription = "刷新云电脑画面" },
                contentAlignment = Alignment.Center
            ) {
                PulseIcon("refresh", c.muted, Modifier.size(17.dp))
            }
            Row(
                Modifier.weight(1f).heightIn(min = 40.dp).clip(RoundedCornerShape(12.dp)).background(c.surface).border(0.5.dp, c.line, RoundedCornerShape(12.dp))
                    .padding(horizontal = 12.dp, vertical = 7.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.Center
            ) {
                PulseIcon("cloud", c.accent, Modifier.size(15.dp))
                Text(
                    "$botName 的云端电脑", Modifier.padding(start = 7.dp),
                    color = c.ink, fontSize = 13.5.sp, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis
                )
            }
            Row(
                Modifier.heightIn(min = 40.dp).clip(RoundedCornerShape(12.dp)).background(c.warn)
                    .iosPressScale(0.92f) { focus.clearFocus(); model.cloudDesktopStop() }
                    .padding(horizontal = 12.dp, vertical = 7.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(5.dp)
            ) {
                PulseIcon("stop", Color.White, Modifier.size(12.dp))
                Text("停止", color = Color.White, fontSize = 12.5.sp, fontWeight = FontWeight.Bold)
            }
            Box(
                Modifier.size(40.dp).clip(CircleShape).background(c.surface).border(0.5.dp, c.line, CircleShape)
                    .iosPressScale(0.92f) { focus.clearFocus(); model.closePanel() }
                    .semantics { contentDescription = "关闭云电脑面板" },
                contentAlignment = Alignment.Center
            ) {
                PulseIcon("close", c.muted, Modifier.size(16.dp))
            }
        }

        Row(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 2.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            val badgeColor = when {
                s.cloudDesktopUserControl -> c.green
                isAiBusy -> c.amber
                else -> c.accent
            }
            val badgeLabel = when {
                s.cloudDesktopUserControl -> "人工接管中"
                isAiBusy -> "AI 执行中"
                else -> "AI 控制中"
            }
            Row(
                Modifier.clip(RoundedCornerShape(8.dp)).background(badgeColor.copy(alpha = 0.14f)).padding(horizontal = 8.dp, vertical = 3.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(5.dp)
            ) {
                Box(Modifier.size(6.dp).clip(CircleShape).background(badgeColor))
                Text(badgeLabel, color = badgeColor, fontSize = 11.5.sp, fontWeight = FontWeight.SemiBold)
            }
            Text(statusDetail, Modifier.weight(1f), color = c.muted, fontSize = 11.5.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }

        Box(Modifier.fillMaxWidth().weight(1f).padding(horizontal = 14.dp, vertical = 6.dp), contentAlignment = Alignment.Center) {
            Column(
                Modifier.fillMaxWidth()
                    .shadow(10.dp, RoundedCornerShape(24.dp))
                    .clip(RoundedCornerShape(24.dp))
                    .background(bezelFill)
                    .border(0.5.dp, bezelStroke, RoundedCornerShape(24.dp))
                    .padding(10.dp),
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Box(
                    Modifier.fillMaxWidth().aspectRatio(1280f / 800f).clip(RoundedCornerShape(14.dp))
                        .background(Color(0xFF0F172A)).border(0.5.dp, bezelStroke, RoundedCornerShape(14.dp)),
                    contentAlignment = Alignment.Center
                ) {
                    val frame = s.cloudDesktopFrame
                    if (frame != null) {
                        BoxWithConstraints(Modifier.fillMaxSize()) {
                            val boxW = maxWidth
                            val boxH = maxHeight
                            val imgAspect = frame.width.toFloat() / frame.height.coerceAtLeast(1).toFloat()
                            val boxAspect = if (boxH.value > 0f) boxW.value / boxH.value else imgAspect
                            val fitW = if (boxAspect > imgAspect) boxH * imgAspect else boxW
                            val fitH = if (boxAspect > imgAspect) boxH else boxW / imgAspect
                            val offX = (boxW - fitW) / 2f
                            val offY = (boxH - fitH) / 2f
                            Image(
                                bitmap = frame,
                                contentDescription = "云端电脑画面",
                                contentScale = ContentScale.Fit,
                                modifier = Modifier.fillMaxSize()
                                    .graphicsLayer {
                                        scaleX = zoomScale
                                        scaleY = zoomScale
                                        translationX = panX
                                        translationY = panY
                                    }
                                    .pointerInput(s.cloudDesktopUserControl, tapMode, frame.width, frame.height) {
                                        fun mapToNorm(ox: Float, oy: Float): Pair<Float, Float>? {
                                            if (size.width <= 0 || size.height <= 0) return null
                                            val cx = size.width / 2f
                                            val cy = size.height / 2f
                                            val unx = (ox - cx - panX) / zoomScale + cx
                                            val uny = (oy - cy - panY) / zoomScale + cy
                                            val fAspect = frame.width.toFloat() / frame.height.coerceAtLeast(1).toFloat()
                                            val sAspect = size.width.toFloat() / size.height.toFloat()
                                            val fw = if (sAspect > fAspect) size.height * fAspect else size.width.toFloat()
                                            val fh = if (sAspect > fAspect) size.height.toFloat() else size.width / fAspect
                                            val fx0 = (size.width - fw) / 2f
                                            val fy0 = (size.height - fh) / 2f
                                            val nx = (unx - fx0) / fw
                                            val ny = (uny - fy0) / fh
                                            if (nx < -0.02f || nx > 1.02f || ny < -0.02f || ny > 1.02f) return null
                                            return nx.coerceIn(0f, 1f) to ny.coerceIn(0f, 1f)
                                        }
                                        detectTapGestures(
                                            onTap = { pt ->
                                                focus.clearFocus()
                                                if (!s.cloudDesktopUserControl) {
                                                    model.cloudDesktopTakeover()
                                                    return@detectTapGestures
                                                }
                                                mapToNorm(pt.x, pt.y)?.let { (nx, ny) ->
                                                    model.cloudDesktopClick(nx, ny, tapMode)
                                                }
                                            },
                                            onLongPress = { pt ->
                                                focus.clearFocus()
                                                if (!s.cloudDesktopUserControl) {
                                                    model.cloudDesktopTakeover()
                                                    return@detectTapGestures
                                                }
                                                mapToNorm(pt.x, pt.y)?.let { (nx, ny) ->
                                                    model.cloudDesktopClick(nx, ny, "right_click")
                                                }
                                            }
                                        )
                                    }
                                    .pointerInput(s.cloudDesktopUserControl) {
                                        var scrollAccumY = 0f
                                        detectTransformGestures { centroid, pan, zoom, _ ->
                                            if (zoom != 1f || zoomScale > 1.05f) {
                                                val newScale = (zoomScale * zoom).coerceIn(1f, 3.2f)
                                                val maxPanX = (size.width * (newScale - 1f)) / 2f
                                                val maxPanY = (size.height * (newScale - 1f)) / 2f
                                                zoomScale = newScale
                                                panX = if (newScale <= 1.02f) 0f else (panX + pan.x).coerceIn(-maxPanX, maxPanX)
                                                panY = if (newScale <= 1.02f) 0f else (panY + pan.y).coerceIn(-maxPanY, maxPanY)
                                            } else if (s.cloudDesktopUserControl) {
                                                scrollAccumY += pan.y
                                                if (kotlin.math.abs(scrollAccumY) >= 48f) {
                                                    val dir = if (scrollAccumY > 0f) "up" else "down"
                                                    scrollAccumY = 0f
                                                    val nx = (centroid.x / size.width.coerceAtLeast(1)).coerceIn(0f, 1f)
                                                    val ny = (centroid.y / size.height.coerceAtLeast(1)).coerceIn(0f, 1f)
                                                    model.cloudDesktopScroll(nx, ny, dir, 3)
                                                }
                                            }
                                        }
                                    }
                            )
                            s.cloudDesktopTapNorm?.let { (nx, ny) ->
                                Box(
                                    Modifier.offset(x = offX + fitW * nx - 7.dp, y = offY + fitH * ny - 7.dp).size(14.dp)
                                        .clip(CircleShape).background(c.accent.copy(alpha = 0.55f)).border(1.5.dp, Color.White, CircleShape)
                                )
                            }
                            if (zoomScale > 1.05f) {
                                Text(
                                    "${String.format(java.util.Locale.US, "%.1f", zoomScale)}x 复位",
                                    Modifier.align(Alignment.TopEnd).padding(8.dp)
                                        .clip(RoundedCornerShape(10.dp)).background(Color(0xCC0F172A))
                                        .iosPressScale(0.94f) { zoomScale = 1f; panX = 0f; panY = 0f }
                                        .padding(horizontal = 9.dp, vertical = 4.dp),
                                    color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.SemiBold
                                )
                            }
                        }
                    } else {
                        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Text(
                                if (s.cloudDesktopLoading) "正在同步云电脑画面…"
                                else if (s.cloudDesktopConfigured) "点击下方图标开启浏览器或终端"
                                else "尚未配置 24 小时云电脑",
                                color = Color(0xFFE2E8F0), fontSize = 14.sp, fontWeight = FontWeight.Medium
                            )
                            Text(quotaLabel, color = Color(0xFF94A3B8), fontSize = 11.5.sp)
                            if (!s.cloudDesktopConfigured && !s.cloudDesktopLoading) {
                                Box(
                                    Modifier.clip(RoundedCornerShape(10.dp)).background(c.accent)
                                        .iosPressScale(0.96f) {
                                            model.closePanel()
                                            model.open(model.ui.primary)
                                            model.sendQuickPrompt("我想配置 24 小时常驻云电脑，请用云电脑自动部署向导帮我一步步连接并部署。")
                                        }.padding(horizontal = 14.dp, vertical = 8.dp)
                                ) {
                                    Text("对话一键部署云电脑", color = Color.White, fontSize = 12.5.sp, fontWeight = FontWeight.SemiBold)
                                }
                            }
                        }
                    }
                }

                Row(
                    Modifier.padding(top = 10.dp).clip(RoundedCornerShape(16.dp)).background(dockFill)
                        .border(0.6.dp, dockBorder, RoundedCornerShape(16.dp)).padding(horizontal = 10.dp, vertical = 6.dp),
                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    listOf(
                        Triple("browser", "网页", "浏览器"),
                        Triple("terminal", ">_", "终端"),
                        Triple("files", "文件", "文件"),
                        Triple("editor", "编辑", "编辑")
                    ).forEach { (appKey, iconTxt, label) ->
                        Box(
                            Modifier.size(40.dp).clip(RoundedCornerShape(10.dp))
                                .background(if (appKey == "terminal") Color(0xFF1E293B) else c.fill)
                                .border(0.5.dp, dockBorder, RoundedCornerShape(10.dp))
                                .iosPressScale(0.92f, enabled = !s.cloudDesktopLoading) {
                                    focus.clearFocus()
                                    model.cloudDesktopLaunchApp(appKey, if (appKey == "browser") urlInput else "")
                                }
                                .semantics { contentDescription = label },
                            contentAlignment = Alignment.Center
                        ) {
                            Text(iconTxt, color = if (appKey == "terminal") Color(0xFFF8FAFC) else c.ink, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                        }
                    }
                }
            }
        }

        AnimatedVisibility(
            visible = s.cloudDesktopUserControl,
            enter = if (reduceMotion) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(150)),
            exit = if (reduceMotion) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(110))
        ) {
            Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 6.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    BasicTextField(
                        value = urlInput, onValueChange = { urlInput = it }, singleLine = true,
                        textStyle = TextStyle(color = c.ink, fontSize = 13.5.sp), cursorBrush = SolidColor(c.accent),
                        modifier = Modifier.weight(1f).clip(RoundedCornerShape(12.dp)).background(c.surface).border(0.5.dp, c.line, RoundedCornerShape(12.dp)).padding(horizontal = 12.dp, vertical = 10.dp),
                        decorationBox = { inner -> Box { if (urlInput.isEmpty()) Text("输入网址前往…", color = c.faint, fontSize = 13.5.sp); inner() } }
                    )
                    Text(
                        "前往",
                        Modifier.heightIn(min = 40.dp).clip(RoundedCornerShape(12.dp)).background(c.accent)
                            .iosPressScale(0.95f) { focus.clearFocus(); model.cloudDesktopNavigate(urlInput) }.padding(horizontal = 14.dp, vertical = 10.dp),
                        color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold
                    )
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                    BasicTextField(
                        value = typeInput, onValueChange = { typeInput = it }, singleLine = true,
                        textStyle = TextStyle(color = c.ink, fontSize = 13.5.sp), cursorBrush = SolidColor(c.accent),
                        modifier = Modifier.weight(1f).clip(RoundedCornerShape(12.dp)).background(c.surface).border(0.5.dp, c.line, RoundedCornerShape(12.dp)).padding(horizontal = 12.dp, vertical = 10.dp),
                        decorationBox = { inner -> Box { if (typeInput.isEmpty()) Text("输入文字发送至当前窗口…", color = c.faint, fontSize = 13.5.sp); inner() } }
                    )
                    Text(
                        "发送",
                        Modifier.heightIn(min = 40.dp).clip(RoundedCornerShape(12.dp)).background(c.accent)
                            .iosPressScale(0.95f) { sendTextAction(false) }.padding(horizontal = 12.dp, vertical = 10.dp),
                        color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        "发+回车",
                        Modifier.heightIn(min = 40.dp).clip(RoundedCornerShape(12.dp)).background(c.surface).border(0.5.dp, c.line, RoundedCornerShape(12.dp))
                            .iosPressScale(0.95f) { sendTextAction(true) }.padding(horizontal = 10.dp, vertical = 10.dp),
                        color = c.ink, fontSize = 12.sp, fontWeight = FontWeight.Medium
                    )
                }
                Row(
                    Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    val modeLabel = when (tapMode) {
                        "double_click" -> "模式:双击"
                        "right_click" -> "模式:右键"
                        else -> "模式:左键"
                    }
                    Box(
                        Modifier.heightIn(min = 38.dp).clip(RoundedCornerShape(10.dp))
                            .background(if (tapMode == "click") c.surface else c.accent.copy(alpha = 0.16f))
                            .border(0.5.dp, if (tapMode == "click") c.line else c.accent, RoundedCornerShape(10.dp))
                            .iosPressScale(0.95f) {
                                tapMode = when (tapMode) {
                                    "click" -> "double_click"
                                    "double_click" -> "right_click"
                                    else -> "click"
                                }
                            }.padding(horizontal = 10.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(modeLabel, color = if (tapMode == "click") c.ink else c.accent, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                    }
                    listOf(
                        "回车" to { model.cloudDesktopKey("Enter") },
                        "退格" to { model.cloudDesktopKey("Backspace") },
                        "Tab" to { model.cloudDesktopKey("Tab") },
                        "Esc" to { model.cloudDesktopKey("Escape") },
                        "后退" to { model.cloudDesktopKey("alt+Left") },
                        "刷新" to { model.cloudDesktopKey("ctrl+r") },
                        "全选" to { model.cloudDesktopKey("ctrl+a") },
                        "复制" to { model.cloudDesktopKey("ctrl+c") },
                        "粘贴" to { model.cloudDesktopKey("ctrl+v") },
                        "上滚" to { model.cloudDesktopScroll(0.5f, 0.5f, "up", 4) },
                        "下滚" to { model.cloudDesktopScroll(0.5f, 0.5f, "down", 4) }
                    ).forEach { (label, act) ->
                        Box(
                            Modifier.heightIn(min = 38.dp).clip(RoundedCornerShape(10.dp)).background(c.surface).border(0.5.dp, c.line, RoundedCornerShape(10.dp))
                                .iosPressScale(0.95f) { act() }.padding(horizontal = 11.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(label, color = c.ink, fontSize = 12.sp, textAlign = TextAlign.Center)
                        }
                    }
                }
            }
        }

        Row(
            Modifier.fillMaxWidth().padding(horizontal = 18.dp, vertical = 12.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(Modifier.weight(1f)) {
                Text(
                    if (s.cloudDesktopUserControl) "人工接管中（可直接点击画面或输入）" else "$botName · AI 控制中",
                    color = if (s.cloudDesktopUserControl) c.green else c.muted, fontSize = 13.sp, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis
                )
                Text(quotaLabel, color = c.faint, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            Text(
                if (s.cloudDesktopUserControl) "交还控制权" else "获取控制权",
                Modifier.padding(start = 10.dp).clip(CircleShape).background(if (s.cloudDesktopUserControl) c.fill else c.accent)
                    .iosPressScale(0.95f) {
                        focus.clearFocus()
                        if (s.cloudDesktopUserControl) model.cloudDesktopHandback() else model.cloudDesktopTakeover()
                    }.padding(horizontal = 16.dp, vertical = 8.dp),
                color = if (s.cloudDesktopUserControl) c.ink else Color.White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold
            )
        }
    }
}
