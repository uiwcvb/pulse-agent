package dev.bubuki.proactive

import org.json.JSONObject

/**
 * [Module] FloatingOverlayStateMachine
 * [Responsibility] Deterministic single-source-of-truth state transitions for the system
 *                  floating virtual display overlay (`MINI`, `EXPANDED`, `EDGE_BUBBLE`, `HIDDEN`).
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
