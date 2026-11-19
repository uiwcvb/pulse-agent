package dev.bubuki.proactive

import android.graphics.BitmapFactory
import android.util.Base64
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.AnimationSpec
import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.Canvas as ComposeCanvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.positionChange
import androidx.compose.ui.input.pointer.util.VelocityTracker as ComposeVelocityTracker
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.*
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.roundToInt

/**
 * [Module] FloatingVirtualDisplayCompose
 * [Responsibility] In-app Jetpack Compose PiP fallback (`InAppFloatingVirtualDisplay`) with
 *                  live touch streaming, promote-to-fullscreen, inline IME quick-input bar,
 *                  and synchronized user-force-close cancellation (`BUG-AND-18`).
 */

@Composable
internal fun InAppFloatingVirtualDisplay() {
    val context = LocalContext.current
    if (FloatingVirtualDisplay.canOverlay(context)) return
    val state by RootPhoneHarness.changes.collectAsState()
    val systemOverlayShowing by FloatingVirtualDisplay.overlayVisible.collectAsState()
    val userDismissed by FloatingVirtualDisplay.userDismissedFlow.collectAsState()
    val disp = remember(state.display) { runCatching { JSONObject(state.display) }.getOrDefault(JSONObject()) }
    val created = disp.optBoolean("created")
    val visible = created && !userDismissed && state.phase != "stopping" && state.phase != "stopped" && !systemOverlayShowing
    if (!visible) return
    var expanded by remember { mutableStateOf(false) }
    var pickingApp by remember { mutableStateOf(false) }
    var inputBarOpen by remember { mutableStateOf(false) }
    var hiddenToPill by remember { mutableStateOf(false) }
    var tierState by remember { mutableStateOf(FloatingVirtualDisplay.currentTier()) }
    LaunchedEffect(visible, hiddenToPill) {
        FloatingVirtualDisplay.setInAppPreviewVisible(visible && !hiddenToPill, context)
    }
    DisposableEffect(Unit) {
        onDispose { FloatingVirtualDisplay.setInAppPreviewVisible(false, context) }
    }
    LaunchedEffect(created, state.active, state.phase, userDismissed) {
        if (!created || userDismissed || state.phase == "stopping" || state.phase == "stopped") {
            expanded = false
            pickingApp = false
            inputBarOpen = false
            hiddenToPill = false
        }
    }
    val bitmap by produceState<androidx.compose.ui.graphics.ImageBitmap?>(null, state.image) {
        value = withContext(Dispatchers.Default) {
            if (state.image.isBlank()) null
            else {
                FloatingVirtualDisplay.cachedBitmap()?.asImageBitmap() ?: runCatching {
                    val bytes = Base64.decode(state.image, Base64.DEFAULT)
                    BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap()
                }.getOrNull()
            }
        }
    }
    AnimatedVisibility(
        visible = visible,
        enter = fadeIn(tween(220)),
        exit = fadeOut(tween(180))
    ) {
        BoxWithConstraints(Modifier.fillMaxSize()) {
            val density = LocalDensity.current
            val scope = rememberCoroutineScope()
            fun handleUserClose() {
                hiddenToPill = false
                expanded = false
                pickingApp = false
                inputBarOpen = false
                AgentForegroundService.onUserForceClosedVirtualDisplay(context)
                FloatingVirtualDisplay.hide()
                scope.launch(Dispatchers.IO) {
                    runCatching { RootPhoneHarness.command(context, JSONObject().put("op", "release").put("user_close", true).put("destroy", true)) }
                }
            }
            val topGrabZonePx = with(density) { 40.dp.toPx() }
            var dockPillLeft by remember { mutableStateOf(false) }
            if (hiddenToPill) {
                PearlEdgeBubbleComposable(
                    state = state,
                    previewBitmap = bitmap,
                    onRestore = { hiddenToPill = false; expanded = true },
                    onClose = { handleUserClose() },
                    modifier = Modifier
                        .align(if (dockPillLeft) Alignment.BottomStart else Alignment.BottomEnd)
                        .padding(
                            start = if (dockPillLeft) 12.dp else 0.dp,
                            end = if (dockPillLeft) 0.dp else 12.dp,
                            bottom = 92.dp
                        )
                )
                return@BoxWithConstraints
            }
            val info = remember(state.imageInfo, state.display) {
                runCatching { JSONObject(state.imageInfo.ifBlank { state.display }) }.getOrDefault(JSONObject())
            }
            val dispW = info.optInt("width", 1080).coerceAtLeast(1)
            val dispH = info.optInt("height", 2400).coerceAtLeast(1)
            val bmp = bitmap
            val rawAspect = if (bmp != null && bmp.width > 0 && bmp.height > 0) {
                bmp.height.toFloat() / bmp.width.toFloat()
            } else {
                dispH.toFloat() / dispW.toFloat()
            }
            val aspect = rawAspect.takeIf { it.isFinite() && it > 0.1f } ?: 2.15f
            val padDp = if (expanded) 4.5.dp else 3.5.dp
            val availWDp = (maxWidth - 24.dp - padDp * 2).coerceAtLeast(80.dp)
            val availHDp = (maxHeight - 160.dp - padDp * 2).coerceAtLeast(120.dp)
            val shortSideDp = minOf(maxWidth, maxHeight)
            val frac = when {
                !expanded -> if (aspect < 1f) 0.44f else 0.27f
                aspect < 1f -> tierState.landscapeFrac
                else -> tierState.portraitFrac
            }
            val baseInnerW = shortSideDp * frac
            val innerW = minOf(baseInnerW, availWDp, availHDp / aspect).coerceAtLeast(68.dp)
            val innerH = (innerW * aspect).coerceAtLeast(68.dp)
            val reduceMotion = LocalReduceMotion.current || animationsOff(context)
            val sizeSpec: AnimationSpec<Dp> = if (reduceMotion) snap() else spring(dampingRatio = 0.84f, stiffness = 390f)
            val w by animateDpAsState(innerW + padDp * 2, sizeSpec, label = "pipW")
            val h by animateDpAsState(innerH + padDp * 2, sizeSpec, label = "pipH")
            val maxW = constraints.maxWidth
            val maxH = constraints.maxHeight
            val wPx = with(density) { w.roundToPx() }
            val hPx = with(density) { h.roundToPx() }
            val marginPx = with(density) { 12.dp.roundToPx() }
            val topSafePx = with(density) { 64.dp.roundToPx() }
            val bottomSafePx = with(density) { 92.dp.roundToPx() }
            var corner by remember { mutableStateOf(PipSpringPhysics.Corner.TopEnd) }
            val (anchorX, anchorY) = PipSpringPhysics.cornerPosition(corner, maxW, maxH, wPx, hPx, marginPx, topSafePx, bottomSafePx)
            var dragOffset by remember { mutableStateOf(Offset.Zero) }
            var springJob by remember { mutableStateOf<Job?>(null) }
            val outerShape = RoundedCornerShape(if (expanded) 22.dp else 17.dp)
            val innerShape = RoundedCornerShape(if (expanded) 17.5.dp else 13.5.dp)
            Box(
                Modifier
                    .offset {
                        val minX = -(wPx * 0.42f).roundToInt()
                        val maxX = (maxW - (wPx * 0.58f).roundToInt()).coerceAtLeast(minX)
                        IntOffset(
                            (anchorX + dragOffset.x.roundToInt()).coerceIn(minX, maxX),
                            (anchorY + dragOffset.y.roundToInt()).coerceIn(topSafePx, (maxH - hPx - bottomSafePx).coerceAtLeast(topSafePx))
                        )
                    }
                    .size(w, h)
                    .shadow(if (expanded) 20.dp else 13.dp, outerShape)
                    .clip(outerShape)
                    .background(Color(PEARL_WHITE_FILL_INT), outerShape)
                    .border(0.9.dp, Color(pearlOuterStrokeColorInt(state.phase)), outerShape)
                    .pointerInput(expanded, maxW, maxH, wPx, hPx) {
                        val velocityTracker = ComposeVelocityTracker()
                        awaitEachGesture {
                            val down = awaitFirstDown(requireUnconsumed = false)
                            if (expanded && down.position.y > topGrabZonePx) return@awaitEachGesture
                            springJob?.cancel()
                            velocityTracker.resetTracking()
                            velocityTracker.addPosition(down.uptimeMillis, down.position)
                            var dragged = false
                            var total = Offset.Zero
                            while (true) {
                                val event = awaitPointerEvent()
                                val change = event.changes.firstOrNull { it.id == down.id } ?: break
                                if (!change.pressed) break
                                velocityTracker.addPosition(change.uptimeMillis, change.position)
                                val delta = change.positionChange()
                                total += delta
                                if (!dragged && total.getDistance() > viewConfiguration.touchSlop) {
                                    dragged = true
                                }
                                if (dragged) {
                                    change.consume()
                                    dragOffset += delta
                                }
                            }
                            if (dragged) {
                                val v = velocityTracker.calculateVelocity()
                                val curX = anchorX + dragOffset.x
                                val curY = anchorY + dragOffset.y
                                val edgeSide = PipSpringPhysics.edgeDockSideOnDragUp(
                                    curX.roundToInt(),
                                    wPx,
                                    maxW,
                                    v.x,
                                    marginPx * 3
                                )
                                if (edgeSide != 0) {
                                    dockPillLeft = edgeSide < 0
                                    dragOffset = Offset.Zero
                                    expanded = false
                                    pickingApp = false
                                    inputBarOpen = false
                                    hiddenToPill = true
                                    return@awaitEachGesture
                                }
                                val targetCorner = PipSpringPhysics.nearestCorner(
                                    curX + PipSpringPhysics.project(v.x),
                                    curY + PipSpringPhysics.project(v.y),
                                    maxW, maxH, wPx, hPx, marginPx, topSafePx, bottomSafePx
                                )
                                val (newAnchorX, newAnchorY) = PipSpringPhysics.cornerPosition(targetCorner, maxW, maxH, wPx, hPx, marginPx, topSafePx, bottomSafePx)
                                val startDx = curX - newAnchorX
                                val startDy = curY - newAnchorY
                                corner = targetCorner
                                dragOffset = Offset(startDx, startDy)
                                springJob = scope.launch {
                                    val startNanos = withFrameNanos { it }
                                    while (isActive) {
                                        val nowNanos = withFrameNanos { it }
                                        val tSec = (nowNanos - startNanos) / 1_000_000_000f
                                        if (tSec >= 0.42f) {
                                            dragOffset = Offset.Zero
                                            break
                                        }
                                        val nx = PipSpringPhysics.stepCriticalSpring(startDx, 0f, v.x, tSec)
                                        val ny = PipSpringPhysics.stepCriticalSpring(startDy, 0f, v.y, tSec)
                                        dragOffset = Offset(nx, ny)
                                    }
                                }
                            } else if (!expanded) {
                                expanded = true
                                if (state.phase == "waiting_user" && tierState == FloatingVirtualDisplay.SizeTier.COMPACT) {
                                    tierState = FloatingVirtualDisplay.SizeTier.MEDIUM
                                }
                            }
                        }
                    }
                    .padding(padDp)
            ) {
                Box(
                    Modifier
                        .fillMaxSize()
                        .clip(innerShape)
                        .background(Color(0xFF101218))
                        .border(0.6.dp, Color(PEARL_INNER_HAIRLINE_INT), innerShape)
                ) {
                    if (bmp != null) {
                        Image(
                            bmp,
                            "虚拟屏实时画面",
                            Modifier
                                .fillMaxSize()
                                .pointerInput(expanded, state.phase, state.imageInfo) {
                                    if (!expanded) return@pointerInput
                                    awaitEachGesture {
                                        val down = awaitFirstDown(requireUnconsumed = false)
                                        fun mx(v: Float) = (v / size.width.coerceAtLeast(1) * dispW).toInt().coerceIn(0, dispW - 1)
                                        fun my(v: Float) = (v / size.height.coerceAtLeast(1) * dispH).toInt().coerceIn(0, dispH - 1)
                                        RootPhoneHarness.sendLiveTouch(context, "down", mx(down.position.x), my(down.position.y))
                                        var end = down.position
                                        var lastMoveAt = android.os.SystemClock.uptimeMillis()
                                        var lastX = mx(down.position.x)
                                        var lastY = my(down.position.y)
                                        do {
                                            val ev = awaitPointerEvent()
                                            val ptr = ev.changes.firstOrNull { it.id == down.id } ?: break
                                            end = ptr.position
                                            ptr.consume()
                                            if (ptr.pressed) {
                                                val now = android.os.SystemClock.uptimeMillis()
                                                val tx = mx(end.x)
                                                val ty = my(end.y)
                                                if (now - lastMoveAt >= 10L || abs(tx - lastX) + abs(ty - lastY) >= 2) {
                                                    lastMoveAt = now
                                                    lastX = tx
                                                    lastY = ty
                                                    RootPhoneHarness.sendLiveTouch(context, "move", tx, ty)
                                                }
                                            }
                                            if (!ptr.pressed) break
                                        } while (true)
                                        RootPhoneHarness.sendLiveTouch(context, "up", mx(end.x), my(end.y))
                                    }
                                },
                            contentScale = ContentScale.Crop
                        )
                    } else {
                        Text(
                            if (expanded) "点此打开应用抽屉" else "虚拟屏",
                            color = Color(0x99FFFFFF),
                            fontSize = if (expanded) 10.sp else 9.sp,
                            modifier = Modifier
                                .align(Alignment.Center)
                                .clickable(enabled = expanded) {
                                    pickingApp = true
                                    inputBarOpen = false
                                    scope.launch(Dispatchers.IO) { runCatching { RootPhoneHarness.listLaunchableApps(context) } }
                                }
                                .padding(8.dp)
                        )
                    }
                    if (expanded && pickingApp) {
                        InAppVirtualAppDrawerOverlay(
                            apps = state.apps,
                            onLaunchApp = { comp ->
                                pickingApp = false
                                scope.launch(Dispatchers.IO) {
                                    runCatching { RootPhoneHarness.command(context, JSONObject().put("op", "launch").put("component", comp).put("manual", true)) }
                                }
                            },
                            onClose = { pickingApp = false }
                        )
                    }
                    if (!expanded) {
                        ComposePipCircleButton(
                            modifier = Modifier.align(Alignment.TopStart).padding(4.dp).size(20.dp),
                            onClick = { hiddenToPill = true }
                        ) {
                            val strokeW = 1.4.dp.toPx()
                            val c = Color(0xF2FFFFFF)
                            drawLine(c, Offset(size.width * 0.28f, size.height * 0.5f), Offset(size.width * 0.72f, size.height * 0.5f), strokeW, StrokeCap.Round)
                        }
                        ComposePipCircleButton(
                            modifier = Modifier.align(Alignment.TopEnd).padding(4.dp).size(20.dp),
                            isDanger = true,
                            onClick = { handleUserClose() }
                        ) {
                            val strokeW = 1.4.dp.toPx()
                            val c = Color(0xFFFF7A72)
                            drawLine(c, Offset(size.width * 0.32f, size.height * 0.32f), Offset(size.width * 0.68f, size.height * 0.68f), strokeW, StrokeCap.Round)
                            drawLine(c, Offset(size.width * 0.68f, size.height * 0.32f), Offset(size.width * 0.32f, size.height * 0.68f), strokeW, StrokeCap.Round)
                        }
                    }
                    if (!expanded && state.phase == "waiting_user") {
                        Box(
                            Modifier
                                .align(Alignment.BottomCenter)
                                .padding(bottom = 5.dp)
                                .clip(RoundedCornerShape(8.dp))
                                .background(Color(0xFFFF9F0A))
                                .padding(horizontal = 6.dp, vertical = 2.dp)
                        ) {
                            Text("点击拉大接管", color = Color.Black, fontSize = 9.sp, fontWeight = FontWeight.Bold)
                        }
                    }
                    if (expanded) {
                        ExpandedFloatingChromeComposable(
                            state = state,
                            inputBarOpen = inputBarOpen,
                            onToggleApps = {
                                pickingApp = !pickingApp
                                if (pickingApp) {
                                    inputBarOpen = false
                                    scope.launch(Dispatchers.IO) { runCatching { RootPhoneHarness.listLaunchableApps(context) } }
                                }
                            },
                            onPromoteFullscreen = {
                                pickingApp = false
                                inputBarOpen = false
                                expanded = false
                                hiddenToPill = true
                                scope.launch(Dispatchers.IO) {
                                    runCatching { RootPhoneHarness.command(context, JSONObject().put("op", "promote").put("manual", true)) }
                                }
                            },
                            onClose = { handleUserClose() },
                            onToggleInput = {
                                inputBarOpen = !inputBarOpen
                                if (inputBarOpen) pickingApp = false
                            },
                            onBackOrResume = {
                                if (state.phase == "waiting_user") {
                                    expanded = false
                                    inputBarOpen = false
                                    runCatching { RootPhoneHarness.resume(context) }
                                } else {
                                    scope.launch(Dispatchers.IO) {
                                        runCatching {
                                            RootPhoneHarness.command(context, JSONObject().put("op", "key").put("key", "back").put("manual", true))
                                        }
                                    }
                                }
                            }
                        )
                        if (inputBarOpen) {
                            InAppInlineImeInputBar(
                                onSend = { txt ->
                                    scope.launch(Dispatchers.IO) {
                                        runCatching {
                                            RootPhoneHarness.command(
                                                context,
                                                JSONObject().put("op", "text").put("text", txt).put("manual", true).put("skip_tree", true)
                                            )
                                        }
                                    }
                                },
                                onClose = { inputBarOpen = false }
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
internal fun ComposePipCircleButton(
    modifier: Modifier = Modifier,
    isDanger: Boolean = false,
    onClick: () -> Unit,
    drawIcon: androidx.compose.ui.graphics.drawscope.DrawScope.() -> Unit
) {
    Box(
        modifier
            .size(26.dp)
            .clip(CircleShape)
            .background(if (isDanger) Color(0xCC3B171B) else Color(0xA6141418))
            .border(
                0.8.dp,
                if (isDanger) Color(0x66FF6961) else Color(0x38FFFFFF),
                CircleShape
            )
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center
    ) {
        ComposeCanvas(Modifier.fillMaxSize(), onDraw = drawIcon)
    }
}
