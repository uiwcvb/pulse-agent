package dev.bubuki.proactive

import android.animation.ValueAnimator
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.LayerDrawable
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Base64
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.pow
import kotlin.math.roundToInt

/**
 * [Module] FloatingVirtualDisplay
 * [Responsibility] System-wide WindowManager overlay lifecycle (`TYPE_APPLICATION_OVERLAY`),
 *                  three-tier adaptive layout calculation (`computeAdaptiveLayout`),
 *                  view hierarchy construction (`rebuildViewHierarchy`), and live bitmap
 *                  frame decoding (`updateWindowContent`).
 *                  Delegates vector icon controls, spring physics, touch/drag gesture handlers,
 *                  and the in-app Compose PiP fallback to FloatingVirtualDisplayControls.kt.
 * [Public API]
 *   - FloatingVirtualDisplay.overlayVisible / userDismissedFlow
 *   - canOverlay(context) / requestOverlayPermission(context)
 *   - show(context, forceExpand) / hide() / enterMiniSuspend(context) / minimizeToSidePill(context)
 *   - cycleSizeTier(context) / currentTier() / isPreviewVisible()
 *   - onWorkspaceStateChanged(context, state) / computeAdaptiveLayout(...)
 */
object FloatingVirtualDisplay {
    enum class SizeTier(val rpcName: String, val badge: String, val portraitFrac: Float, val landscapeFrac: Float) {
        COMPACT("compact", "S", 0.44f, 0.54f),
        MEDIUM("balanced", "M", 0.58f, 0.70f),
        LARGE("large", "L", 0.72f, 0.82f);
        fun next(): SizeTier = when (this) {
            COMPACT -> MEDIUM
            MEDIUM -> LARGE
            LARGE -> COMPACT }
    }
    internal val mainHandler = Handler(Looper.getMainLooper())
    internal val overlayScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var watchJob: Job? = null
    internal var windowManager: WindowManager? = null
    internal var rootView: FrameLayout? = null
    internal var params: WindowManager.LayoutParams? = null
    private val stateLock = Any()
    // Single source of truth for floating window state (legacy booleans are read-only projections).
    @Volatile private var uiState: OverlayUiState = OverlayUiState()
    internal val expanded: Boolean get() = uiState.expanded
    internal val fullStage: Boolean get() = false
    internal val minimizedToPill: Boolean get() = uiState.dockedAsEdgeBubble
    internal val dockedAsEdgeBubble: Boolean get() = uiState.dockedAsEdgeBubble
    internal val uiPickingApp: Boolean get() = uiState.pickingApp
    internal val uiInputBarOpen: Boolean get() = uiState.inputBarOpen
    private val pickingApp: Boolean get() = uiState.pickingApp
    private val inputBarOpen: Boolean get() = uiState.inputBarOpen
    private val userDismissed: Boolean get() = uiState.userDismissed
    private val overlayMode: OverlayMode get() = uiState.mode
    private val drawerMode: OverlayDrawer get() = uiState.drawer
    internal var edgeBubbleY = -1
    @Volatile internal var drawerSearchFocused: Boolean = false
    private var sizeTier = SizeTier.COMPACT
    internal var customWidthFraction: Float? = null
    internal var snapCorner = PipSpringPhysics.Corner.TopEnd
    internal var springAnimator: ValueAnimator? = null
    private val _overlayModeFlow = MutableStateFlow(OverlayMode.HIDDEN)
    internal val overlayModeFlow: StateFlow<OverlayMode> = _overlayModeFlow.asStateFlow()
    internal fun transitionState(
        mode: OverlayMode,
        drawer: OverlayDrawer = OverlayDrawer.NONE,
        userClosed: Boolean? = null,
    ) {
        val next = synchronized(stateLock) {
            val resolvedClosed = userClosed ?: uiState.userDismissed
            reduceOverlayTransition(uiState, mode, drawer, resolvedClosed).also { uiState = it }
        }
        if (next.drawer != OverlayDrawer.APP_PICKER) {
            drawerSearchFocused = false
            if (Looper.myLooper() == Looper.getMainLooper()) {
                appDrawerController?.clearSearchFocus()
            } else {
                mainHandler.post { appDrawerController?.clearSearchFocus() }
            }
        }
        _userDismissed.value = next.userDismissed
        _overlayModeFlow.value = next.mode
    }
    internal fun currentOverlayMode(): OverlayMode = uiState.mode
    internal fun promoteTierOnWaitingExpandIfCompact(phase: String) {
        if (phase == "waiting_user" && sizeTier == SizeTier.COMPACT) {
            sizeTier = SizeTier.MEDIUM
            customWidthFraction = null
        }
    }
    internal var screenImageView: ImageView? = null
    internal var touchIndicatorView: View? = null
    internal var emptyHintView: TextView? = null
    internal var waitingCapsuleView: TextView? = null
    internal var inlineInputBar: View? = null
    internal var inlineInputField: EditText? = null
    internal var bottomLeftRow: View? = null
    internal var appDrawerController: VirtualDisplayAppDrawer.DrawerController? = null
    private var sizeTierButton: PipVectorIconButton? = null
    private var sizeTierBadgeView: TextView? = null
    internal var outerBorderDrawable: GradientDrawable? = null
    internal var lastBorderColor: Int = 0
    internal var lastRenderedAppsKey: Int = -1
    private var lastTouchShownTime: Long = 0L
    internal fun clearChildViewRefs() {
        screenImageView = null
        touchIndicatorView = null
        emptyHintView = null
        waitingCapsuleView = null
        inlineInputBar = null
        inlineInputField = null
        bottomLeftRow = null
        appDrawerController = null
        sizeTierButton = null
        sizeTierBadgeView = null
    }
    @JvmStatic
    fun currentTier(): SizeTier = sizeTier
    @Volatile private var inAppPreviewVisible: Boolean = false
    internal fun setInAppPreviewVisible(visible: Boolean, context: Context) {
        val changed = inAppPreviewVisible != visible
        inAppPreviewVisible = visible
        if (changed) RootPhoneHarness.onPreviewVisibilityChanged(context.applicationContext)
    }
    @JvmStatic
    fun isPreviewVisible(): Boolean = rootView != null && !minimizedToPill && !dockedAsEdgeBubble && !userDismissed
    @JvmStatic
    fun isAnyPreviewShowing(): Boolean = isPreviewVisible() || inAppPreviewVisible
    @JvmStatic
    fun cycleSizeTier(context: Context) {
        sizeTier = sizeTier.next()
        customWidthFraction = null
        mainHandler.post {
            sizeTierButton?.setBadgeText(sizeTier.badge)
            sizeTierBadgeView?.text = sizeTier.badge
            transitionSizeAndRebuild(context.applicationContext, RootPhoneHarness.changes.value) }
        overlayScope.launch(Dispatchers.IO) {
            runCatching {
                RootPhoneHarness.command(
                    context.applicationContext,
                    JSONObject().put("op", "resize").put("tier", sizeTier.rpcName).put("manual", true)) }
        } }
    // Frame decoding cache
    @Volatile private var lastDecodedB64: String = ""
    @Volatile internal var currentBitmap: Bitmap? = null
    @Volatile private var isDecodingFrame: Boolean = false
    @Volatile private var pendingFrameB64: String? = null
    @Volatile internal var dockPillOnLeft: Boolean = false
    private val _overlayVisible = MutableStateFlow(false)
    val overlayVisible: StateFlow<Boolean> = _overlayVisible.asStateFlow()
    private val _userDismissed = MutableStateFlow(false)
    val userDismissedFlow: StateFlow<Boolean> = _userDismissed.asStateFlow()
    internal fun cachedBitmap(): Bitmap? = currentBitmap
    internal fun dp(context: Context, value: Float): Int =
        (value * context.resources.displayMetrics.density).roundToInt()
    internal fun dp(context: Context, value: Int): Int =
        (value * context.resources.displayMetrics.density).roundToInt()
    @JvmStatic
    fun canOverlay(context: Context): Boolean = Settings.canDrawOverlays(context)
    @JvmStatic
    fun requestOverlayPermission(context: Context) {
        runCatching {
            val intent = Intent(
                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:${context.packageName}")
            ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(intent)
            Toast.makeText(context, "请开启「显示在其他应用上层」，虚拟屏小窗即可悬浮在桌面上", Toast.LENGTH_LONG).show() }
    }
    @Volatile private var dismissedUntilElapsed = 0L
    @JvmStatic
    fun markUserDismissedFor(durationMs: Long) {
        dismissedUntilElapsed = android.os.SystemClock.elapsedRealtime() + durationMs.coerceAtLeast(0L)
        synchronized(stateLock) {
            uiState = uiState.copy(userDismissed = true)
        }
        _userDismissed.value = true
    }
    @JvmStatic
    fun resetUserDismissed() {
        synchronized(stateLock) {
            uiState = uiState.copy(userDismissed = false)
        }
        _userDismissed.value = false
        dismissedUntilElapsed = 0L
    }
    @JvmStatic
    fun show(context: Context, forceExpand: Boolean = false) {
        val appCtx = context.applicationContext
        resetUserDismissed()
        mainHandler.post {
            val nextMode = when {
                forceExpand -> OverlayMode.EXPANDED
                rootView == null || overlayMode == OverlayMode.HIDDEN || overlayMode == OverlayMode.EDGE_BUBBLE -> OverlayMode.MINI
                else -> overlayMode
            }
            transitionState(nextMode, if (nextMode == OverlayMode.EXPANDED) drawerMode else OverlayDrawer.NONE, userClosed = false)
            ensureWatching(appCtx)
            if (canOverlay(appCtx)) {
                mountOrUpdateWindow(appCtx, RootPhoneHarness.changes.value)
                RootPhoneHarness.onPreviewVisibilityChanged(appCtx) }
        } }
    @JvmStatic
    fun showAuto(context: Context) {
        val appCtx = context.applicationContext
        if (userDismissed && android.os.SystemClock.elapsedRealtime() < dismissedUntilElapsed) return
        if (userDismissed) resetUserDismissed()
        mainHandler.post {
            if (rootView == null && overlayMode == OverlayMode.HIDDEN) {
                transitionState(OverlayMode.MINI, OverlayDrawer.NONE, userClosed = false)
            }
            ensureWatching(appCtx)
            if (canOverlay(appCtx)) {
                mountOrUpdateWindow(appCtx, RootPhoneHarness.changes.value)
                RootPhoneHarness.onPreviewVisibilityChanged(appCtx) }
        } }
    @JvmStatic
    fun promoteToSystemOverlayIfPermitted(context: Context) {
        val appCtx = context.applicationContext
        val st = RootPhoneHarness.changes.value
        if (!st.displayCreated || userDismissed || !canOverlay(appCtx)) return
        mainHandler.post {
            if (rootView == null && !userDismissed && canOverlay(appCtx)) {
                if (overlayMode == OverlayMode.HIDDEN) {
                    transitionState(OverlayMode.MINI, OverlayDrawer.NONE, userClosed = false)
                }
                ensureWatching(appCtx)
                mountOrUpdateWindow(appCtx, RootPhoneHarness.changes.value)
                RootPhoneHarness.onPreviewVisibilityChanged(appCtx)
            }
        }
    }
    internal fun handleUserPowerClose(context: Context) {
        val appCtx = context.applicationContext
        markUserDismissedFor(60_000L)
        hide()
        AgentForegroundService.onUserForceClosedVirtualDisplay(appCtx)
        overlayScope.launch(Dispatchers.IO) {
            runCatching { RootPhoneHarness.command(appCtx, JSONObject().put("op", "release").put("user_close", true).put("destroy", true)) }
        }
    }
    @JvmStatic
    fun hide() {
        val next = synchronized(stateLock) {
            reduceOverlayTransition(uiState, OverlayMode.HIDDEN, OverlayDrawer.NONE, userClosed = true).also {
                uiState = it
            }
        }
        drawerSearchFocused = false
        _userDismissed.value = next.userDismissed
        _overlayModeFlow.value = next.mode
        mainHandler.post {
            appDrawerController?.clearSearchFocus()
            removeWindow()
        } }
    @JvmStatic
    fun enterMiniSuspend(context: Context) {
        val appCtx = context.applicationContext
        resetUserDismissed()
        mainHandler.post {
            customWidthFraction = null
            if (rootView == null) {
                transitionState(OverlayMode.MINI, OverlayDrawer.NONE, userClosed = false)
                if (canOverlay(appCtx)) {
                    ensureWatching(appCtx)
                    mountOrUpdateWindow(appCtx, RootPhoneHarness.changes.value)
                }
                return@post
            }
            val metrics = appCtx.resources.displayMetrics
            val hostLandscape = metrics.widthPixels > metrics.heightPixels
            params?.let { lp ->
                snapCorner = PipSpringPhysics.nearestCorner(
                    projectedX = lp.x.toFloat(),
                    projectedY = lp.y.toFloat(),
                    screenW = metrics.widthPixels,
                    screenH = metrics.heightPixels,
                    pipW = lp.width.coerceAtLeast(1),
                    pipH = lp.height.coerceAtLeast(1),
                    marginX = dp(appCtx, 12),
                    topMargin = dp(appCtx, if (hostLandscape) 14 else 56),
                    bottomMargin = dp(appCtx, if (hostLandscape) 14 else 92)
                )
            }
            transitionState(OverlayMode.MINI, OverlayDrawer.NONE, userClosed = false)
            transitionSizeAndRebuild(appCtx, RootPhoneHarness.changes.value)
            RootPhoneHarness.onPreviewVisibilityChanged(appCtx)
        } }
    @JvmStatic @JvmOverloads
    fun minimizeToSidePill(context: Context, dockLeft: Boolean? = null) {
        dockToEdgeBubble(context, dockLeft)
    }
    @JvmStatic @JvmOverloads
    fun dockToEdgeBubble(context: Context, dockLeft: Boolean? = null) {
        val appCtx = context.applicationContext
        if (dockLeft != null) {
            dockPillOnLeft = dockLeft
        }
        mainHandler.post {
            if (rootView == null) return@post
            params?.let { edgeBubbleY = it.y }
            transitionState(OverlayMode.EDGE_BUBBLE, OverlayDrawer.NONE, userClosed = uiState.userDismissed)
            RootPhoneHarness.onPreviewVisibilityChanged(appCtx)
            if (!userDismissed && canOverlay(appCtx) && rootView != null) {
                transitionSizeAndRebuild(appCtx, RootPhoneHarness.changes.value)
            }
        } }
    @JvmStatic fun syncKeepScreenOn(context: Context) {
        val appCtx = context.applicationContext
        mainHandler.post {
            val container = rootView ?: return@post; val lp = params ?: return@post
            val wm = windowManager ?: appCtx.getSystemService(WindowManager::class.java).also { windowManager = it } ?: return@post
            val want = RootPhoneHarness.shouldKeepScreenOn(expanded)
            val newFlags = if (want) lp.flags or WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON else lp.flags and WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON.inv()
            if (newFlags != lp.flags) { lp.flags = newFlags; runCatching { wm.updateViewLayout(container, lp) } }
        }
    }
    internal fun onWorkspaceStateChanged(context: Context, state: RootPhoneState) {
        val appCtx = context.applicationContext
        mainHandler.post {
            ensureWatching(appCtx)
            val dispObj = runCatching { JSONObject(state.display) }.getOrNull()
            val created = dispObj?.optBoolean("created") == true
            val explicitlyDestroyed = shouldAutoCloseVirtualDisplay(state.phase, state.display)
            if (explicitlyDestroyed) {
                val keepDismissed = android.os.SystemClock.elapsedRealtime() < dismissedUntilElapsed
                transitionState(OverlayMode.HIDDEN, OverlayDrawer.NONE, userClosed = keepDismissed)
                removeWindow()
                return@post }
            if ((created || rootView != null) && !userDismissed && canOverlay(appCtx)) {
                if (overlayMode == OverlayMode.HIDDEN) {
                    transitionState(OverlayMode.MINI, OverlayDrawer.NONE, userClosed = false)
                }
                mountOrUpdateWindow(appCtx, state) }
        } }
    private fun ensureWatching(context: Context) {
        if (watchJob?.isActive == true) return
        watchJob = overlayScope.launch {
            RootPhoneHarness.changes.collect { state ->
                val dispObj = runCatching { JSONObject(state.display) }.getOrNull()
                val created = dispObj?.optBoolean("created") == true
                val shouldClose = shouldAutoCloseVirtualDisplay(state.phase, state.display)
                if (shouldClose) {
                    transitionState(OverlayMode.HIDDEN, OverlayDrawer.NONE, userClosed = uiState.userDismissed)
                    removeWindow()
                } else if ((created || rootView != null) && !userDismissed && canOverlay(context)) {
                    if (overlayMode == OverlayMode.HIDDEN) {
                        transitionState(OverlayMode.MINI, OverlayDrawer.NONE, userClosed = false)
                    }
                    mountOrUpdateWindow(context, state) }
            } }
    }
    private fun removeWindow() {
        watchJob?.cancel()
        watchJob = null
        springAnimator?.cancel()
        springAnimator = null
        val view = rootView
        val wm = windowManager
        rootView = null
        params = null
        clearChildViewRefs()
        outerBorderDrawable = null
        currentBitmap = null
        lastDecodedB64 = ""
        pendingFrameB64 = null
        isDecodingFrame = false
        lastRenderedAppsKey = -1
        _overlayVisible.value = false
        if (view != null && wm != null) {
            runCatching { wm.removeView(view) } }
    }
    private fun mountOrUpdateWindow(context: Context, state: RootPhoneState) {
        runCatching {
            if (!canOverlay(context)) {
                removeWindow()
                return }
            val wm = windowManager ?: context.getSystemService(WindowManager::class.java).also { windowManager = it }
            if (rootView == null) {
                val metrics = context.resources.displayMetrics
                val hostLandscape = metrics.widthPixels > metrics.heightPixels
                val (initW, initH) = computeWindowSizePx(context, state, expanded, minimizedToPill)
                val (initX, initY) = PipSpringPhysics.cornerPosition(
                    snapCorner,
                    metrics.widthPixels,
                    metrics.heightPixels,
                    initW,
                    initH,
                    dp(context, 12),
                    dp(context, if (hostLandscape) 14 else 56),
                    dp(context, if (hostLandscape) 14 else 92))
                val lp = WindowManager.LayoutParams(
                    initW,
                    initH,
                    WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                        WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                        WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
                        if (RootPhoneHarness.shouldKeepScreenOn(expanded)) WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON else 0,
                    PixelFormat.TRANSLUCENT
                ).apply {
                    gravity = Gravity.TOP or Gravity.START
                    x = initX
                    y = initY }
                val container = object : FrameLayout(context) {
                    override fun onConfigurationChanged(newConfig: android.content.res.Configuration?) {
                        super.onConfigurationChanged(newConfig)
                        transitionSizeAndRebuild(context.applicationContext, RootPhoneHarness.changes.value)
                    }
                }
                params = lp
                rootView = container
                rebuildViewHierarchy(context, container, state)
                runCatching {
                    wm.addView(container, lp)
                    _overlayVisible.value = true
                    runCatching {
                        context.applicationContext.startForegroundService(Intent(context.applicationContext, RootPhoneService::class.java))
                    }
                    if (!animationsOff(context) && !PowerSaveController.isPowerSaveActive(context)) {
                        val animTarget = container.getChildAt(0) ?: container
                        animTarget.alpha = 0f
                        animTarget.scaleX = 0.90f
                        animTarget.scaleY = 0.90f
                        animTarget.animate()
                            .alpha(1f)
                            .scaleX(1f)
                            .scaleY(1f)
                            .setDuration(220L)
                            .setInterpolator(android.view.animation.PathInterpolator(0.22f, 1f, 0.32f, 1f))
                            .start()
                    }
                }.onFailure { err ->
                    android.util.Log.e("FloatingVD", "wm.addView failed", err)
                    rootView = null
                    params = null
                    clearChildViewRefs()
                    outerBorderDrawable = null
                    _overlayVisible.value = false
                    return }
            }
            updateWindowContent(context, state)
        }.onFailure { err ->
            android.util.Log.e("FloatingVD", "mountOrUpdateWindow failed", err)
        }
    }
    @SuppressLint("ClickableViewAccessibility")
    private fun rebuildViewHierarchy(context: Context, container: FrameLayout, state: RootPhoneState) {
        rebuildViewHierarchyImpl(context, container, state)
    }
    internal fun transitionSizeAndRebuild(context: Context, state: RootPhoneState) {
        val appCtx = context.applicationContext
        mainHandler.post {
            runCatching {
                val container = rootView ?: return@post
                val lp = params ?: return@post
                val wm = windowManager ?: return@post
                val fromW = lp.width
                val fromH = lp.height
                val fromX = lp.x
                val fromY = lp.y
                val metrics = appCtx.resources.displayMetrics
                val hostLandscape = metrics.widthPixels > metrics.heightPixels
                val (targetW, targetH) = computeWindowSizePx(appCtx, state, expanded, minimizedToPill)
                val (cornerX, cornerY) = if (dockedAsEdgeBubble) {
                    val bx = if (dockPillOnLeft) dp(appCtx, 4) else (metrics.widthPixels - targetW - dp(appCtx, 4)).coerceAtLeast(dp(appCtx, 4))
                    val minBY = dp(appCtx, if (hostLandscape) 14 else 56)
                    val maxBY = (metrics.heightPixels - targetH - dp(appCtx, if (hostLandscape) 14 else 92)).coerceAtLeast(minBY)
                    val by = (if (edgeBubbleY >= 0) edgeBubbleY else (metrics.heightPixels / 3)).coerceIn(minBY, maxBY)
                    bx to by
                } else {
                    PipSpringPhysics.cornerPosition(
                        if (minimizedToPill) {
                            if (dockPillOnLeft) PipSpringPhysics.Corner.BottomStart else PipSpringPhysics.Corner.BottomEnd
                        } else snapCorner,
                        metrics.widthPixels,
                        metrics.heightPixels,
                        targetW,
                        targetH,
                        dp(appCtx, 12),
                        dp(appCtx, if (hostLandscape) 14 else 56),
                        dp(appCtx, if (hostLandscape) 14 else 92))
                }
                rebuildViewHierarchy(appCtx, container, state)
                updateWindowContent(appCtx, state)
                val canMorph = !animationsOff(appCtx) && !PowerSaveController.isPowerSaveActive(appCtx) &&
                    fromW > 0 && fromH > 0 && (abs(fromW - targetW) > 2 || abs(fromH - targetH) > 2 || abs(fromX - cornerX) > 2 || abs(fromY - cornerY) > 2)
                springAnimator?.cancel()
                if (!canMorph) {
                    lp.width = targetW
                    lp.height = targetH
                    lp.x = cornerX
                    lp.y = cornerY
                    wm.updateViewLayout(container, lp)
                } else {
                    lp.width = fromW
                    lp.height = fromH
                    lp.x = fromX
                    lp.y = fromY
                    wm.updateViewLayout(container, lp)
                    springAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
                        duration = 220L
                        interpolator = android.view.animation.PathInterpolator(0.22f, 1.0f, 0.32f, 1.0f)
                        addUpdateListener { anim ->
                            val t = anim.animatedValue as Float
                            lp.width = (fromW + (targetW - fromW) * t).roundToInt().coerceAtLeast(1)
                            lp.height = (fromH + (targetH - fromH) * t).roundToInt().coerceAtLeast(1)
                            lp.x = (fromX + (cornerX - fromX) * t).roundToInt()
                            lp.y = (fromY + (cornerY - fromY) * t).roundToInt()
                            if (rootView === container) {
                                runCatching { wm.updateViewLayout(container, lp) }
                            }
                        }
                        start()
                    }
                }
            }
        } }
    internal fun computeAdaptiveLayout(
        context: Context,
        state: RootPhoneState,
        isExpanded: Boolean,
        isPill: Boolean
    ): PipLayoutMetrics = computeAdaptiveLayoutImpl(context, state, isExpanded, isPill)
    internal fun computeWindowSizePx(context: Context, state: RootPhoneState, isExpanded: Boolean, isPill: Boolean): Pair<Int, Int> {
        val m = computeAdaptiveLayout(context, state, isExpanded, isPill)
        return m.winW to m.winH }
    internal fun updateWindowContent(context: Context, state: RootPhoneState) {
        runCatching {
            val container = rootView ?: return
            val lp = params ?: return
            val wm = windowManager ?: return
            val newBorderColor = borderColorFor(state)
            if (newBorderColor != lastBorderColor) {
                lastBorderColor = newBorderColor
                outerBorderDrawable?.setStroke(dp(context, 0.9f).coerceAtLeast(1), newBorderColor) }
            if (!minimizedToPill && !dockedAsEdgeBubble) {
                if (!expanded) {
                    waitingCapsuleView?.text = "点击拉大接管"
                    waitingCapsuleView?.visibility = if (state.phase == "waiting_user") View.VISIBLE else View.GONE
                } else {
                    val capText = if (state.phase == "waiting_user") "我处理好了，继续" else "‹ 返回"
                    if (waitingCapsuleView?.text != capText) {
                        waitingCapsuleView?.text = capText }
                } }
            val dot = touchIndicatorView
            val imgView = screenImageView
            if (dot != null && imgView != null && state.lastTouchX >= 0 && state.lastTouchY >= 0 && state.lastTouchTime != lastTouchShownTime && System.currentTimeMillis() - state.lastTouchTime < 1200L && imgView.width > 0 && imgView.height > 0) {
                lastTouchShownTime = state.lastTouchTime
                val info = runCatching { JSONObject(state.imageInfo.ifBlank { state.display }) }.getOrDefault(JSONObject())
                val dispW = info.optInt("width", 540).coerceAtLeast(1)
                val dispH = info.optInt("height", 1200).coerceAtLeast(1)
                val dotRadius = dp(context, 6)
                val px = (state.lastTouchX.toFloat() / dispW * imgView.width).roundToInt() - dotRadius
                val py = (state.lastTouchY.toFloat() / dispH * imgView.height).roundToInt() - dotRadius
                if (!animationsOff(context) && !PowerSaveController.isPowerSaveActive(context)) {
                    dot.translationX = px.toFloat()
                    dot.translationY = py.toFloat()
                    dot.scaleX = 0.90f
                    dot.scaleY = 0.90f
                    dot.visibility = View.VISIBLE
                    dot.alpha = 0.92f
                    dot.animate().alpha(0f).scaleX(1.18f).scaleY(1.18f).setDuration(360L).withEndAction { dot.visibility = View.GONE }.start()
                } }
            if (!minimizedToPill && !dockedAsEdgeBubble && !userDismissed && state.image.isNotBlank() && state.image != lastDecodedB64) {
                if (isDecodingFrame) {
                    pendingFrameB64 = state.image
                } else {
                    kickFrameDecode(context, container, lp, wm, state, state.image)
                }
            } else if (state.image.isBlank()) {
                if (currentBitmap == null) {
                    emptyHintView?.visibility = View.VISIBLE }
            } else if (currentBitmap != null && screenImageView?.drawable == null) {
                screenImageView?.setImageBitmap(currentBitmap)
                emptyHintView?.visibility = View.GONE }
            val drawer = appDrawerController
            drawer?.rootView?.visibility = if (expanded && pickingApp) View.VISIBLE else View.GONE
            inlineInputBar?.visibility = if (expanded && inputBarOpen) View.VISIBLE else View.GONE
            bottomLeftRow?.visibility = if (expanded && !inputBarOpen) View.VISIBLE else View.GONE
            val needFocusable = expanded && (inputBarOpen || (pickingApp && drawerSearchFocused))
            val wantKeepOn = RootPhoneHarness.shouldKeepScreenOn(expanded)
            var newFlags = if (needFocusable) lp.flags and WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE.inv() else lp.flags or WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
            newFlags = if (wantKeepOn) newFlags or WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON else newFlags and WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON.inv()
            if (newFlags != lp.flags) { lp.flags = newFlags; runCatching { wm.updateViewLayout(container, lp) } }
            val appsKey = if (expanded && pickingApp) (state.apps.hashCode() * 31 + (state.currentPackage?.hashCode() ?: 0)) else -1
            if (expanded && pickingApp && drawer != null && appsKey != lastRenderedAppsKey) {
                lastRenderedAppsKey = appsKey
                if (state.apps.isEmpty()) {
                    overlayScope.launch(Dispatchers.IO) {
                        runCatching { RootPhoneHarness.listLaunchableApps(context) } }
                }
                drawer.updateApps(state.apps, state.currentPackage) }
            val (targetW, targetH) = computeWindowSizePx(context, state, expanded, minimizedToPill)
            if (springAnimator?.isRunning != true && (abs(lp.width - targetW) > 1 || abs(lp.height - targetH) > 1)) {
                lp.width = targetW
                lp.height = targetH
                runCatching { wm.updateViewLayout(container, lp) } }
        }.onFailure { err ->
            android.util.Log.w("FloatingVD", "updateWindowContent failed", err)
        }
    }

    private fun kickFrameDecode(
        context: Context,
        container: FrameLayout,
        lp: WindowManager.LayoutParams,
        wm: WindowManager,
        state: RootPhoneState,
        b64: String,
    ) {
        isDecodingFrame = true
        lastDecodedB64 = b64
        overlayScope.launch(Dispatchers.Default) {
            try {
                val bmp = runCatching {
                    val bytes = Base64.decode(b64, Base64.DEFAULT)
                    BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                }.getOrNull()
                if (bmp != null) {
                    withContext(Dispatchers.Main) {
                        if (rootView !== container) return@withContext
                        currentBitmap = bmp
                        screenImageView?.setImageBitmap(bmp)
                        emptyHintView?.visibility = View.GONE
                        val (tw, th) = computeWindowSizePx(context, state, expanded, minimizedToPill)
                        if (springAnimator?.isRunning != true && (abs(lp.width - tw) > 1 || abs(lp.height - th) > 1)) {
                            lp.width = tw
                            lp.height = th
                            runCatching { wm.updateViewLayout(container, lp) }
                        }
                    }
                }
            } finally {
                isDecodingFrame = false
                val next = pendingFrameB64
                pendingFrameB64 = null
                if (!next.isNullOrBlank() && next != lastDecodedB64 && !minimizedToPill && !dockedAsEdgeBubble && !userDismissed) {
                    mainHandler.post {
                        if (rootView === container && !isDecodingFrame && !minimizedToPill && !dockedAsEdgeBubble && !userDismissed) {
                            kickFrameDecode(context, container, lp, wm, RootPhoneHarness.changes.value, next)
                        }
                    }
                }
            }
        }
    }
}
