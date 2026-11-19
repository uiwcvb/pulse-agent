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
    val isDark = c.bg.luminance() < 0.3f
    val focus = LocalFocusManager.current
    val botName = remember(s.cloudDesktopLiveBotId, s.botId, s.bots) {
        model.bot(s.cloudDesktopLiveBotId ?: s.botId)?.name ?: "助理"
    }
    var urlInput by remember(s.cloudDesktopUrl) { mutableStateOf(s.cloudDesktopUrl) }
    var typeInput by remember { mutableStateOf("") }
    val bezelFill = if (isDark) Color(0xFF1A1D26) else Color(0xFFF7FAFAFC)
    val bezelStroke = if (isDark) Color(0x33FFFFFF) else Color(0x26000000)
    val dockFill = if (isDark) Color(0xCC222633) else Color(0xEBFFFFFF)
    val dockBorder = if (isDark) Color(0x33FFFFFF) else Color(0x1F000000)
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

    Column(modifier.background(c.bg), horizontalAlignment = Alignment.CenterHorizontally) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            Text(
                "↻",
                Modifier.clip(CircleShape).background(c.surface).border(0.6.dp, c.line, CircleShape)
                    .iosPressScale(0.92f) { focus.clearFocus(); model.openCloudDesktop() }.padding(horizontal = 11.dp, vertical = 6.dp),
                color = c.muted, fontSize = 15.sp
            )
            Row(
                Modifier.weight(1f).clip(RoundedCornerShape(12.dp)).background(c.surface).border(0.75.dp, c.line, RoundedCornerShape(12.dp))
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
            Text(
                "■ 停止",
                Modifier.clip(RoundedCornerShape(12.dp)).background(c.warn)
                    .iosPressScale(0.92f) { focus.clearFocus(); model.cloudDesktopStop() }.padding(horizontal = 11.dp, vertical = 7.dp),
                color = Color.White, fontSize = 12.5.sp, fontWeight = FontWeight.Bold
            )
            Text(
                "✕",
                Modifier.clip(CircleShape).background(c.surface).border(0.6.dp, c.line, CircleShape)
                    .iosPressScale(0.92f) { focus.clearFocus(); model.closePanel() }.padding(horizontal = 11.dp, vertical = 6.dp),
                color = c.muted, fontSize = 15.sp
            )
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
                    .shadow(if (isDark) 10.dp else 14.dp, RoundedCornerShape(24.dp))
                    .clip(RoundedCornerShape(24.dp))
                    .background(bezelFill)
                    .border(0.9.dp, bezelStroke, RoundedCornerShape(24.dp))
                    .padding(start = 10.dp, end = 10.dp, top = 12.dp, bottom = 10.dp),
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Box(
                    Modifier.fillMaxWidth().aspectRatio(1280f / 800f).clip(RoundedCornerShape(15.dp))
                        .background(Color(0xFF0F172A)).border(0.6.dp, bezelStroke, RoundedCornerShape(15.dp)),
                    contentAlignment = Alignment.Center
                ) {
                    val frame = s.cloudDesktopFrame
                    if (frame != null) {
                        BoxWithConstraints(Modifier.fillMaxSize()) {
                            val boxW = maxWidth
                            val boxH = maxHeight
                            Image(
                                bitmap = frame,
                                contentDescription = "云端电脑画面",
                                contentScale = ContentScale.Fit,
                                modifier = Modifier.fillMaxSize().pointerInput(s.cloudDesktopUserControl) {
                                    if (!s.cloudDesktopUserControl) return@pointerInput
                                    fun sendAt(ox: Float, oy: Float, act: String) {
                                        if (size.width > 0 && size.height > 0) {
                                            focus.clearFocus()
                                            model.cloudDesktopClick(ox / size.width.toFloat(), oy / size.height.toFloat(), act)
                                        }
                                    }
                                    detectTapGestures(
                                        onTap = { sendAt(it.x, it.y, "click") },
                                        onDoubleTap = { sendAt(it.x, it.y, "double_click") },
                                        onLongPress = { sendAt(it.x, it.y, "right_click") }
                                    )
                                }
                            )
                            s.cloudDesktopTapNorm?.let { (nx, ny) ->
                                Box(
                                    Modifier.offset(x = boxW * nx - 7.dp, y = boxH * ny - 7.dp).size(14.dp)
                                        .clip(CircleShape).background(c.accent.copy(alpha = 0.55f)).border(1.5.dp, Color.White, CircleShape)
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
                        Triple("lite_browser", "网页", "浏览器"),
                        Triple("terminal", ">_", "终端"),
                        Triple("files", "文件", "文件"),
                        Triple("editor", "编辑", "编辑")
                    ).forEach { (appKey, iconTxt, label) ->
                        Box(
                            Modifier.size(38.dp).clip(RoundedCornerShape(10.dp))
                                .background(if (appKey == "terminal") Color(0xFF1E293B) else if (isDark) Color(0xFF2D3343) else Color(0xFFF1F5F9))
                                .border(0.5.dp, dockBorder, RoundedCornerShape(10.dp))
                                .iosPressScale(0.92f, enabled = !s.cloudDesktopLoading) { focus.clearFocus(); model.cloudDesktopLaunchApp(appKey, urlInput) }
                                .semantics { contentDescription = label },
                            contentAlignment = Alignment.Center
                        ) {
                            Text(iconTxt, color = if (appKey == "terminal" || isDark) Color(0xFFF8FAFC) else Color(0xFF1E293B), fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                        }
                    }
                }
            }
        }

        AnimatedVisibility(
            visible = s.cloudDesktopUserControl,
            enter = expandVertically(spring(dampingRatio = 0.80f, stiffness = 400f)) + fadeIn(),
            exit = shrinkVertically(spring(dampingRatio = 0.85f, stiffness = 450f)) + fadeOut()
        ) {
            Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 6.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    BasicTextField(
                        value = urlInput, onValueChange = { urlInput = it }, singleLine = true,
                        textStyle = TextStyle(color = c.ink, fontSize = 13.5.sp), cursorBrush = SolidColor(c.accent),
                        modifier = Modifier.weight(1f).clip(RoundedCornerShape(12.dp)).background(c.surface).border(0.6.dp, c.line, RoundedCornerShape(12.dp)).padding(horizontal = 12.dp, vertical = 10.dp),
                        decorationBox = { inner -> Box { if (urlInput.isEmpty()) Text("输入网址前往…", color = c.faint, fontSize = 13.5.sp); inner() } }
                    )
                    Text(
                        "前往",
                        Modifier.clip(RoundedCornerShape(12.dp)).background(c.accent)
                            .iosPressScale(0.95f) { focus.clearFocus(); model.cloudDesktopNavigate(urlInput) }.padding(horizontal = 14.dp, vertical = 10.dp),
                        color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold
                    )
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                    BasicTextField(
                        value = typeInput, onValueChange = { typeInput = it }, singleLine = true,
                        textStyle = TextStyle(color = c.ink, fontSize = 13.5.sp), cursorBrush = SolidColor(c.accent),
                        modifier = Modifier.weight(1f).clip(RoundedCornerShape(12.dp)).background(c.surface).border(0.6.dp, c.line, RoundedCornerShape(12.dp)).padding(horizontal = 12.dp, vertical = 10.dp),
                        decorationBox = { inner -> Box { if (typeInput.isEmpty()) Text("输入文字发送至当前窗口…", color = c.faint, fontSize = 13.5.sp); inner() } }
                    )
                    Text(
                        "发送",
                        Modifier.clip(RoundedCornerShape(12.dp)).background(c.accent)
                            .iosPressScale(0.95f) { sendTextAction(false) }.padding(horizontal = 12.dp, vertical = 10.dp),
                        color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        "发+回车",
                        Modifier.clip(RoundedCornerShape(12.dp)).background(c.surface).border(0.6.dp, c.line, RoundedCornerShape(12.dp))
                            .iosPressScale(0.95f) { sendTextAction(true) }.padding(horizontal = 10.dp, vertical = 10.dp),
                        color = c.ink, fontSize = 12.sp, fontWeight = FontWeight.Medium
                    )
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    listOf(
                        "▲ 上滚" to { model.cloudDesktopScroll(0.5f, 0.5f, "up", 4) },
                        "▼ 下滚" to { model.cloudDesktopScroll(0.5f, 0.5f, "down", 4) },
                        "回车" to { model.cloudDesktopKey("Enter") },
                        "退格" to { model.cloudDesktopKey("Backspace") },
                        "Esc" to { model.cloudDesktopKey("Escape") }
                    ).forEach { (label, act) ->
                        Text(
                            label,
                            Modifier.weight(1f).clip(RoundedCornerShape(10.dp)).background(c.surface).border(0.6.dp, c.line, RoundedCornerShape(10.dp))
                                .iosPressScale(0.95f) { act() }.padding(vertical = 8.dp),
                            color = c.ink, fontSize = 12.sp, textAlign = TextAlign.Center
                        )
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
