package dev.bubuki.proactive

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.LayerDrawable
import android.view.Gravity
import android.view.View
import android.view.inputmethod.InputMethodManager
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * [Module] FloatingVirtualDisplayViews
 * [Responsibility] Constructs the pearl-white floating overlay View hierarchy for `EDGE_BUBBLE`,
 *                  `MINI`, and `EXPANDED` modes, wiring chrome controls, touch stage, app drawer,
 *                  and inline IME bar back to `FloatingVirtualDisplay`.
 */

@SuppressLint("ClickableViewAccessibility")
internal fun FloatingVirtualDisplay.rebuildViewHierarchyImpl(
    context: Context,
    container: FrameLayout,
    state: RootPhoneState
) {
    container.removeAllViews()
    clearChildViewRefs()
    val layout = computeAdaptiveLayout(context, state, expanded, false)
    if (currentOverlayMode() == OverlayMode.EDGE_BUBBLE) {
        val bubble = createPearlEdgeBubbleView(
            context = context,
            state = state,
            previewBitmap = cachedBitmap(),
            dockLeft = dockPillOnLeft,
            dp = { v -> dp(context, v) },
            onRestore = {
                transitionState(OverlayMode.EXPANDED, OverlayDrawer.NONE, userClosed = false)
                RootPhoneHarness.onPreviewVisibilityChanged(context)
                val cur = RootPhoneHarness.changes.value
                transitionSizeAndRebuild(context, cur)
                val dispObj = runCatching { JSONObject(cur.display) }.getOrNull()
                if (dispObj?.optBoolean("hasPromotedTask", false) == true) {
                    overlayScope.launch(Dispatchers.IO) {
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
            setStroke(dp(context, 0.9f).coerceAtLeast(1), borderColorFor(state))
        }
        outerBorderDrawable = bg
        background = bg
        elevation = dp(context, if (expanded) 20 else 13).toFloat()
    }
    val innerBox = FrameLayout(context).apply {
        val innerBg = GradientDrawable().apply {
            setColor(0xFF101218.toInt())
            cornerRadius = innerRadius
        }
        val innerRim = GradientDrawable().apply {
            setColor(Color.TRANSPARENT)
            cornerRadius = innerRadius
            setStroke(1, PEARL_INNER_HAIRLINE_INT)
        }
        background = LayerDrawable(arrayOf(innerBg, innerRim))
        clipToOutline = true
        outlineProvider = object : android.view.ViewOutlineProvider() {
            override fun getOutline(view: View, outline: android.graphics.Outline) {
                outline.setRoundRect(0, 0, view.width, view.height, innerRadius)
            }
        }
    }
    val img = ImageView(context).apply {
        scaleType = ImageView.ScaleType.FIT_XY
    }
    screenImageView = img
    val dotSize = layout.touchDotSizePx
    val coreSize = (dotSize / 4).coerceAtLeast(dp(context, 3))
    val touchDot = FrameLayout(context).apply {
        visibility = View.GONE
        background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(0x18FFFFFF)
            setStroke(dp(context, 0.75f).coerceAtLeast(1), 0xCCFFFFFF.toInt())
        }
        val centerCore = View(context).apply {
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(0xE6FFFFFF.toInt())
            }
        }
        addView(centerCore, FrameLayout.LayoutParams(coreSize, coreSize, Gravity.CENTER))
    }
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
                overlayScope.launch(Dispatchers.IO) {
                    runCatching { RootPhoneHarness.listLaunchableApps(context) }
                }
                updateWindowContent(context, RootPhoneHarness.changes.value)
            }
        }
    }
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
                cornerRadius = dp(context, 8).toFloat()
            }
            visibility = if (state.phase == "waiting_user") View.VISIBLE else View.GONE
        }
        waitingCapsuleView = waitTag
        innerBox.addView(waitTag, FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL).apply {
            bottomMargin = dp(context, 5)
        })
        attachSpringDragAndTap(context, pearlFrame, dockOnEdge = true, onTap = {
            transitionState(OverlayMode.EXPANDED, OverlayDrawer.NONE, userClosed = false)
            val cur = RootPhoneHarness.changes.value
            promoteTierOnWaitingExpandIfCompact(cur.phase)
            transitionSizeAndRebuild(context, cur)
        })
    } else {
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
                overlayScope.launch(Dispatchers.IO) {
                    runCatching {
                        RootPhoneHarness.command(
                            context,
                            JSONObject().put("op", "launch").put("component", component).put("manual", true)
                        )
                    }
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
            }
        )
        drawerCtrl.rootView.visibility = if (uiPickingApp) View.VISIBLE else View.GONE
        appDrawerController = drawerCtrl
        screenStage.addView(drawerCtrl.rootView, FrameLayout.LayoutParams(-1, -1))
        innerBox.addView(screenStage, FrameLayout.LayoutParams(-1, -1))

        val grabberHandle = createTopGrabberHandle(context) { v -> dp(context, v) }
        attachSpringDragAndTap(context, grabberHandle, onTap = null, dockOnEdge = true)
        innerBox.addView(grabberHandle, FrameLayout.LayoutParams(dp(context, 108), dp(context, 28), Gravity.TOP or Gravity.CENTER_HORIZONTAL))

        val toolCapsule = createExpandedToolCapsule(
            context = context,
            sigma = layout.sigma,
            dp = { v -> dp(context, v) },
            onToggleApps = {
                val nextDrawer = if (uiPickingApp) OverlayDrawer.NONE else OverlayDrawer.APP_PICKER
                transitionState(OverlayMode.EXPANDED, nextDrawer)
                lastRenderedAppsKey = -1
                if (uiPickingApp && RootPhoneHarness.changes.value.apps.isEmpty()) {
                    overlayScope.launch(Dispatchers.IO) { runCatching { RootPhoneHarness.listLaunchableApps(context) } }
                }
                updateWindowContent(context, RootPhoneHarness.changes.value)
            }
        )
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
                overlayScope.launch(Dispatchers.IO) {
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
                setStroke(1, 0x38FFFFFF)
            }
            setOnClickListener {
                val cur = RootPhoneHarness.changes.value
                if (cur.phase == "waiting_user") {
                    runCatching { RootPhoneHarness.resume(context) }
                } else if (uiPickingApp) {
                    transitionState(OverlayMode.EXPANDED, OverlayDrawer.NONE)
                    lastRenderedAppsKey = -1
                    updateWindowContent(context, cur)
                } else {
                    overlayScope.launch(Dispatchers.IO) {
                        runCatching {
                            RootPhoneHarness.command(context, JSONObject().put("op", "key").put("key", "back").put("manual", true))
                        }
                    }
                }
            }
        }
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
                val nextDrawer = if (uiInputBarOpen) OverlayDrawer.NONE else OverlayDrawer.INPUT_BAR
                transitionState(OverlayMode.EXPANDED, nextDrawer)
                updateWindowContent(context, RootPhoneHarness.changes.value)
                if (uiInputBarOpen) {
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
            visibility = if (uiInputBarOpen) View.GONE else View.VISIBLE
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
                overlayScope.launch(Dispatchers.IO) {
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
        imeBar.visibility = if (uiInputBarOpen) View.VISIBLE else View.GONE
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
    container.addView(pearlFrame, FrameLayout.LayoutParams(-1, -1))
}
