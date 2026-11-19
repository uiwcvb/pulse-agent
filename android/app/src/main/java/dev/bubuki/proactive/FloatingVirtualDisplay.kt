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
internal fun shouldAutoCloseVirtualDisplay(phase: String, displayJson: String): Boolean {
    val dispObj = runCatching { JSONObject(displayJson) }.getOrNull() ?: return false
    return dispObj.optBoolean("explicit_destroy", false)
}

internal enum class OverlayMode(val label: String) {
    HIDDEN("隐藏"),
    MINI("小窗挂起"),
    EXPANDED("展开操作"),
    EDGE_BUBBLE("贴边气泡"),
}

internal enum class OverlayDrawer {
    NONE,
    APP_PICKER,
    INPUT_BAR,
}

internal data class OverlayUiState(
    val mode: OverlayMode = OverlayMode.HIDDEN,
    val drawer: OverlayDrawer = OverlayDrawer.NONE,
    val userDismissed: Boolean = false,
) {
    val expanded: Boolean get() = mode == OverlayMode.EXPANDED
    val dockedAsEdgeBubble: Boolean get() = mode == OverlayMode.EDGE_BUBBLE
    val pickingApp: Boolean get() = mode == OverlayMode.EXPANDED && drawer == OverlayDrawer.APP_PICKER
    val inputBarOpen: Boolean get() = mode == OverlayMode.EXPANDED && drawer == OverlayDrawer.INPUT_BAR
}

internal fun reduceOverlayTransition(
    current: OverlayUiState,
    targetMode: OverlayMode,
    targetDrawer: OverlayDrawer = OverlayDrawer.NONE,
    userClosed: Boolean = current.userDismissed,
): OverlayUiState = OverlayUiState(
    mode = targetMode,
    drawer = if (targetMode == OverlayMode.EXPANDED) targetDrawer else OverlayDrawer.NONE,
    userDismissed = userClosed,
)

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
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var watchJob: Job? = null
    internal var windowManager: WindowManager? = null
    internal var rootView: FrameLayout? = null
    internal var params: WindowManager.LayoutParams? = null
    // Single source of truth for floating window state (legacy booleans are read-only projections).
    @Volatile private var uiState: OverlayUiState = OverlayUiState()
    internal val expanded: Boolean get() = uiState.expanded
    internal val fullStage: Boolean get() = false
    internal val minimizedToPill: Boolean get() = uiState.dockedAsEdgeBubble
    internal val dockedAsEdgeBubble: Boolean get() = uiState.dockedAsEdgeBubble
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
    private fun transitionState(
        mode: OverlayMode,
        drawer: OverlayDrawer = OverlayDrawer.NONE,
        userClosed: Boolean = uiState.userDismissed,
    ) {
        val next = reduceOverlayTransition(uiState, mode, drawer, userClosed)
        if (next.drawer != OverlayDrawer.APP_PICKER) {
            drawerSearchFocused = false
            appDrawerController?.clearSearchFocus()
        }
        uiState = next
        _userDismissed.value = next.userDismissed
        _overlayModeFlow.value = next.mode
    }
    internal fun currentOverlayMode(): OverlayMode = uiState.mode
    private var screenImageView: ImageView? = null
    private var touchIndicatorView: View? = null
    private var emptyHintView: TextView? = null
    private var waitingCapsuleView: TextView? = null
    private var inlineInputBar: View? = null
    private var inlineInputField: EditText? = null
    private var bottomLeftRow: View? = null
    private var appDrawerController: VirtualDisplayAppDrawer.DrawerController? = null
    private var sizeTierButton: PipVectorIconButton? = null
    private var sizeTierBadgeView: TextView? = null
    private var outerBorderDrawable: GradientDrawable? = null
    private var lastBorderColor: Int = 0
    private var lastRenderedAppsKey: Int = -1
    private var lastTouchShownTime: Long = 0L
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
        scope.launch(Dispatchers.IO) {
            runCatching {
                RootPhoneHarness.command(
                    context.applicationContext,
                    JSONObject().put("op", "resize").put("tier", sizeTier.rpcName).put("manual", true)) }
        } }
    // Frame decoding cache
    @Volatile private var lastDecodedB64: String = ""
    @Volatile internal var currentBitmap: Bitmap? = null
    @Volatile private var isDecodingFrame: Boolean = false
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
    fun resetUserDismissed() {
        uiState = uiState.copy(userDismissed = false)
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
    private fun handleUserPowerClose(context: Context) {
        hide()
        dismissedUntilElapsed = android.os.SystemClock.elapsedRealtime() + 60_000L
        AgentForegroundService.onUserForceClosedVirtualDisplay(context)
        scope.launch(Dispatchers.IO) {
            runCatching { RootPhoneHarness.command(context, JSONObject().put("op", "release").put("user_close", true).put("destroy", true)) }
        }
    }
    @JvmStatic
    fun hide() {
        transitionState(OverlayMode.HIDDEN, OverlayDrawer.NONE, userClosed = true)
        mainHandler.post {
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
        watchJob = scope.launch {
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
        springAnimator?.cancel()
        springAnimator = null
        val view = rootView
        val wm = windowManager
        rootView = null
        params = null
        screenImageView = null
        touchIndicatorView = null
        emptyHintView = null
        waitingCapsuleView = null
        inlineInputBar = null
        inlineInputField = null
        bottomLeftRow = null
        appDrawerController = null
        outerBorderDrawable = null
        sizeTierButton = null
        sizeTierBadgeView = null
        currentBitmap = null
        lastDecodedB64 = ""
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
                        WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                    PixelFormat.TRANSLUCENT
                ).apply {
                    gravity = Gravity.TOP or Gravity.START
                    x = initX
                    y = initY }
                val container = object : FrameLayout(context) {
                    override fun onConfigurationChanged(newConfig: android.content.res.Configuration?) {
                        super.onConfigurationChanged(newConfig)
                        transitionSizeAndRebuild(context, RootPhoneHarness.changes.value)
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
                    if (!animationsOff(context)) {
                        val animTarget = container.getChildAt(0) ?: container
                        animTarget.alpha = 0f
                        animTarget.scaleX = 0.90f
                        animTarget.scaleY = 0.90f
                        animTarget.animate()
                            .alpha(1f)
                            .scaleX(1f)
                            .scaleY(1f)
                            .setDuration(220L)
                            .setInterpolator(android.view.animation.OvershootInterpolator(0.75f))
                            .start()
                    }
                }.onFailure { err ->
                    android.util.Log.e("FloatingVD", "wm.addView failed", err)
                    rootView = null
                    params = null
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
        container.removeAllViews()
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
        val layout = computeAdaptiveLayout(context, state, expanded, false)
        if (overlayMode == OverlayMode.EDGE_BUBBLE) {
            val bubble = createPearlEdgeBubbleView(
                context = context,
                state = state,
                previewBitmap = currentBitmap,
                dockLeft = dockPillOnLeft,
                dp = { v -> dp(context, v) },
                onRestore = {
                    transitionState(OverlayMode.EXPANDED, OverlayDrawer.NONE, userClosed = false)
                    RootPhoneHarness.onPreviewVisibilityChanged(context)
                    val cur = RootPhoneHarness.changes.value
                    transitionSizeAndRebuild(context, cur)
                    val dispObj = runCatching { JSONObject(cur.display) }.getOrNull()
                    if (dispObj?.optBoolean("hasPromotedTask", false) == true) {
                        scope.launch(Dispatchers.IO) {
                            runCatching {
                                RootPhoneHarness.command(context, JSONObject().put("op", "pull_back").put("manual", true))
                                if (RootPhoneHarness.changes.value.phase == "waiting_user") {
                                    RootPhoneHarness.resume(context)
                                }
                            }
                        }
                    }
                },
                onClose = { handleUserPowerClose(context) }
            )
            container.addView(bubble, FrameLayout.LayoutParams(-1, -1))
            return
        }
        val pad = layout.framePadPx
        val outerRadius = layout.outerRadiusPx
        val innerRadius = layout.innerRadiusPx
        val pearlFrame = FrameLayout(context).apply {
            setPadding(pad, pad, pad, pad)
            val bg = GradientDrawable().apply {
                setColor(PEARL_WHITE_FILL_INT)
                cornerRadius = outerRadius
                setStroke(dp(context, 0.9f).coerceAtLeast(1), borderColorFor(state)) }
            outerBorderDrawable = bg
            background = bg
            elevation = dp(context, if (expanded) 20 else 13).toFloat() }
        val innerBox = FrameLayout(context).apply {
            val innerBg = GradientDrawable().apply {
                setColor(0xFF101218.toInt())
                cornerRadius = innerRadius }
            val innerRim = GradientDrawable().apply {
                setColor(Color.TRANSPARENT)
                cornerRadius = innerRadius
                setStroke(1, PEARL_INNER_HAIRLINE_INT) }
            background = LayerDrawable(arrayOf(innerBg, innerRim))
            clipToOutline = true
            outlineProvider = object : android.view.ViewOutlineProvider() {
                override fun getOutline(view: View, outline: android.graphics.Outline) {
                    outline.setRoundRect(0, 0, view.width, view.height, innerRadius) }
            } }
        val img = ImageView(context).apply {
            scaleType = ImageView.ScaleType.FIT_XY }
        screenImageView = img
        val dotSize = layout.touchDotSizePx
        val coreSize = (dotSize / 4).coerceAtLeast(dp(context, 3))
        val touchDot = FrameLayout(context).apply {
            visibility = View.GONE
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(0x18FFFFFF)
                setStroke(dp(context, 0.75f).coerceAtLeast(1), 0xCCFFFFFF.toInt()) }
            val centerCore = View(context).apply {
                background = GradientDrawable().apply {
                    shape = GradientDrawable.OVAL
                    setColor(0xE6FFFFFF.toInt()) }
            }
            addView(centerCore, FrameLayout.LayoutParams(coreSize, coreSize, Gravity.CENTER)) }
        touchIndicatorView = touchDot
        val hint = TextView(context).apply {
            text = if (expanded) "轻触选择应用启动" else "虚拟屏"
            textSize = layout.hintSp
            setTextColor(0x99FFFFFF.toInt())
            gravity = Gravity.CENTER
            setPadding(dp(context, 8), dp(context, 8), dp(context, 8), dp(context, 8))
            if (expanded) {
                setOnClickListener {
                    transitionState(OverlayMode.EXPANDED, OverlayDrawer.APP_PICKER)
                    lastRenderedAppsKey = -1
                    scope.launch(Dispatchers.IO) {
                        runCatching { RootPhoneHarness.listLaunchableApps(context) }
                    }
                    updateWindowContent(context, RootPhoneHarness.changes.value)
                }
            } }
        emptyHintView = hint
        if (!expanded) {
            innerBox.addView(img, FrameLayout.LayoutParams(-1, -1))
            innerBox.addView(touchDot, FrameLayout.LayoutParams(dotSize, dotSize))
            innerBox.addView(hint, FrameLayout.LayoutParams(-1, -1, Gravity.CENTER))
            val miniBtnSize = dp(context, 20)
            val miniHangBtn = PipVectorIconButton(context, PipVectorIconButton.IconKind.DOCK_PILL) {
                minimizeToSidePill(context)
            }.apply { contentDescription = "挂起虚拟屏" }
            innerBox.addView(miniHangBtn, FrameLayout.LayoutParams(miniBtnSize, miniBtnSize, Gravity.TOP or Gravity.START).apply {
                leftMargin = dp(context, 4)
                topMargin = dp(context, 4)
            })
            val miniCloseBtn = PipVectorIconButton(context, PipVectorIconButton.IconKind.POWER_CLOSE, isDanger = true) {
                handleUserPowerClose(context)
            }.apply { contentDescription = "关闭虚拟屏" }
            innerBox.addView(miniCloseBtn, FrameLayout.LayoutParams(miniBtnSize, miniBtnSize, Gravity.TOP or Gravity.END).apply {
                rightMargin = dp(context, 4)
                topMargin = dp(context, 4)
            })
            val waitTag = TextView(context).apply {
                text = "点击拉大接管"
                textSize = layout.capsuleSp
                setTextColor(Color.BLACK)
                typeface = Typeface.DEFAULT_BOLD
                setPadding(dp(context, 7), dp(context, 2), dp(context, 7), dp(context, 2))
                background = GradientDrawable().apply {
                    setColor(0xFFFF9F0A.toInt())
                    cornerRadius = dp(context, 8).toFloat() }
                visibility = if (state.phase == "waiting_user") View.VISIBLE else View.GONE }
            waitingCapsuleView = waitTag
            innerBox.addView(waitTag, FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL).apply {
                bottomMargin = dp(context, 5)
            })
            attachSpringDragAndTap(context, pearlFrame, dockOnEdge = true, onTap = {
                transitionState(OverlayMode.EXPANDED, OverlayDrawer.NONE, userClosed = false)
                val cur = RootPhoneHarness.changes.value
                if (cur.phase == "waiting_user" && sizeTier == SizeTier.COMPACT) {
                    sizeTier = SizeTier.MEDIUM
                    customWidthFraction = null
                }
                transitionSizeAndRebuild(context, cur)
            })
        } else {
            sizeTierButton = null
            val screenStage = FrameLayout(context)
            attachExpandedScreenTouchAndDrag(context, img, 0)
            screenStage.addView(img, FrameLayout.LayoutParams(-1, -1))
            screenStage.addView(touchDot, FrameLayout.LayoutParams(dotSize, dotSize))
            screenStage.addView(hint, FrameLayout.LayoutParams(-1, -1, Gravity.CENTER))
            val drawerCtrl = VirtualDisplayAppDrawer.createDrawerView(
                context = context,
                dp = { v -> dp(context, v) },
                topInsetPx = dp(context, 38),
                cornerRadiusPx = 0f,
                onLaunchApp = { component ->
                    transitionState(OverlayMode.EXPANDED, OverlayDrawer.NONE)
                    lastRenderedAppsKey = -1
                    scope.launch(Dispatchers.IO) {
                        runCatching {
                            RootPhoneHarness.command(
                                context,
                                JSONObject().put("op", "launch").put("component", component).put("manual", true)) }
                    }
                    updateWindowContent(context, RootPhoneHarness.changes.value)
                },
                onClose = {
                    transitionState(OverlayMode.EXPANDED, OverlayDrawer.NONE)
                    lastRenderedAppsKey = -1
                    updateWindowContent(context, RootPhoneHarness.changes.value)
                },
                onSearchFocusRequested = {
                    drawerSearchFocused = true
                    updateWindowContent(context, RootPhoneHarness.changes.value)
                })
            drawerCtrl.rootView.visibility = if (pickingApp) View.VISIBLE else View.GONE
            appDrawerController = drawerCtrl
            screenStage.addView(drawerCtrl.rootView, FrameLayout.LayoutParams(-1, -1))
            innerBox.addView(screenStage, FrameLayout.LayoutParams(-1, -1))

            // Overlaid floating translucent chrome (Mi-style minimal top bar: 应用 | draggable grabber | 全屏 & ✕)
            val grabberHandle = createTopGrabberHandle(context) { v -> dp(context, v) }
            attachSpringDragAndTap(context, grabberHandle, onTap = null, dockOnEdge = true)
            innerBox.addView(grabberHandle, FrameLayout.LayoutParams(dp(context, 108), dp(context, 28), Gravity.TOP or Gravity.CENTER_HORIZONTAL))

            val toolCapsule = createExpandedToolCapsule(
                context = context,
                sigma = layout.sigma,
                dp = { v -> dp(context, v) },
                onToggleApps = {
                    val nextDrawer = if (pickingApp) OverlayDrawer.NONE else OverlayDrawer.APP_PICKER
                    transitionState(OverlayMode.EXPANDED, nextDrawer)
                    lastRenderedAppsKey = -1
                    if (pickingApp && RootPhoneHarness.changes.value.apps.isEmpty()) {
                        scope.launch(Dispatchers.IO) { runCatching { RootPhoneHarness.listLaunchableApps(context) } }
                    }
                    updateWindowContent(context, RootPhoneHarness.changes.value)
                }
            )
            sizeTierBadgeView = null
            innerBox.addView(toolCapsule, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.START).apply {
                topMargin = dp(context, 7)
                leftMargin = dp(context, 6)
            })

            val controlCapsule = createExpandedControlCapsule(
                context = context,
                sigma = layout.sigma,
                dp = { v -> dp(context, v) },
                onPromoteFullscreen = {
                    minimizeToSidePill(context)
                    scope.launch(Dispatchers.IO) {
                        runCatching { RootPhoneHarness.command(context, JSONObject().put("op", "promote").put("manual", true)) }
                    }
                },
                onClose = { handleUserPowerClose(context) }
            )
            innerBox.addView(controlCapsule, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.END).apply {
                topMargin = dp(context, 7)
                rightMargin = dp(context, 6)
            })

            val bottomCapsule = TextView(context).apply {
                text = if (state.phase == "waiting_user") "我处理好了，继续" else "‹ 返回"
                textSize = if (state.phase == "waiting_user") (layout.capsuleSp + 0.5f) else layout.capsuleSp
                gravity = Gravity.CENTER
                setTextColor(if (state.phase == "waiting_user") Color.BLACK else 0xF2FFFFFF.toInt())
                typeface = Typeface.DEFAULT_BOLD
                setPadding(layout.capsulePadHPx, layout.capsulePadVPx, layout.capsulePadHPx, layout.capsulePadVPx)
                background = GradientDrawable().apply {
                    setColor(if (state.phase == "waiting_user") 0xFFFF9F0A.toInt() else 0xBF18181E.toInt())
                    cornerRadius = dp(context, 12 * layout.sigma).toFloat()
                    setStroke(1, 0x38FFFFFF) }
                setOnClickListener {
                    val cur = RootPhoneHarness.changes.value
                    if (cur.phase == "waiting_user") {
                        runCatching { RootPhoneHarness.resume(context) }
                    } else if (pickingApp) {
                        transitionState(OverlayMode.EXPANDED, OverlayDrawer.NONE)
                        lastRenderedAppsKey = -1
                        updateWindowContent(context, cur)
                    } else {
                        scope.launch(Dispatchers.IO) {
                            runCatching {
                                RootPhoneHarness.command(context, JSONObject().put("op", "key").put("key", "back").put("manual", true)) }
                        } }
                } }
            waitingCapsuleView = bottomCapsule

            val imeToggleBtn = TextView(context).apply {
                text = "⌨ 输入"
                textSize = layout.capsuleSp
                gravity = Gravity.CENTER
                setTextColor(0xF2FFFFFF.toInt())
                typeface = Typeface.DEFAULT_BOLD
                setPadding(dp(context, 9f * layout.sigma), layout.capsulePadVPx, dp(context, 9f * layout.sigma), layout.capsulePadVPx)
                background = GradientDrawable().apply {
                    setColor(0xBF18181E.toInt())
                    cornerRadius = dp(context, 12 * layout.sigma).toFloat()
                    setStroke(1, 0x38FFFFFF)
                }
                setOnClickListener {
                    val nextDrawer = if (inputBarOpen) OverlayDrawer.NONE else OverlayDrawer.INPUT_BAR
                    transitionState(OverlayMode.EXPANDED, nextDrawer)
                    updateWindowContent(context, RootPhoneHarness.changes.value)
                    if (inputBarOpen) {
                        mainHandler.postDelayed({
                            inlineInputField?.let { ed ->
                                ed.requestFocus()
                                context.getSystemService(InputMethodManager::class.java)?.showSoftInput(ed, InputMethodManager.SHOW_IMPLICIT)
                            }
                        }, 80L)
                    }
                }
            }
            val resizeGrip = ResizeGripView(context)
            val gripSize = dp(context, 26 * layout.sigma).coerceIn(dp(context, 22), dp(context, 34))
            attachCornerResizeTouch(context, resizeGrip)

            val bottomRow = LinearLayout(context).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                addView(bottomCapsule)
                addView(imeToggleBtn, LinearLayout.LayoutParams(-2, -2).apply { leftMargin = dp(context, 6) })
            }
            val bottomScroll = android.widget.HorizontalScrollView(context).apply {
                isHorizontalScrollBarEnabled = false
                overScrollMode = View.OVER_SCROLL_NEVER
                addView(bottomRow)
                visibility = if (inputBarOpen) View.GONE else View.VISIBLE
            }
            bottomLeftRow = bottomScroll
            innerBox.addView(bottomScroll, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM or Gravity.START).apply {
                leftMargin = dp(context, 8)
                rightMargin = gripSize + dp(context, 8)
                bottomMargin = dp(context, 7)
            })

            val (imeBar, imeEdit) = createInlineImeInputBar(
                context = context,
                sigma = layout.sigma,
                dp = { v -> dp(context, v) },
                onSend = { text ->
                    scope.launch(Dispatchers.IO) {
                        runCatching {
                            RootPhoneHarness.command(
                                context,
                                JSONObject().put("op", "text").put("text", text).put("manual", true).put("skip_tree", true)
                            )
                        }
                    }
                },
                onClose = {
                    transitionState(OverlayMode.EXPANDED, OverlayDrawer.NONE)
                    updateWindowContent(context, RootPhoneHarness.changes.value)
                }
            )
            imeBar.visibility = if (inputBarOpen) View.VISIBLE else View.GONE
            inlineInputBar = imeBar
            inlineInputField = imeEdit
            innerBox.addView(imeBar, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM).apply {
                leftMargin = dp(context, 7)
                rightMargin = dp(context, 7)
                bottomMargin = dp(context, 7)
            })
            innerBox.addView(resizeGrip, FrameLayout.LayoutParams(gripSize, gripSize, Gravity.BOTTOM or Gravity.END))
        }
        lastBorderColor = borderColorFor(state)
        lastRenderedAppsKey = -1
        pearlFrame.addView(innerBox, FrameLayout.LayoutParams(-1, -1))
        container.addView(pearlFrame, FrameLayout.LayoutParams(-1, -1)) }
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
                dot.translationX = px.toFloat()
                dot.translationY = py.toFloat()
                dot.scaleX = 0.85f
                dot.scaleY = 0.85f
                dot.visibility = View.VISIBLE
                dot.alpha = 0.92f
                dot.animate().alpha(0f).scaleX(1.18f).scaleY(1.18f).setDuration(360L).withEndAction { dot.visibility = View.GONE }.start() }
            if (!minimizedToPill && !dockedAsEdgeBubble && !userDismissed && state.image.isNotBlank() && state.image != lastDecodedB64 && !isDecodingFrame) {
                isDecodingFrame = true
                lastDecodedB64 = state.image
                val b64 = state.image
                scope.launch(Dispatchers.Default) {
                    try {
                        val bmp = runCatching {
                            val bytes = Base64.decode(b64, Base64.DEFAULT)
                            BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                        }.getOrNull()
                        if (bmp != null) {
                            withContext(Dispatchers.Main) {
                                currentBitmap = bmp
                                screenImageView?.setImageBitmap(bmp)
                                emptyHintView?.visibility = View.GONE
                                val (tw, th) = computeWindowSizePx(context, state, expanded, minimizedToPill)
                                if (abs(lp.width - tw) > 1 || abs(lp.height - th) > 1) {
                                    lp.width = tw
                                    lp.height = th
                                    if (rootView === container) runCatching { wm.updateViewLayout(container, lp) } }
                            } }
                    } finally {
                        isDecodingFrame = false }
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
            val curNotFocusable = (lp.flags and WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE) != 0
            if (needFocusable == curNotFocusable) {
                lp.flags = if (needFocusable) {
                    lp.flags and WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE.inv()
                } else {
                    lp.flags or WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE }
                runCatching { wm.updateViewLayout(container, lp) } }
            val appsKey = if (expanded && pickingApp) (state.apps.hashCode() * 31 + (state.currentPackage?.hashCode() ?: 0)) else -1
            if (expanded && pickingApp && drawer != null && appsKey != lastRenderedAppsKey) {
                lastRenderedAppsKey = appsKey
                if (state.apps.isEmpty()) {
                    scope.launch(Dispatchers.IO) {
                        runCatching { RootPhoneHarness.listLaunchableApps(context) } }
                }
                drawer.updateApps(state.apps, state.currentPackage) }
            val (targetW, targetH) = computeWindowSizePx(context, state, expanded, minimizedToPill)
            if (abs(lp.width - targetW) > 1 || abs(lp.height - targetH) > 1) {
                lp.width = targetW
                lp.height = targetH
                runCatching { wm.updateViewLayout(container, lp) } }
        }.onFailure { err ->
            android.util.Log.w("FloatingVD", "updateWindowContent failed", err)
        }
    }
}
