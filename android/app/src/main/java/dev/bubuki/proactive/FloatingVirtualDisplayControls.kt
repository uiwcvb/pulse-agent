package dev.bubuki.proactive

import android.animation.ValueAnimator
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Typeface
import android.util.Base64
import android.view.MotionEvent
import android.view.VelocityTracker
import android.view.View
import android.view.ViewConfiguration
import android.view.animation.LinearInterpolator
import android.widget.ImageView
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
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
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
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.exp
import kotlin.math.hypot
import kotlin.math.pow
import kotlin.math.roundToInt
/**
 * [Module] FloatingVirtualDisplayControls
 * [Responsibility] Custom-drawn vector icon views (`PipVectorIconButton`, `ResizeGripView`),
 *                  critical-damped spring physics (`PipSpringPhysics`), adaptive layout metrics
 *                  model (`PipLayoutMetrics`), overlay touch/drag/resize gesture handlers, and
 *                  the Compose in-app PiP widget (`InAppFloatingVirtualDisplay`).
 * [Public API]
 *   - PipSpringPhysics: project / cornerPosition / nearestCorner / stepCriticalSpring
 *   - PipVectorIconButton / ResizeGripView / PipLayoutMetrics
 *   - FloatingVirtualDisplay.attachCornerResizeTouch / attachSpringDragAndTap / attachExpandedScreenTouchAndDrag
 *   - FloatingVirtualDisplay.animateSpringToNearestCorner / borderColorFor / dotColorFor / mainScreenAspect
 *   - InAppFloatingVirtualDisplay()
 */
internal object PipSpringPhysics {
    private const val DECEL_FACTOR = 0.992f / (1000f * (1f - 0.992f)) // ~0.124s
    private const val OMEGA = (2.0 * PI / 0.4).toFloat() // response = 0.4s
    enum class Corner(val right: Boolean, val bottom: Boolean) {
        TopStart(false, false),
        TopEnd(true, false),
        BottomStart(false, true),
        BottomEnd(true, true), }
    fun project(velocityPxPerSec: Float): Float = velocityPxPerSec * DECEL_FACTOR
    fun cornerPosition(
        corner: Corner,
        screenW: Int,
        screenH: Int,
        pipW: Int,
        pipH: Int,
        marginX: Int,
        topMargin: Int,
        bottomMargin: Int
    ): Pair<Int, Int> {
        val x = if (corner.right) (screenW - pipW - marginX).coerceAtLeast(marginX) else marginX
        val y = if (corner.bottom) (screenH - pipH - bottomMargin).coerceAtLeast(topMargin) else topMargin
        return x to y }
    fun nearestCorner(
        projectedX: Float,
        projectedY: Float,
        screenW: Int,
        screenH: Int,
        pipW: Int,
        pipH: Int,
        marginX: Int,
        topMargin: Int,
        bottomMargin: Int
    ): Corner = Corner.entries.minBy { c ->
        val (cx, cy) = cornerPosition(c, screenW, screenH, pipW, pipH, marginX, topMargin, bottomMargin)
        val dx = projectedX - cx
        val dy = projectedY - cy
        dx * dx + dy * dy }
    /** Analytical solution of critical-damped harmonic oscillator at time t (seconds). */
    fun stepCriticalSpring(start: Float, target: Float, initialVelocity: Float, tSec: Float): Float {
        val d0 = start - target
        val decay = exp((-OMEGA * tSec).toDouble()).toFloat()
        return target + (d0 + (initialVelocity + OMEGA * d0) * tSec) * decay }

    const val MIN_ACTIVE_WIDTH_FRACTION = 0.38f
    const val MIN_RESIZE_DRAG_FRACTION = 0.22f

    fun shouldDockPillOnResizeUp(widthFraction: Float): Boolean =
        widthFraction < MIN_ACTIVE_WIDTH_FRACTION

    /**
     * Returns -1 to dock bubble on left edge, +1 to dock on right edge, or 0 to snap back to nearest corner.
     */
    fun edgeDockSideOnDragUp(
        winX: Int,
        winW: Int,
        screenW: Int,
        vx: Float,
        edgeAllowancePx: Int
    ): Int {
        val leftOverflowThreshold = -(winW * 0.08f).roundToInt()
        val rightOverflowThreshold = screenW + (winW * 0.08f).roundToInt()
        val tightEdgePx = (edgeAllowancePx / 5).coerceAtLeast(8)
        if (winX <= leftOverflowThreshold || winX <= tightEdgePx || (vx < -950f && winX <= edgeAllowancePx)) {
            return -1
        }
        if (winX + winW >= rightOverflowThreshold || winX + winW >= screenW - tightEdgePx || (vx > 950f && winX + winW >= screenW - edgeAllowancePx)) {
            return 1
        }
        return 0
    }
}
internal class PipVectorIconButton(
    context: Context,
    private val kind: IconKind,
    private val isDanger: Boolean = false,
    private var badgeText: String = "M",
    onClick: () -> Unit
) : View(context) {
    enum class IconKind { EYE_OFF, SIZE_TIER, APPS_GRID, SHRINK, DOCK_PILL, POWER_CLOSE }
    private val bgPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = if (isDanger) 0xCC3B171B.toInt() else 0xA6141418.toInt() }
    private val strokePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = context.resources.displayMetrics.density * 0.8f
        color = if (isDanger) 0x66FF6961 else 0x38FFFFFF }
    private val iconPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
        strokeWidth = context.resources.displayMetrics.density * 1.5f
        color = if (isDanger) 0xFFFF7A72.toInt() else 0xF2FFFFFF.toInt() }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = 0xF2FFFFFF.toInt()
        textAlign = Paint.Align.CENTER
        typeface = Typeface.DEFAULT_BOLD }
    private val rectF = RectF()
    init {
        isClickable = true
        setOnClickListener { onClick() } }
    fun updateBadge(text: String) {
        if (badgeText != text) {
            badgeText = text
            invalidate() }
    }
    fun setBadgeText(text: String) = updateBadge(text)
    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        val minSide = minOf(w, h)
        if (minSide <= 2f) return
        val u = minSide / 26f
        strokePaint.strokeWidth = (0.85f * u).coerceAtLeast(1f)
        iconPaint.strokeWidth = (1.55f * u).coerceAtLeast(1.2f)
        val cx = w / 2f
        val cy = h / 2f
        val r = (minSide / 2f) - strokePaint.strokeWidth
        canvas.drawCircle(cx, cy, r, bgPaint)
        canvas.drawCircle(cx, cy, r, strokePaint)
        when (kind) {
            IconKind.EYE_OFF -> {
                rectF.set(cx - 5.5f * u, cy - 3.2f * u, cx + 5.5f * u, cy + 3.2f * u)
                canvas.drawOval(rectF, iconPaint)
                canvas.drawLine(cx - 5.2f * u, cy + 4.5f * u, cx + 5.2f * u, cy - 4.5f * u, iconPaint) }
            IconKind.SIZE_TIER -> {
                textPaint.textSize = 10.2f * u
                val fm = textPaint.fontMetrics
                val baseline = cy - (fm.ascent + fm.descent) / 2f
                canvas.drawText(badgeText, cx, baseline, textPaint) }
            IconKind.APPS_GRID -> {
                val s = 3.4f * u
                val gap = 1.2f * u
                val cr = 0.9f * u
                rectF.set(cx - gap / 2 - s, cy - gap / 2 - s, cx - gap / 2, cy - gap / 2)
                canvas.drawRoundRect(rectF, cr, cr, iconPaint)
                rectF.set(cx + gap / 2, cy - gap / 2 - s, cx + gap / 2 + s, cy - gap / 2)
                canvas.drawRoundRect(rectF, cr, cr, iconPaint)
                rectF.set(cx - gap / 2 - s, cy + gap / 2, cx - gap / 2, cy + gap / 2 + s)
                canvas.drawRoundRect(rectF, cr, cr, iconPaint)
                rectF.set(cx + gap / 2, cy + gap / 2, cx + gap / 2 + s, cy + gap / 2 + s)
                canvas.drawRoundRect(rectF, cr, cr, iconPaint) }
            IconKind.SHRINK -> {
                val arm = 3.2f * u
                canvas.drawLine(cx + 1.2f * u, cy - 1.2f * u, cx + 4.8f * u, cy - 4.8f * u, iconPaint)
                canvas.drawLine(cx + 1.2f * u, cy - 1.2f * u, cx + 1.2f * u + arm, cy - 1.2f * u, iconPaint)
                canvas.drawLine(cx + 1.2f * u, cy - 1.2f * u, cx + 1.2f * u, cy - 1.2f * u - arm, iconPaint)
                canvas.drawLine(cx - 1.2f * u, cy + 1.2f * u, cx - 4.8f * u, cy + 4.8f * u, iconPaint)
                canvas.drawLine(cx - 1.2f * u, cy + 1.2f * u, cx - 1.2f * u - arm, cy + 1.2f * u, iconPaint)
                canvas.drawLine(cx - 1.2f * u, cy + 1.2f * u, cx - 1.2f * u, cy + 1.2f * u + arm, iconPaint) }
            IconKind.DOCK_PILL -> {
                canvas.drawLine(cx - 4.0f * u, cy, cx + 4.0f * u, cy, iconPaint) }
            IconKind.POWER_CLOSE -> {
                val len = 3.8f * u
                canvas.drawLine(cx - len, cy - len, cx + len, cy + len, iconPaint)
                canvas.drawLine(cx + len, cy - len, cx - len, cy + len, iconPaint) }
        } }
}
internal class ResizeGripView(context: Context) : View(context) {
    private val boxFillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = 0x55101218
    }
    private val boxStrokePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
        color = 0xCCFFFFFF.toInt()
    }
    private val innerFillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = 0xAAFFFFFF.toInt()
    }
    private val boxRect = RectF()
    private val innerRect = RectF()
    init {
        contentDescription = "按住方框拖曳缩放，使劲向内缩小可挂起虚拟屏"
    }
    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        val u = minOf(w, h) / 22f
        val side = 11.5f * u
        val pad = 3.5f * u
        val cr = 3.0f * u
        boxStrokePaint.strokeWidth = (1.5f * u).coerceAtLeast(1.2f)
        boxRect.set(w - pad - side, h - pad - side, w - pad, h - pad)
        canvas.drawRoundRect(boxRect, cr, cr, boxFillPaint)
        canvas.drawRoundRect(boxRect, cr, cr, boxStrokePaint)
        val innerSide = 4.0f * u
        val inset = 2.4f * u
        innerRect.set(
            boxRect.right - inset - innerSide,
            boxRect.bottom - inset - innerSide,
            boxRect.right - inset,
            boxRect.bottom - inset
        )
        canvas.drawRoundRect(innerRect, 1.2f * u, 1.2f * u, innerFillPaint)
    }
}
internal data class PipLayoutMetrics(
    val winW: Int,
    val winH: Int,
    val innerW: Int,
    val innerH: Int,
    val framePadPx: Int,
    val outerRadiusPx: Float,
    val innerRadiusPx: Float,
    val btnSizePx: Int,
    val btnMarginPx: Int,
    val dragZoneHeightPx: Int,
    val touchDotSizePx: Int,
    val capsulePadHPx: Int,
    val capsulePadVPx: Int,
    val capsuleSp: Float,
    val hintSp: Float,
    val pickerItemSp: Float,
    val sigma: Float,
    val useSideRail: Boolean = false,
    val sideRailWidthPx: Int = 0
)
internal fun FloatingVirtualDisplay.mainScreenAspect(context: Context): Float {
    val m = context.resources.displayMetrics
    val w = minOf(m.widthPixels, m.heightPixels)
    val h = maxOf(m.widthPixels, m.heightPixels)
    return if (w > 0 && h > 0) h.toFloat() / w.toFloat() else 2.15f
}
internal fun FloatingVirtualDisplay.borderColorFor(state: RootPhoneState): Int =
    pearlOuterStrokeColorInt(state.phase)

internal fun FloatingVirtualDisplay.dotColorFor(state: RootPhoneState): Int =
    pearlStatusDotColorInt(state.phase)
@SuppressLint("ClickableViewAccessibility")
internal fun FloatingVirtualDisplay.attachCornerResizeTouch(context: Context, gripView: View) {
    val slop = ViewConfiguration.get(context).scaledTouchSlop
    var downRawX = 0f
    var downRawY = 0f
    var startInnerW = 0
    var startWinX = 0
    var startRightEdge = 0
    var resizing = false
    gripView.setOnTouchListener { _, event ->
        runCatching {
            val lp = params ?: return@setOnTouchListener false
            val container = rootView ?: return@setOnTouchListener false
            val wm = windowManager ?: return@setOnTouchListener false
            val metrics = context.resources.displayMetrics
            val shortSidePx = minOf(metrics.widthPixels, metrics.heightPixels).coerceAtLeast(1)
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    springAnimator?.cancel()
                    downRawX = event.rawX
                    downRawY = event.rawY
                    val curLayout = computeAdaptiveLayout(context, RootPhoneHarness.changes.value, expanded, minimizedToPill)
                    startInnerW = curLayout.innerW
                    startWinX = lp.x
                    startRightEdge = lp.x + lp.width
                    resizing = false }
                MotionEvent.ACTION_MOVE -> {
                    val dx = event.rawX - downRawX
                    val dy = event.rawY - downRawY
                    if (!resizing && hypot(dx, dy) > slop) {
                        resizing = true }
                    if (resizing) {
                        val state = RootPhoneHarness.changes.value
                        val bmp = currentBitmap
                        val rawAspect = if (bmp != null && bmp.width > 0 && bmp.height > 0) {
                            bmp.height.toFloat() / bmp.width.toFloat()
                        } else {
                            mainScreenAspect(context) }
                        val aspect = if (rawAspect.isFinite() && rawAspect > 0.2f) rawAspect else 2.15f
                        val dwFromY = dy / aspect
                        val deltaW = if (abs(dwFromY) >= abs(dx)) dwFromY else dx
                        val minDragInnerW = (shortSidePx * PipSpringPhysics.MIN_RESIZE_DRAG_FRACTION).roundToInt().coerceAtLeast(dp(context, 72))
                        val maxInnerW = (shortSidePx * 0.84f).roundToInt().coerceAtLeast(minDragInnerW + 1)
                        val newInnerW = (startInnerW + deltaW.roundToInt()).coerceIn(minDragInnerW, maxInnerW)
                        customWidthFraction = (newInnerW.toFloat() / shortSidePx.toFloat()).coerceIn(PipSpringPhysics.MIN_RESIZE_DRAG_FRACTION, 0.84f)
                        val (targetW, targetH) = computeWindowSizePx(context, state, expanded, minimizedToPill)
                        if (abs(lp.width - targetW) > 1 || abs(lp.height - targetH) > 1) {
                            lp.width = targetW
                            lp.height = targetH
                            val desiredX = if (snapCorner.right) (startRightEdge - targetW) else startWinX
                            val minX = dp(context, 6)
                            val maxX = (metrics.widthPixels - lp.width - minX).coerceAtLeast(minX)
                            val minY = dp(context, 36)
                            val maxY = (metrics.heightPixels - lp.height - minY).coerceAtLeast(minY)
                            lp.x = desiredX.coerceIn(minX, maxX)
                            lp.y = lp.y.coerceIn(minY, maxY)
                            wm.updateViewLayout(container, lp) }
                    } }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    val wasResizing = resizing
                    resizing = false
                    val frac = customWidthFraction
                    if (wasResizing && frac != null && PipSpringPhysics.shouldDockPillOnResizeUp(frac)) {
                        runCatching { gripView.performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS) }
                        enterMiniSuspend(context)
                    } }
            } }
        true }
}
@SuppressLint("ClickableViewAccessibility")
internal fun FloatingVirtualDisplay.attachSpringDragAndTap(
    context: Context,
    handleView: View,
    onTap: (() -> Unit)?,
    dockOnEdge: Boolean = false
) {
    val slop = ViewConfiguration.get(context).scaledTouchSlop
    var downRawX = 0f
    var downRawY = 0f
    var startWinX = 0
    var startWinY = 0
    var dragging = false
    var tracker: VelocityTracker? = null
    handleView.setOnTouchListener { _, event ->
        val lp = params ?: return@setOnTouchListener false
        val container = rootView ?: return@setOnTouchListener false
        val wm = windowManager ?: return@setOnTouchListener false
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                springAnimator?.cancel()
                tracker?.recycle()
                tracker = VelocityTracker.obtain().also { it.addMovement(event) }
                downRawX = event.rawX
                downRawY = event.rawY
                startWinX = lp.x
                startWinY = lp.y
                dragging = false
                true }
            MotionEvent.ACTION_MOVE -> {
                tracker?.addMovement(event)
                val dx = event.rawX - downRawX
                val dy = event.rawY - downRawY
                if (!dragging && hypot(dx, dy) > slop) {
                    dragging = true }
                if (dragging) {
                    val metrics = context.resources.displayMetrics
                    val minX = if (dockOnEdge) -(lp.width * 0.42f).roundToInt() else dp(context, 6)
                    val maxX = if (dockOnEdge) {
                        (metrics.widthPixels - (lp.width * 0.58f).roundToInt()).coerceAtLeast(minX)
                    } else {
                        (metrics.widthPixels - lp.width - dp(context, 6)).coerceAtLeast(dp(context, 6))
                    }
                    lp.x = (startWinX + dx.roundToInt()).coerceIn(minX, maxX)
                    lp.y = (startWinY + dy.roundToInt()).coerceIn(
                        dp(context, 36),
                        (metrics.heightPixels - lp.height - dp(context, 36)).coerceAtLeast(dp(context, 36)))
                    runCatching { wm.updateViewLayout(container, lp) } }
                true }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                tracker?.addMovement(event)
                tracker?.computeCurrentVelocity(1000)
                val vx = tracker?.xVelocity ?: 0f
                val vy = tracker?.yVelocity ?: 0f
                tracker?.recycle()
                tracker = null
                if (dragging) {
                    val metrics = context.resources.displayMetrics
                    val dockSide = if (dockOnEdge) {
                        PipSpringPhysics.edgeDockSideOnDragUp(lp.x, lp.width, metrics.widthPixels, vx, dp(context, 40))
                    } else 0
                    if (dockSide != 0) {
                        runCatching { handleView.performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS) }
                        dockToEdgeBubble(context, dockLeft = dockSide < 0)
                    } else {
                        animateSpringToNearestCorner(context, vx, vy)
                    }
                } else if (event.actionMasked == MotionEvent.ACTION_UP) {
                    mainHandler.post { onTap?.invoke() } }
                dragging = false
                true }
            else -> false }
    }
}
@SuppressLint("ClickableViewAccessibility")
internal fun FloatingVirtualDisplay.attachExpandedScreenTouchAndDrag(
    context: Context,
    imageView: ImageView,
    dragZonePx: Int = dp(context, 38)
) {
    val slop = ViewConfiguration.get(context).scaledTouchSlop
    var downX = 0f
    var downY = 0f
    var downRawX = 0f
    var downRawY = 0f
    var startWinX = 0
    var startWinY = 0
    var windowDragMode = false
    var draggingWindow = false
    var liveTouchStarted = false
    var lastMoveSentAt = 0L
    var lastSentX = -1
    var lastSentY = -1
    var tracker: VelocityTracker? = null
    imageView.setOnTouchListener { v, event ->
        val lp = params ?: return@setOnTouchListener false
        val container = rootView ?: return@setOnTouchListener false
        val wm = windowManager ?: return@setOnTouchListener false
        val state = RootPhoneHarness.changes.value
        val vw = v.width.coerceAtLeast(1)
        val vh = v.height.coerceAtLeast(1)
        val info = runCatching { JSONObject(state.imageInfo.ifBlank { state.display }) }.getOrDefault(JSONObject())
        val dispW = info.optInt("width", 1080).coerceAtLeast(1)
        val dispH = info.optInt("height", 2400).coerceAtLeast(1)
        fun mapX(px: Float) = (px / vw * dispW).toInt().coerceIn(0, dispW - 1)
        fun mapY(py: Float) = (py / vh * dispH).toInt().coerceIn(0, dispH - 1)
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                springAnimator?.cancel()
                downX = event.x
                downY = event.y
                downRawX = event.rawX
                downRawY = event.rawY
                startWinX = lp.x
                startWinY = lp.y
                windowDragMode = dragZonePx > 0 && downY <= dragZonePx
                draggingWindow = false
                liveTouchStarted = false
                if (windowDragMode) {
                    tracker?.recycle()
                    tracker = VelocityTracker.obtain().also { it.addMovement(event) }
                } else {
                    val tx = mapX(downX)
                    val ty = mapY(downY)
                    liveTouchStarted = true
                    lastSentX = tx
                    lastSentY = ty
                    lastMoveSentAt = android.os.SystemClock.uptimeMillis()
                    RootPhoneHarness.sendLiveTouch(context, "down", tx, ty) }
                true }
            MotionEvent.ACTION_MOVE -> {
                if (windowDragMode) {
                    tracker?.addMovement(event)
                    val dx = event.rawX - downRawX
                    val dy = event.rawY - downRawY
                    if (!draggingWindow && hypot(dx, dy) > slop) {
                        draggingWindow = true }
                    if (draggingWindow) {
                        val metrics = context.resources.displayMetrics
                        lp.x = (startWinX + dx.roundToInt()).coerceIn(
                            dp(context, 6),
                            (metrics.widthPixels - lp.width - dp(context, 6)).coerceAtLeast(dp(context, 6)))
                        lp.y = (startWinY + dy.roundToInt()).coerceIn(
                            dp(context, 36),
                            (metrics.heightPixels - lp.height - dp(context, 36)).coerceAtLeast(dp(context, 36)))
                        runCatching { wm.updateViewLayout(container, lp) } }
                } else if (liveTouchStarted) {
                    val now = android.os.SystemClock.uptimeMillis()
                    val tx = mapX(event.x)
                    val ty = mapY(event.y)
                    if (now - lastMoveSentAt >= 10L || abs(tx - lastSentX) + abs(ty - lastSentY) >= 2) {
                        lastMoveSentAt = now
                        lastSentX = tx
                        lastSentY = ty
                        RootPhoneHarness.sendLiveTouch(context, "move", tx, ty) }
                }
                true }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                if (windowDragMode) {
                    tracker?.addMovement(event)
                    tracker?.computeCurrentVelocity(1000)
                    val vx = tracker?.xVelocity ?: 0f
                    val vy = tracker?.yVelocity ?: 0f
                    tracker?.recycle()
                    tracker = null
                    if (draggingWindow) {
                        animateSpringToNearestCorner(context, vx, vy) }
                    draggingWindow = false
                    return@setOnTouchListener true }
                if (liveTouchStarted) {
                    liveTouchStarted = false
                    val tx = mapX(event.x)
                    val ty = mapY(event.y)
                    val act = if (event.actionMasked == MotionEvent.ACTION_CANCEL) "cancel" else "up"
                    RootPhoneHarness.sendLiveTouch(context, act, tx, ty) }
                true }
            else -> true }
    }
}
internal fun FloatingVirtualDisplay.animateSpringToNearestCorner(context: Context, vx: Float, vy: Float) {
    val lp = params ?: return
    val container = rootView ?: return
    val wm = windowManager ?: return
    val metrics = context.resources.displayMetrics
    val hostLandscape = metrics.widthPixels > metrics.heightPixels
    val marginX = dp(context, 12)
    val topMargin = dp(context, if (hostLandscape) 14 else 56)
    val bottomMargin = dp(context, if (hostLandscape) 14 else 92)
    val projX = lp.x + PipSpringPhysics.project(vx)
    val projY = lp.y + PipSpringPhysics.project(vy)
    snapCorner = PipSpringPhysics.nearestCorner(
        projX, projY,
        metrics.widthPixels, metrics.heightPixels,
        lp.width, lp.height,
        marginX, topMargin, bottomMargin)
    val (targetX, targetY) = PipSpringPhysics.cornerPosition(
        snapCorner,
        metrics.widthPixels,
        metrics.heightPixels,
        lp.width, lp.height,
        marginX, topMargin, bottomMargin)
    val startX = lp.x.toFloat()
    val startY = lp.y.toFloat()
    if (abs(startX - targetX) < 1f && abs(startY - targetY) < 1f) return
    springAnimator?.cancel()
    springAnimator = ValueAnimator.ofFloat(0f, 0.42f).apply {
        duration = 420L
        interpolator = LinearInterpolator()
        addUpdateListener { anim ->
            val tSec = anim.animatedValue as Float
            lp.x = PipSpringPhysics.stepCriticalSpring(startX, targetX.toFloat(), vx, tSec).roundToInt()
            lp.y = PipSpringPhysics.stepCriticalSpring(startY, targetY.toFloat(), vy, tSec).roundToInt()
            if (rootView === container) {
                runCatching { wm.updateViewLayout(container, lp) } }
        }
        start() }
}

internal fun FloatingVirtualDisplay.attachEdgeBubbleDragAndTap(
    context: Context,
    bubbleView: View,
    onTap: () -> Unit
) {
    val slop = ViewConfiguration.get(context).scaledTouchSlop
    var downRawX = 0f
    var downRawY = 0f
    var startWinX = 0
    var startWinY = 0
    var dragging = false
    bubbleView.setOnTouchListener { _, event ->
        val lp = params ?: return@setOnTouchListener false
        val container = rootView ?: return@setOnTouchListener false
        val wm = windowManager ?: return@setOnTouchListener false
        val metrics = context.resources.displayMetrics
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                springAnimator?.cancel()
                downRawX = event.rawX
                downRawY = event.rawY
                startWinX = lp.x
                startWinY = lp.y
                dragging = false
                true
            }
            MotionEvent.ACTION_MOVE -> {
                val dx = event.rawX - downRawX
                val dy = event.rawY - downRawY
                if (!dragging && hypot(dx, dy) > slop) dragging = true
                if (dragging) {
                    val minX = dp(context, 4)
                    val maxX = (metrics.widthPixels - lp.width - minX).coerceAtLeast(minX)
                    val minY = dp(context, 48)
                    val maxY = (metrics.heightPixels - lp.height - dp(context, 84)).coerceAtLeast(minY)
                    lp.x = (startWinX + dx.roundToInt()).coerceIn(minX, maxX)
                    lp.y = (startWinY + dy.roundToInt()).coerceIn(minY, maxY)
                    runCatching { wm.updateViewLayout(container, lp) }
                }
                true
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                if (dragging) {
                    val centerX = lp.x + lp.width / 2f
                    dockPillOnLeft = centerX < (metrics.widthPixels / 2f)
                    edgeBubbleY = lp.y
                    val targetX = if (dockPillOnLeft) dp(context, 4) else (metrics.widthPixels - lp.width - dp(context, 4)).coerceAtLeast(dp(context, 4))
                    val startX = lp.x.toFloat()
                    springAnimator?.cancel()
                    springAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
                        duration = 200L
                        interpolator = android.view.animation.PathInterpolator(0.22f, 1.0f, 0.32f, 1.0f)
                        addUpdateListener { anim ->
                            val t = anim.animatedValue as Float
                            lp.x = (startX + (targetX - startX) * t).roundToInt()
                            if (rootView === container) runCatching { wm.updateViewLayout(container, lp) }
                        }
                        start()
                    }
                } else if (event.actionMasked == MotionEvent.ACTION_UP) {
                    mainHandler.post { onTap() }
                }
                dragging = false
                true
            }
            else -> false
        }
    }
}

internal fun FloatingVirtualDisplay.computeAdaptiveLayoutImpl(
    context: Context,
    state: RootPhoneState,
    isExpanded: Boolean,
    isPill: Boolean
): PipLayoutMetrics {
    if (dockedAsEdgeBubble || isPill) {
        return PipLayoutMetrics(
            winW = dp(context, 56),
            winH = dp(context, 56),
            innerW = dp(context, 48),
            innerH = dp(context, 48),
            framePadPx = dp(context, 1),
            outerRadiusPx = dp(context, 24).toFloat(),
            innerRadiusPx = dp(context, 23).toFloat(),
            btnSizePx = dp(context, 18),
            btnMarginPx = dp(context, 4),
            dragZoneHeightPx = dp(context, 24),
            touchDotSizePx = dp(context, 8),
            capsulePadHPx = dp(context, 6),
            capsulePadVPx = dp(context, 2),
            capsuleSp = 9f,
            hintSp = 9f,
            pickerItemSp = 11f,
            sigma = 0.75f
        )
    }
    val bmp = currentBitmap
    val rawAspect = if (bmp != null && bmp.width > 0 && bmp.height > 0) {
        bmp.height.toFloat() / bmp.width.toFloat()
    } else {
        val info = runCatching { JSONObject(state.imageInfo.ifBlank { state.display }) }.getOrDefault(JSONObject())
        val vw = info.optInt("width", 0)
        val vh = info.optInt("height", 0)
        if (vw > 0 && vh > 0) vh.toFloat() / vw.toFloat() else mainScreenAspect(context)
    }
    val aspect = rawAspect.takeIf { it.isFinite() && it > 0.1f } ?: 2.15f
    val metrics = context.resources.displayMetrics
    val hostLandscape = metrics.widthPixels > metrics.heightPixels
    val shortSidePx = minOf(metrics.widthPixels, metrics.heightPixels).coerceAtLeast(dp(context, 280))
    val isLandscape = aspect < 1f
    val frac = when {
        !isExpanded -> if (isLandscape) 0.44f else 0.27f
        customWidthFraction != null -> customWidthFraction!!
        else -> if (isLandscape) currentTier().landscapeFrac else currentTier().portraitFrac
    }
    val prelimInnerW = (shortSidePx * frac).roundToInt()
    val prelimInnerDp = prelimInnerW / metrics.density.coerceAtLeast(1f)
    val pad = if (isExpanded) dp(context, 4.5f).coerceIn(dp(context, 3), dp(context, 6)) else dp(context, 3.5f).coerceAtLeast(dp(context, 3))
    val verticalMarginTotal = if (hostLandscape) dp(context, 24) else dp(context, 116)
    val availW = (metrics.widthPixels - dp(context, 20) - pad * 2).coerceAtLeast(dp(context, 80))
    val availH = (metrics.heightPixels - verticalMarginTotal - pad * 2).coerceAtLeast(dp(context, 160))
    val innerW = minOf(
        prelimInnerW,
        availW,
        (availH / aspect).roundToInt().coerceAtLeast(dp(context, 84))
    ).coerceAtLeast(dp(context, 84))
    val innerH = (innerW * aspect).roundToInt().coerceAtLeast(dp(context, 84))
    val innerDp = innerW / metrics.density.coerceAtLeast(1f)
    val sigma = (innerDp / 240f).pow(0.65f).coerceIn(0.66f, 1.35f)
    val outerRadius = dp(context, if (isExpanded) 22f else 17f).toFloat()
    val innerRadius = (outerRadius - pad).coerceAtLeast(dp(context, 10).toFloat())
    val btnFloor = dp(context, 18)
    val maxBtnByWidth = ((innerW - dp(context, 24)) / 5).coerceIn(btnFloor, dp(context, 34))
    val btnSize = dp(context, 25f * sigma).coerceIn(btnFloor, maxBtnByWidth.coerceAtLeast(btnFloor))
    val btnMargin = dp(context, 6f * sigma).coerceIn(dp(context, 3), dp(context, 9))
    val dotSize = dp(context, 13f * sigma).coerceIn(dp(context, 10), dp(context, 18))
    return PipLayoutMetrics(
        winW = innerW + pad * 2,
        winH = innerH + pad * 2,
        innerW = innerW,
        innerH = innerH,
        framePadPx = pad,
        outerRadiusPx = outerRadius,
        innerRadiusPx = innerRadius,
        btnSizePx = btnSize,
        btnMarginPx = btnMargin,
        dragZoneHeightPx = dp(context, 24),
        touchDotSizePx = dotSize,
        capsulePadHPx = dp(context, 10.5f * sigma).coerceIn(dp(context, 7), dp(context, 14)),
        capsulePadVPx = dp(context, 4f * sigma).coerceIn(dp(context, 2), dp(context, 6)),
        capsuleSp = (10f * sigma).coerceIn(8.5f, 12.5f),
        hintSp = (10f * sigma).coerceIn(8.5f, 12f),
        pickerItemSp = (11.5f * sigma).coerceIn(10f, 14f),
        sigma = sigma,
        useSideRail = false,
        sideRailWidthPx = 0
    )
}
