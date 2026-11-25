package dev.bubuki.proactive

import android.graphics.BitmapFactory
import android.util.Base64
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.tween
import androidx.compose.foundation.*
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.min

@Composable
internal fun RootPhoneScreen(
    state: RootPhoneState,
    busy: Boolean,
    error: String,
    command: suspend (String, JSONObject) -> Boolean,
    settings: () -> Unit,
    back: () -> Unit
) {
    val dark = appearanceDark()
    val activity = LocalContext.current as androidx.activity.ComponentActivity
    LaunchedEffect(dark) { activity.systemBarsFor(dark) }
    val powerSave = rememberPowerSaveActive(false)
    val reduceMotion = animationsOff(activity) || powerSave
    val palette = rememberAnimatedPulsePalette(dark, reduceMotion)

    PulseTheme(dark = dark, reduceMotion = reduceMotion) {
        CompositionLocalProvider(LocalPulsePalette provides palette) {
            val c = LocalPulsePalette.current
            val scope = rememberCoroutineScope()
            val focus = LocalFocusManager.current
            val haptics = LocalHapticFeedback.current
            val scrollState = rememberScrollState()
            val shizuku = remember(activity) { ShizukuController.get(activity) }
            val shizukuState by shizuku.getStateFlow().collectAsState()
            LaunchedEffect(Unit) { shizuku.refreshState() }
            val display = remember(state.display) { runCatching { JSONObject(state.display) }.getOrDefault(JSONObject()) }
            val created = display.optBoolean("created")
            val manual = state.phase != "running" && !busy
            var picking by remember { mutableStateOf(false) }
            var appQuery by remember { mutableStateOf("") }
            var input by remember { mutableStateOf("") }
            val image by produceState<ImageBitmap?>(null, state.image) {
                value = withContext(Dispatchers.Default) {
                    if (state.image.isBlank()) null
                    else runCatching {
                        val bytes = Base64.decode(state.image, Base64.DEFAULT)
                        BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap()
                    }.getOrNull()
                }
            }
            fun act(op: String, args: JSONObject = JSONObject()) {
                scope.launch { command(op, args) }
            }

            val hdrDivAlpha by animateFloatAsState(
                targetValue = if (scrollState.value > 6) 0.65f else 0f,
                animationSpec = if (reduceMotion) snap() else tween(160),
                label = "rp_hdr_div"
            )

            Column(
                Modifier
                    .fillMaxSize()
                    .background(c.bg)
                    .windowInsetsPadding(WindowInsets.systemBars.union(WindowInsets.ime))
            ) {
                Surface(color = c.surface.copy(alpha = 0.96f), tonalElevation = 0.dp) {
                    Column {
                        Row(
                            Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            PulseRoundButton("back", "返回私人空间", { focus.clearFocus(); back() }, background = true)
                            Column(Modifier.padding(start = 10.dp).weight(1f)) {
                                Text(
                                    "手机工作区",
                                    color = c.ink,
                                    fontWeight = FontWeight.SemiBold,
                                    fontSize = 18.sp,
                                    letterSpacing = (-0.3).sp
                                )
                                val modeBadge = when {
                                    state.privilegeMode == "shizuku" -> "Shizuku"
                                    state.privilegeMode == "wireless_adb" -> "无线 ADB"
                                    state.root -> "Root"
                                    else -> "特权"
                                }
                                Text(
                                    if (created) "独立屏 #${display.optInt("displayId")} · $modeBadge" else "沙盒虚拟屏幕",
                                    color = c.muted,
                                    fontSize = 11.5.sp,
                                    style = TextStyle(fontFeatureSettings = "tnum")
                                )
                            }
                            if (state.active) {
                                Box(
                                    Modifier.heightIn(min = 40.dp)
                                        .clip(RoundedCornerShape(12.dp))
                                        .background(c.warn.copy(alpha = 0.12f))
                                        .iosPressScale(0.94f) {
                                            haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                                            act("stop")
                                        }
                                        .padding(horizontal = 13.dp, vertical = 8.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    Text("停止", color = c.warn, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                                }
                            }
                        }
                        HorizontalDivider(color = c.line.copy(alpha = hdrDivAlpha), thickness = 0.5.dp)
                    }
                }

                Column(
                    Modifier
                        .fillMaxWidth()
                        .verticalScroll(scrollState)
                        .padding(horizontal = 16.dp, vertical = 16.dp),
                    verticalArrangement = Arrangement.spacedBy(14.dp)
                ) {
                    Column(Modifier.fillMaxWidth().padding(horizontal = 4.dp, vertical = 4.dp)) {
                        Text(
                            "ISOLATED PHONE WORKSPACE",
                            color = c.accent,
                            fontSize = 10.5.sp,
                            fontWeight = FontWeight.SemiBold,
                            letterSpacing = 0.8.sp
                        )
                        Text(
                            "在独立屏里，把事情做完。",
                            Modifier.padding(top = 6.dp),
                            color = c.ink,
                            fontWeight = FontWeight.Bold,
                            fontSize = 24.sp,
                            letterSpacing = (-0.5).sp
                        )
                        Text(
                            "所有触控与脚本操作只运行在隔离虚拟屏中，不抢占主桌面焦点。",
                            Modifier.padding(top = 4.dp),
                            color = c.muted,
                            fontSize = 13.sp,
                            lineHeight = 19.sp
                        )
                    }

                    val privReady = state.privileged || state.root
                    val statusDot by animateColorAsState(
                        targetValue = if (privReady) c.green else c.muted,
                        animationSpec = if (reduceMotion) snap() else tween(180),
                        label = "rp_status_dot"
                    )
                    Row(
                        Modifier.fillMaxWidth()
                            .clip(RoundedCornerShape(16.dp))
                            .background(c.surface)
                            .padding(horizontal = 15.dp, vertical = 14.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Box(Modifier.size(8.dp).clip(CircleShape).background(statusDot))
                        val statusLabel = when {
                            busy -> "正在连接工作区权限…"
                            state.sdk < 31 -> "独立虚拟屏需要 Android 12 或更高版本"
                            state.root && state.privilegeMode == "root" -> "Root 已授权 · uid 0"
                            state.privilegeMode == "shizuku" && state.privileged -> "Shizuku 已授权 · uid ${state.uid}"
                            state.privileged -> "无线 ADB 已授权 · uid ${state.uid}"
                            shizukuState.binderAlive && !shizukuState.preV11 && !shizukuState.granted -> "Shizuku 运行中 · 等待授予权限"
                            else -> "尚未授权 Root / Shizuku / 无线 ADB"
                        }
                        Text(
                            statusLabel,
                            Modifier.padding(start = 10.dp).weight(1f),
                            color = c.ink,
                            fontSize = 13.5.sp,
                            fontWeight = FontWeight.Medium,
                            style = TextStyle(fontFeatureSettings = "tnum")
                        )
                    }

                    if (!privReady) {
                        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            val canConnect = !busy && state.sdk >= 31
                            val needsShizukuGrant = shizukuState.binderAlive && !shizukuState.preV11 && !shizukuState.granted
                            Box(
                                Modifier.fillMaxWidth()
                                    .heightIn(min = 48.dp)
                                    .clip(RoundedCornerShape(14.dp))
                                    .background(if (canConnect) c.accent else c.fill)
                                    .iosPressScale(0.97f, enabled = canConnect) {
                                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                        if (needsShizukuGrant) shizuku.requestPermission() else act("connect")
                                    }
                                    .padding(vertical = 13.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text(
                                    if (needsShizukuGrant) "一键授权 Shizuku（免 Root）" else "连接 Root / Shizuku / 无线 ADB",
                                    color = if (canConnect) Color.White else c.muted,
                                    fontSize = 15.sp,
                                    fontWeight = FontWeight.SemiBold
                                )
                            }
                            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                if (shizukuState.installed && !shizukuState.binderAlive && !busy) {
                                    Box(
                                        Modifier.weight(1f)
                                            .heightIn(min = 42.dp)
                                            .clip(RoundedCornerShape(12.dp))
                                            .background(c.surface)
                                            .iosPressScale(0.97f) {
                                                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                                shizuku.openShizukuApp(activity)
                                            }
                                            .padding(vertical = 10.dp),
                                        contentAlignment = Alignment.Center
                                    ) {
                                        Text("打开 Shizuku 启动服务", color = c.accent, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                                    }
                                }
                                Box(
                                    Modifier.weight(1f)
                                        .heightIn(min = 42.dp)
                                        .clip(RoundedCornerShape(12.dp))
                                        .background(c.surface)
                                        .iosPressScale(0.97f) {
                                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                            settings()
                                        }
                                        .padding(vertical = 10.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    Text("配置 Shizuku / 无线调试", color = c.ink, fontSize = 13.sp, fontWeight = FontWeight.Medium)
                                }
                            }
                            Text(
                                "免 Root 推荐先用「无线调试」启动 Shizuku（切流量也不掉线），或前往设置配对内置无线 ADB；未授权前手机控制工具将保持静默。",
                                color = c.muted,
                                fontSize = 12.sp,
                                lineHeight = 18.sp,
                                modifier = Modifier.padding(horizontal = 4.dp)
                            )
                        }
                    } else {
                        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                                val canCreate = !created && !busy
                                val canOpenApps = created && manual
                                Box(
                                    Modifier.weight(1f)
                                        .heightIn(min = 44.dp)
                                        .clip(RoundedCornerShape(13.dp))
                                        .background(if (canCreate) c.accent else c.surface)
                                        .iosPressScale(0.96f, enabled = canCreate) {
                                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                            act("create")
                                        }
                                        .padding(vertical = 11.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    Text(
                                        if (created) "工作区已就绪" else "创建工作区",
                                        color = if (canCreate) Color.White else c.muted,
                                        fontSize = 13.5.sp,
                                        fontWeight = FontWeight.SemiBold
                                    )
                                }
                                Box(
                                    Modifier.weight(1f)
                                        .heightIn(min = 44.dp)
                                        .clip(RoundedCornerShape(13.dp))
                                        .background(c.surface)
                                        .iosPressScale(0.96f, enabled = canOpenApps) {
                                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                            appQuery = ""
                                            act("apps")
                                            picking = true
                                        }
                                        .padding(vertical = 11.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    Text(
                                        "打开应用",
                                        color = if (canOpenApps) c.accent else c.faint,
                                        fontSize = 13.5.sp,
                                        fontWeight = FontWeight.SemiBold
                                    )
                                }
                            }
                            if (created) {
                                val overlayShowing by FloatingVirtualDisplay.overlayVisible.collectAsState()
                                Box(
                                    Modifier.fillMaxWidth()
                                        .heightIn(min = 46.dp)
                                        .clip(RoundedCornerShape(14.dp))
                                        .background(c.accent)
                                        .iosPressScale(0.97f) {
                                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                            if (!FloatingVirtualDisplay.canOverlay(activity)) {
                                                FloatingVirtualDisplay.requestOverlayPermission(activity)
                                            } else {
                                                FloatingVirtualDisplay.show(activity, forceExpand = false)
                                                val home = android.content.Intent(android.content.Intent.ACTION_MAIN)
                                                    .addCategory(android.content.Intent.CATEGORY_HOME)
                                                    .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
                                                runCatching { activity.startActivity(home) }
                                            }
                                        }
                                        .padding(horizontal = 14.dp, vertical = 12.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    Text(
                                        when {
                                            !FloatingVirtualDisplay.canOverlay(activity) -> "开启悬浮窗权限 · 在桌面悬停预览"
                                            overlayShowing -> "虚拟屏已悬浮 · 切回桌面查看小窗"
                                            else -> "弹出桌面悬浮小窗"
                                        },
                                        color = Color.White,
                                        fontWeight = FontWeight.SemiBold,
                                        fontSize = 14.sp
                                    )
                                }
                            }
                        }
                    }

                    if (error.isNotBlank() || state.error.isNotBlank()) {
                        Text(
                            error.ifBlank { state.error },
                            Modifier.fillMaxWidth()
                                .clip(RoundedCornerShape(12.dp))
                                .background(c.warn.copy(alpha = 0.10f))
                                .padding(horizontal = 12.dp, vertical = 10.dp),
                            color = c.warn,
                            fontSize = 12.5.sp,
                            lineHeight = 18.sp
                        )
                    }

                    if (created) {
                        val frame = remember(state.imageInfo, state.display) {
                            runCatching { JSONObject(state.imageInfo.ifBlank { state.display }) }.getOrDefault(JSONObject())
                        }
                        val width = frame.optInt("width", display.optInt("width", 1080)).coerceAtLeast(1)
                        val height = frame.optInt("height", display.optInt("height", 2400)).coerceAtLeast(1)
                        val curTier = display.optString("tier", FloatingVirtualDisplay.currentTier().rpcName)

                        Column(
                            Modifier.fillMaxWidth()
                                .clip(RoundedCornerShape(16.dp))
                                .background(c.surface)
                                .padding(14.dp),
                            verticalArrangement = Arrangement.spacedBy(10.dp)
                        ) {
                            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    "屏幕尺寸档位",
                                    Modifier.weight(1f),
                                    color = c.ink,
                                    fontSize = 14.sp,
                                    fontWeight = FontWeight.SemiBold
                                )
                                Text(
                                    "${width}×${height}",
                                    color = c.muted,
                                    fontSize = 12.sp,
                                    style = TextStyle(fontFeatureSettings = "tnum")
                                )
                            }
                            Row(
                                Modifier.fillMaxWidth()
                                    .clip(RoundedCornerShape(11.dp))
                                    .background(c.fill)
                                    .padding(3.dp),
                                horizontalArrangement = Arrangement.spacedBy(3.dp)
                            ) {
                                listOf("compact" to "紧凑 S", "balanced" to "标准 M", "large" to "大屏 L").forEach { (tierKey, label) ->
                                    val selected = curTier == tierKey
                                    Box(
                                        Modifier.weight(1f)
                                            .heightIn(min = 36.dp)
                                            .clip(RoundedCornerShape(8.dp))
                                            .background(if (selected) c.surface else Color.Transparent)
                                            .iosPressScale(0.96f, enabled = manual) {
                                                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                                act("resize", JSONObject().put("tier", tierKey))
                                            },
                                        contentAlignment = Alignment.Center
                                    ) {
                                        Text(
                                            label,
                                            color = if (selected) c.ink else c.muted,
                                            fontSize = 12.5.sp,
                                            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Medium
                                        )
                                    }
                                }
                            }
                            BoxWithConstraints(
                                Modifier.fillMaxWidth()
                                    .clip(RoundedCornerShape(14.dp))
                                    .background(c.elevated)
                                    .border(0.5.dp, c.line, RoundedCornerShape(14.dp))
                                    .padding(8.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                val h = min(460f, maxWidth.value * height / width).dp
                                val w = (h.value * width / height).dp
                                val curWidth by rememberUpdatedState(width)
                                val curHeight by rememberUpdatedState(height)
                                val bitmap = image
                                if (bitmap == null) {
                                    Text(
                                        "打开任意应用后，实时独立屏画面将在此显示。",
                                        Modifier.padding(vertical = 40.dp, horizontal = 20.dp),
                                        color = c.muted,
                                        fontSize = 12.5.sp,
                                        textAlign = TextAlign.Center
                                    )
                                } else {
                                    Image(
                                        bitmap,
                                        "独立工作区实时画面",
                                        Modifier.size(w, h).clip(RoundedCornerShape(9.dp)).pointerInput(manual) {
                                            if (!manual) return@pointerInput
                                            awaitEachGesture {
                                                val down = awaitFirstDown(requireUnconsumed = false)
                                                down.consume()
                                                fun x(v: Float) = (v / size.width.coerceAtLeast(1) * curWidth).toInt().coerceIn(0, (curWidth - 1).coerceAtLeast(0))
                                                fun y(v: Float) = (v / size.height.coerceAtLeast(1) * curHeight).toInt().coerceIn(0, (curHeight - 1).coerceAtLeast(0))
                                                var end = down.position
                                                var sentDown = false
                                                try {
                                                    RootPhoneHarness.sendLiveTouch(activity, "down", x(down.position.x), y(down.position.y))
                                                    sentDown = true
                                                    var lastMoveAt = android.os.SystemClock.uptimeMillis()
                                                    var lastX = x(down.position.x)
                                                    var lastY = y(down.position.y)
                                                    do {
                                                        val event = awaitPointerEvent()
                                                        val pointer = event.changes.firstOrNull { it.id == down.id } ?: break
                                                        end = pointer.position
                                                        pointer.consume()
                                                        if (pointer.pressed) {
                                                            val now = android.os.SystemClock.uptimeMillis()
                                                            val tx = x(end.x)
                                                            val ty = y(end.y)
                                                            if (now - lastMoveAt >= 10L || abs(tx - lastX) + abs(ty - lastY) >= 2) {
                                                                lastMoveAt = now; lastX = tx; lastY = ty
                                                                RootPhoneHarness.sendLiveTouch(activity, "move", tx, ty)
                                                            }
                                                        }
                                                        if (!pointer.pressed) break
                                                    } while (true)
                                                } finally {
                                                    if (sentDown) RootPhoneHarness.sendLiveTouch(activity, "up", x(end.x), y(end.y))
                                                }
                                            }
                                        }
                                    )
                                }
                            }
                            Text(
                                if (state.phase == "running") "助手正在操控，停止后可手动点按画面。" else "直接在上方画面轻点或滑动即可控制独立屏。",
                                color = c.muted,
                                fontSize = 11.5.sp
                            )
                            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                Box(
                                    Modifier.weight(1f).heightIn(min = 40.dp).clip(RoundedCornerShape(10.dp))
                                        .background(c.fill)
                                        .iosPressScale(0.95f, enabled = !busy) { act("capture") }
                                        .padding(vertical = 8.dp),
                                    contentAlignment = Alignment.Center
                                ) { Text("刷新画面", color = if (!busy) c.ink else c.faint, fontSize = 12.5.sp, fontWeight = FontWeight.Medium) }
                                Box(
                                    Modifier.weight(1f).heightIn(min = 40.dp).clip(RoundedCornerShape(10.dp))
                                        .background(c.fill)
                                        .iosPressScale(0.95f, enabled = manual) { act("key", JSONObject().put("key", "back")) }
                                        .padding(vertical = 8.dp),
                                    contentAlignment = Alignment.Center
                                ) { Text("返回键", color = if (manual) c.ink else c.faint, fontSize = 12.5.sp, fontWeight = FontWeight.Medium) }
                                Box(
                                    Modifier.weight(1f).heightIn(min = 40.dp).clip(RoundedCornerShape(10.dp))
                                        .background(c.warn.copy(alpha = if (manual && !state.active) 0.12f else 0.05f))
                                        .iosPressScale(0.95f, enabled = manual && !state.active) {
                                            haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                                            act("release", JSONObject().put("destroy", true).put("user_close", true))
                                        }
                                        .padding(vertical = 8.dp),
                                    contentAlignment = Alignment.Center
                                ) { Text("销毁屏", color = if (manual && !state.active) c.warn else c.faint, fontSize = 12.5.sp, fontWeight = FontWeight.Medium) }
                            }
                            PulseInputField(
                                label = "向独立屏输入文字",
                                value = input,
                                placeholder = "输入需要粘贴或键入的内容…",
                                singleLine = false,
                                onChange = { input = it.take(4000) }
                            )
                            val canSendText = manual && input.isNotEmpty()
                            Box(
                                Modifier.fillMaxWidth()
                                    .heightIn(min = 42.dp)
                                    .clip(RoundedCornerShape(11.dp))
                                    .background(if (canSendText) c.accent else c.fill)
                                    .iosPressScale(0.97f, enabled = canSendText) {
                                        scope.launch {
                                            if (command("text", JSONObject().put("text", input))) input = ""
                                        }
                                    }
                                    .padding(vertical = 10.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text("发送文字到虚拟屏", color = if (canSendText) Color.White else c.faint, fontSize = 13.5.sp, fontWeight = FontWeight.SemiBold)
                            }
                        }
                    }

                    if (state.active || state.result.isNotBlank()) {
                        val traceLen = remember(state.trace) { runCatching { org.json.JSONArray(state.trace).length() }.getOrDefault(0) }
                        Column(
                            Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(15.dp),
                            verticalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Text(
                                when (state.phase) {
                                    "running" -> "正在执行"
                                    "waiting_user" -> "需要人工接管"
                                    "stopping" -> "正在停止"
                                    "failed", "stopped" -> "执行已停止"
                                    else -> "本轮结果"
                                },
                                color = c.ink,
                                fontSize = 15.sp,
                                fontWeight = FontWeight.SemiBold
                            )
                            if (state.goal.isNotBlank()) Text(state.goal, color = c.muted, fontSize = 13.sp, lineHeight = 19.sp)
                            if (state.result.isNotBlank()) Text(state.result, color = c.ink, fontSize = 13.5.sp, lineHeight = 20.sp)
                            Text("已记录 $traceLen 个工具动作 · 完整轨迹见对话流", color = c.faint, fontSize = 11.5.sp, style = TextStyle(fontFeatureSettings = "tnum"))
                            if (state.phase == "waiting_user") {
                                Box(
                                    Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(c.accent)
                                        .iosPressScale(0.97f) { act("resume") }.padding(vertical = 11.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    Text("我处理好了，继续执行", color = Color.White, fontSize = 13.5.sp, fontWeight = FontWeight.SemiBold)
                                }
                            }
                        }
                    }

                    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text("模型与系统权限", color = c.muted, fontSize = 12.sp, fontWeight = FontWeight.Medium, modifier = Modifier.padding(start = 6.dp))
                        Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface)) {
                            SettingsCategoryRow(
                                icon = "spark",
                                title = "视觉执行模型",
                                subtitle = if (state.modelReady) "支持识图操控独立屏" else "点击前往设置配置模型",
                                value = if (state.modelReady) state.model else "未配置",
                                onClick = settings
                            )
                            HorizontalDivider(Modifier.padding(start = 58.dp), color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
                            val unifiedStatus = when {
                                state.notifications && state.overlay -> "已开启"
                                state.notifications -> "待开悬浮窗"
                                else -> "去开启"
                            }
                            SettingsCategoryRow(
                                icon = "bell",
                                title = "分级通知",
                                subtitle = "弱通知进通知栏 · 强提醒弹桌面悬浮窗",
                                value = unifiedStatus,
                                onClick = { act("unified_notify") }
                            )
                            HorizontalDivider(Modifier.padding(start = 58.dp), color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
                            SettingsCategoryRow(
                                icon = "check",
                                title = "通知已读同步",
                                subtitle = "同步消除手机通知与待办感知",
                                value = if (state.listener) "已开启" else "去授权",
                                onClick = { act("listener") }
                            )
                            HorizontalDivider(Modifier.padding(start = 58.dp), color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
                            SettingsCategoryRow(
                                icon = "cloud",
                                title = "后台云端连接",
                                subtitle = "保持加密 WebSocket 长连实时唤醒",
                                value = if (state.background) "已开启" else "去设置",
                                onClick = { act("background") }
                            )
                        }
                    }

                    Text(
                        "提示：创建工作区或启动手机任务后，虚拟屏可收缩为贴边小气泡或悬停小窗，点按即展开直触。",
                        color = c.faint,
                        fontSize = 12.sp,
                        lineHeight = 18.sp,
                        modifier = Modifier.padding(horizontal = 4.dp, vertical = 4.dp)
                    )
                }
            }

            if (picking) {
                val filteredApps = remember(state.apps, appQuery) {
                    val q = appQuery.trim()
                    if (q.isEmpty()) state.apps else state.apps.filter { it.name.contains(q, ignoreCase = true) || it.component.contains(q, ignoreCase = true) }
                }
                Dialog(onDismissRequest = { picking = false }, properties = DialogProperties(usePlatformDefaultWidth = false)) {
                    Box(
                        Modifier.fillMaxSize()
                            .background(Color.Black.copy(alpha = if (c.isDark) 0.58f else 0.36f))
                            .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null) { picking = false }
                            .padding(horizontal = 20.dp, vertical = 40.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Column(
                            Modifier.widthIn(max = 420.dp)
                                .fillMaxWidth()
                                .clip(RoundedCornerShape(20.dp))
                                .background(c.surface)
                                .border(0.5.dp, c.line.copy(alpha = 0.7f), RoundedCornerShape(20.dp))
                                .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null) {}
                                .padding(14.dp),
                            verticalArrangement = Arrangement.spacedBy(10.dp)
                        ) {
                            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                                Text("在独立屏打开应用", Modifier.weight(1f).padding(start = 4.dp), color = c.ink, fontSize = 17.sp, fontWeight = FontWeight.SemiBold)
                                PulseRoundButton("close", "关闭", { picking = false }, background = true)
                            }
                            AppleSearchField(query = appQuery, onQueryChange = { appQuery = it.orEmpty() })
                            LazyColumn(Modifier.fillMaxWidth().heightIn(min = 120.dp, max = 380.dp)) {
                                if (state.apps.isEmpty()) {
                                    item(key = "loading_apps") {
                                        Text("正在读取设备可启动应用…", Modifier.padding(vertical = 28.dp).fillMaxWidth(), color = c.muted, fontSize = 13.sp, textAlign = TextAlign.Center)
                                    }
                                } else if (filteredApps.isEmpty()) {
                                    item(key = "empty_apps") {
                                        Text("没有匹配的应用", Modifier.padding(vertical = 28.dp).fillMaxWidth(), color = c.muted, fontSize = 13.sp, textAlign = TextAlign.Center)
                                    }
                                } else {
                                    itemsIndexed(filteredApps, key = { _, a -> a.component }) { idx, app ->
                                        if (idx > 0) HorizontalDivider(Modifier.padding(horizontal = 6.dp), color = c.line.copy(alpha = 0.5f), thickness = 0.5.dp)
                                        Row(
                                            Modifier.fillMaxWidth()
                                                .heightIn(min = 46.dp)
                                                .clip(RoundedCornerShape(10.dp))
                                                .iosPressScale(0.98f) {
                                                    haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                                    picking = false
                                                    act("launch", JSONObject().put("component", app.component))
                                                }
                                                .padding(horizontal = 10.dp, vertical = 10.dp),
                                            verticalAlignment = Alignment.CenterVertically
                                        ) {
                                            Text(app.name, Modifier.weight(1f), color = c.ink, fontSize = 14.5.sp, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                                            PulseIcon("next", c.faint, Modifier.size(15.dp))
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
