package dev.bubuki.proactive

import android.content.Context
import android.os.SystemClock
import kotlinx.coroutines.flow.update
import org.json.JSONArray
import org.json.JSONObject

/**
 * [Module] RootPhoneHarnessActions
 * [Responsibility] Coordinate normalization/validation, Accessibility UI tree node resolution &
 *                  interaction (`ui_tree`, `tap_node`, `type_node`, `tap_and_type`), and frame attach.
 *                  Device telemetry, shell, notifications, and app resolution live in RootPhoneDeviceShell.kt.
 */

private fun mapNormToPixel(value: Double, dim: Int, axis: String): Int {
    if (value.isNaN() || value < 0.0 || value > 1000.0) {
        throw IllegalArgumentException("归一化坐标 $axis=$value 超出 [0, 1000] 范围")
    }
    return (value / 1000.0 * dim).toInt().coerceIn(0, (dim - 1).coerceAtLeast(0))
}

internal fun RootPhoneHarness.resolveGridOrNormCoords(operation: String, cmdObj: JSONObject, dispW: Int, dispH: Int) {
    if (cmdObj.has("start_x") && !cmdObj.has("x1")) cmdObj.put("x1", cmdObj.opt("start_x"))
    if (cmdObj.has("start_y") && !cmdObj.has("y1")) cmdObj.put("y1", cmdObj.opt("start_y"))
    if (cmdObj.has("end_x") && !cmdObj.has("x2")) cmdObj.put("x2", cmdObj.opt("end_x"))
    if (cmdObj.has("end_y") && !cmdObj.has("y2")) cmdObj.put("y2", cmdObj.opt("end_y"))
    val dir = cmdObj.optString("direction", "").trim().lowercase()
    if (operation == "swipe" && dir.isNotEmpty() && !cmdObj.has("x2") && !cmdObj.has("nx2")) {
        val cx = dispW / 2
        val cy = dispH / 2
        when (dir) {
            "up" -> { cmdObj.put("x1", cx); cmdObj.put("y1", (dispH * 0.72).toInt()); cmdObj.put("x2", cx); cmdObj.put("y2", (dispH * 0.25).toInt()) }
            "down" -> { cmdObj.put("x1", cx); cmdObj.put("y1", (dispH * 0.28).toInt()); cmdObj.put("x2", cx); cmdObj.put("y2", (dispH * 0.75).toInt()) }
            "left" -> { cmdObj.put("x1", (dispW * 0.80).toInt()); cmdObj.put("y1", cy); cmdObj.put("x2", (dispW * 0.20).toInt()); cmdObj.put("y2", cy) }
            "right" -> { cmdObj.put("x1", (dispW * 0.20).toInt()); cmdObj.put("y1", cy); cmdObj.put("x2", (dispW * 0.80).toInt()); cmdObj.put("y2", cy) }
        }
    }
    val grid = cmdObj.optString("grid", "").trim().uppercase()
    if (grid.length in 2..3 && grid[0] in 'A'..'F') {
        val colIdx = grid[0] - 'A'
        val rowIdx = (grid.substring(1).toIntOrNull() ?: 1).coerceIn(1, 10) - 1
        cmdObj.put("x", ((colIdx + 0.5) / 6.0 * dispW).toInt().coerceIn(0, (dispW - 1).coerceAtLeast(0)))
        cmdObj.put("y", ((rowIdx + 0.5) / 10.0 * dispH).toInt().coerceIn(0, (dispH - 1).coerceAtLeast(0)))
        return
    }
    val isNormFlag = cmdObj.optBoolean("normalized", false) || cmdObj.optString("coord_space", "") == "normalized"
    if (cmdObj.has("nx") || cmdObj.has("ny")) {
        if (!cmdObj.has("nx") || !cmdObj.has("ny")) {
            throw IllegalArgumentException("必须同时提供 nx 与 ny 归一化坐标")
        }
        val nx = cmdObj.optDouble("nx", Double.NaN)
        val ny = cmdObj.optDouble("ny", Double.NaN)
        cmdObj.put("x", mapNormToPixel(nx, dispW, "nx"))
        cmdObj.put("y", mapNormToPixel(ny, dispH, "ny"))
    } else if (!cmdObj.has("grid") && cmdObj.has("x") && cmdObj.has("y")) {
        val rx = cmdObj.optDouble("x", Double.NaN)
        val ry = cmdObj.optDouble("y", Double.NaN)
        if (rx.isNaN() || ry.isNaN()) throw IllegalArgumentException("坐标 x, y 必须是合法数字")
        if (isNormFlag) {
            cmdObj.put("x", mapNormToPixel(rx, dispW, "x"))
            cmdObj.put("y", mapNormToPixel(ry, dispH, "y"))
        } else {
            if (rx < 0 || ry < 0) throw IllegalArgumentException("像素坐标不能为负数: ($rx, $ry)")
            cmdObj.put("x", rx.toInt().coerceIn(0, (dispW - 1).coerceAtLeast(0)))
            cmdObj.put("y", ry.toInt().coerceIn(0, (dispH - 1).coerceAtLeast(0)))
        }
    }
    if (cmdObj.has("nx1") || cmdObj.has("ny1")) {
        if (!cmdObj.has("nx1") || !cmdObj.has("ny1")) throw IllegalArgumentException("必须同时提供 nx1 与 ny1")
        cmdObj.put("x1", mapNormToPixel(cmdObj.optDouble("nx1", Double.NaN), dispW, "nx1"))
        cmdObj.put("y1", mapNormToPixel(cmdObj.optDouble("ny1", Double.NaN), dispH, "ny1"))
    } else if (cmdObj.has("x1") && cmdObj.has("y1")) {
        val rx1 = cmdObj.optDouble("x1", Double.NaN)
        val ry1 = cmdObj.optDouble("y1", Double.NaN)
        if (rx1.isNaN() || ry1.isNaN()) throw IllegalArgumentException("坐标 x1, y1 必须是合法数字")
        if (isNormFlag) {
            cmdObj.put("x1", mapNormToPixel(rx1, dispW, "x1"))
            cmdObj.put("y1", mapNormToPixel(ry1, dispH, "y1"))
        } else {
            if (rx1 < 0 || ry1 < 0) throw IllegalArgumentException("像素坐标 x1, y1 不能为负数")
            cmdObj.put("x1", rx1.toInt().coerceIn(0, (dispW - 1).coerceAtLeast(0)))
            cmdObj.put("y1", ry1.toInt().coerceIn(0, (dispH - 1).coerceAtLeast(0)))
        }
    }
    if (cmdObj.has("nx2") || cmdObj.has("ny2")) {
        if (!cmdObj.has("nx2") || !cmdObj.has("ny2")) throw IllegalArgumentException("必须同时提供 nx2 与 ny2")
        cmdObj.put("x2", mapNormToPixel(cmdObj.optDouble("nx2", Double.NaN), dispW, "nx2"))
        cmdObj.put("y2", mapNormToPixel(cmdObj.optDouble("ny2", Double.NaN), dispH, "ny2"))
    } else if (cmdObj.has("x2") && cmdObj.has("y2")) {
        val rx2 = cmdObj.optDouble("x2", Double.NaN)
        val ry2 = cmdObj.optDouble("y2", Double.NaN)
        if (rx2.isNaN() || ry2.isNaN()) throw IllegalArgumentException("坐标 x2, y2 必须是合法数字")
        if (isNormFlag) {
            cmdObj.put("x2", mapNormToPixel(rx2, dispW, "x2"))
            cmdObj.put("y2", mapNormToPixel(ry2, dispH, "y2"))
        } else {
            if (rx2 < 0 || ry2 < 0) throw IllegalArgumentException("像素坐标 x2, y2 不能为负数")
            cmdObj.put("x2", rx2.toInt().coerceIn(0, (dispW - 1).coerceAtLeast(0)))
            cmdObj.put("y2", ry2.toInt().coerceIn(0, (dispH - 1).coerceAtLeast(0)))
        }
    }
}

internal fun RootPhoneHarness.formatInteractiveNodesBrief(displayId: Int): String {
    val snap = PulseAccessibilityService.getCachedSnapshot(displayId) ?: return "无障碍节点缓存为空"
    val candidates = snap.nodes.filter { it.editable || it.clickable || it.role.equals("EditText", ignoreCase = true) }.take(8)
    if (candidates.isEmpty()) return "当前无障碍树无可见可交互节点 (共 ${snap.nodes.size} 个节点)"
    return candidates.joinToString("; ") { n ->
        val lbl = n.text.ifBlank { n.desc.ifBlank { n.hint.ifBlank { n.resourceId } } }.take(20)
        "${n.ref}[${n.role}:'${lbl}'@(${n.centerX},${n.centerY})]"
    }
}

internal fun RootPhoneHarness.executeTapAndTypeAction(
    base: Context,
    input: JSONObject,
    currentDisplay: Int,
    dispW: Int,
    dispH: Int
): JSONObject {
    if (currentDisplay <= 0) throw Exception("先创建独立工作区")
    val ref = input.optString("ref", input.optString("node", "")).trim()
    val targetLabel = input.optString("target", input.optString("label", input.optString("hint", ""))).trim()
    val resId = input.optString("id", input.optString("view_id", input.optString("resource_id", ""))).trim()
    val txt = input.optString("text", input.optString("value", ""))
    val clear = input.optBoolean("clear", false)
    val submit = input.optBoolean("submit", false) || input.optBoolean("press_enter", false) || input.optBoolean("send", false)
    val hasExplicitCoords = input.has("x") && input.has("y")
    var resolvedNode: PulseAccessibilityService.CachedNode? = null
    if (!hasExplicitCoords) {
        if (ref.isEmpty() && targetLabel.isEmpty() && resId.isEmpty()) {
            throw Exception("tap_and_type 需要明确提供坐标 (nx, ny)/(x, y) 或目标节点 ref/target/id")
        }
        resolvedNode = PulseAccessibilityService.getCachedSnapshot(currentDisplay)?.let {
            PulseAccessibilityService.matchNodeInSnapshot(it, ref, targetLabel, resId, preferEditable = true)
        } ?: PulseAccessibilityService.resolveTargetNode(currentDisplay, dispW, dispH, ref, targetLabel, resId, preferEditable = true)
        if (resolvedNode == null) {
            readUiTreeForDisplay(currentDisplay, dispW, dispH, forceRefresh = true)
            resolvedNode = PulseAccessibilityService.resolveTargetNode(currentDisplay, dispW, dispH, ref, targetLabel, resId, preferEditable = true)
        }
    }
    val brief = formatInteractiveNodesBrief(currentDisplay)
    val tx = if (hasExplicitCoords) input.optInt("x") else (resolvedNode?.centerX ?: throw Exception("未识别到输入框节点 (ref=$ref, target=$targetLabel, id=$resId)；当前可交互节点: $brief"))
    val ty = if (hasExplicitCoords) input.optInt("y") else (resolvedNode?.centerY ?: throw Exception("未识别到输入框节点 (ref=$ref, target=$targetLabel, id=$resId)；当前可交互节点: $brief"))
    var a11yOk = false
    if ((txt.isNotEmpty() || clear) && resolvedNode != null) {
        a11yOk = if (txt.isEmpty() && clear) {
            PulseAccessibilityService.performAccessibilityAction(currentDisplay, resolvedNode, "clear_text", "")
        } else {
            PulseAccessibilityService.performAccessibilityAction(currentDisplay, resolvedNode, "set_text", txt)
        }
    }
    if (!a11yOk) {
        privilege!!.execute(JSONObject().put("op", "tap").put("display_id", currentDisplay).put("x", tx).put("y", ty))
        state.update { it.copy(lastTouchX = tx, lastTouchY = ty, lastTouchTime = System.currentTimeMillis()) }
        FloatingVirtualDisplay.onWorkspaceStateChanged(base.applicationContext, state.value)
        Thread.sleep(320)
        if ((txt.isNotEmpty() || clear) && resolvedNode != null) {
            a11yOk = if (txt.isEmpty() && clear) {
                PulseAccessibilityService.performAccessibilityAction(currentDisplay, resolvedNode, "clear_text", "")
            } else {
                PulseAccessibilityService.performAccessibilityAction(currentDisplay, resolvedNode, "set_text", txt)
            }
        }
    }
    val typeRes = if (txt.isEmpty() && !clear) {
        JSONObject()
    } else if (a11yOk) {
        JSONObject().put("ok", true).put("typed", true).put("cleared", clear).put("method", "accessibility_set_text")
    } else {
        privilege!!.execute(JSONObject().put("op", "text").put("display_id", currentDisplay).put("text", txt).put("clear", clear))
    }
    if (submit) {
        Thread.sleep(200)
        privilege!!.execute(JSONObject().put("op", "key").put("display_id", currentDisplay).put("key", "enter"))
    }
    val skipTree = input.optBoolean("skip_tree", false) || input.optBoolean("pure_vision", false)
    val afterTree = if (skipTree) JSONObject() else readUiTreeForDisplay(currentDisplay, dispW, dispH, forceRefresh = true, waitMs = 450L)
    val summary = afterTree.optString("summary", "")
    val aHash = afterTree.optString("treeHash", "")
    if (!skipTree) state.update { it.copy(lastTreeSummary = summary) }
    return JSONObject()
        .put("uid", 0)
        .put("ok", true)
        .put("created", true)
        .put("displayId", currentDisplay)
        .put("display_id", currentDisplay)
        .put("width", dispW)
        .put("height", dispH)
        .put("densityDpi", afterTree.optInt("densityDpi", ((dispW * 160) / 360).coerceIn(240, 640)))
        .put("screenWidthDp", afterTree.optInt("screenWidthDp", 360))
        .put("fontScale", 1.0)
        .put("op", "tap_and_type")
        .put("ref", resolvedNode?.ref ?: ref)
        .put("x", tx)
        .put("y", ty)
        .put("tapped", JSONObject().put("x", tx).put("y", ty))
        .put("typed", typeRes)
        .put("package", afterTree.optString("package", "unknown"))
        .put("treeHash", aHash)
        .put("tree_hash", aHash)
        .put("nodeCount", afterTree.optInt("nodeCount", 0))
        .put("layoutStable", afterTree.optBoolean("layoutStable", false))
        .put("settled", afterTree.optBoolean("settled", false))
        .put("settle_ms", afterTree.optLong("settle_ms", 0L))
        .put("layout_signature", afterTree.optString("layout_signature", ""))
        .put("ui_tree", summary)
        .also { attachLatestDisplayFrame(it, currentDisplay) }
}

internal fun RootPhoneHarness.executeTapNodeAction(
    base: Context,
    input: JSONObject,
    currentDisplay: Int,
    dispW: Int,
    dispH: Int
): JSONObject {
    if (currentDisplay <= 0) throw Exception("先创建独立工作区")
    val ref = input.optString("ref", input.optString("node", "")).trim()
    val text = input.optString("text", input.optString("target", input.optString("label", ""))).trim()
    val resId = input.optString("id", input.optString("resource_id", "")).trim()
    var node = PulseAccessibilityService.getCachedSnapshot(currentDisplay)?.let {
        PulseAccessibilityService.matchNodeInSnapshot(it, ref, text, resId)
    } ?: PulseAccessibilityService.resolveTargetNode(currentDisplay, dispW, dispH, ref, text, resId)
    if (node == null) {
        readUiTreeForDisplay(currentDisplay, dispW, dispH, forceRefresh = true)
        node = PulseAccessibilityService.resolveTargetNode(currentDisplay, dispW, dispH, ref, text, resId)
    }
    if (node == null) {
        val brief = formatInteractiveNodesBrief(currentDisplay)
        throw Exception("在当前无障碍节点树中未找到目标节点 (ref=$ref, text=$text, id=$resId)；当前可交互节点: $brief")
    }
    val beforePkg = PulseAccessibilityService.getCachedSnapshot(currentDisplay)?.activePackage.orEmpty()
    val a11yClicked = PulseAccessibilityService.performAccessibilityAction(currentDisplay, node, "click")
    var settledTree: JSONObject? = null
    val tapRes = if (!a11yClicked) {
        val rawTap = privilege!!.execute(JSONObject().put("op", "tap").put("display_id", currentDisplay).put("x", node.centerX).put("y", node.centerY))
        try { Thread.sleep(260L) } catch (ignored: InterruptedException) {}
        settledTree = readUiTreeForDisplay(currentDisplay, dispW, dispH, forceRefresh = true, waitMs = 650L)
        rawTap
    } else {
        try { Thread.sleep(240L) } catch (ignored: InterruptedException) {}
        JSONObject().put("uid", 0).put("created", true).put("displayId", currentDisplay).put("display_id", currentDisplay).put("clicked", true)
    }
    var finalTree = settledTree ?: readUiTreeForDisplay(currentDisplay, dispW, dispH, forceRefresh = true, waitMs = 550L)
    if (beforePkg == "dev.bubuki.proactive" && !node.editable && finalTree.optString("package") == "dev.bubuki.proactive") {
        try { Thread.sleep(480L) } catch (ignored: InterruptedException) {}
        finalTree = readUiTreeForDisplay(currentDisplay, dispW, dispH, forceRefresh = true, waitMs = 850L)
    }
    val summary = finalTree.optString("summary", "")
    val fHash = finalTree.optString("treeHash", "")
    val nowTs = System.currentTimeMillis()
    state.update { it.copy(lastTouchX = node.centerX, lastTouchY = node.centerY, lastTouchTime = nowTs, lastTreeSummary = summary) }
    FloatingVirtualDisplay.onWorkspaceStateChanged(base.applicationContext, state.value)
    return tapRes
        .put("created", true)
        .put("displayId", currentDisplay)
        .put("display_id", currentDisplay)
        .put("width", dispW)
        .put("height", dispH)
        .put("densityDpi", finalTree.optInt("densityDpi", ((dispW * 160) / 360).coerceIn(240, 640)))
        .put("screenWidthDp", finalTree.optInt("screenWidthDp", 360))
        .put("fontScale", 1.0)
        .put("clicked", true)
        .put("method", if (a11yClicked) "accessibility_node" else "node_center_tap")
        .put("ref", node.ref)
        .put("role", node.role)
        .put("label", node.text.ifBlank { node.desc })
        .put("x", node.centerX)
        .put("y", node.centerY)
        .put("package", finalTree.optString("package", "unknown"))
        .put("treeHash", fHash)
        .put("tree_hash", fHash)
        .put("nodeCount", finalTree.optInt("nodeCount", 0))
        .put("layoutStable", finalTree.optBoolean("layoutStable", false))
        .put("settled", finalTree.optBoolean("settled", false))
        .put("settle_ms", finalTree.optLong("settle_ms", 0L))
        .put("likely_blank", finalTree.optBoolean("likely_blank", false))
        .put("layout_signature", finalTree.optString("layout_signature", ""))
        .put("ui_tree", summary)
        .also { attachLatestDisplayFrame(it, currentDisplay) }
}

internal fun RootPhoneHarness.executeTypeNodeAction(
    input: JSONObject,
    currentDisplay: Int,
    dispW: Int,
    dispH: Int
): JSONObject {
    if (currentDisplay <= 0) throw Exception("先创建独立工作区")
    val ref = input.optString("ref", input.optString("node", "")).trim()
    val targetLabel = input.optString("target", input.optString("label", input.optString("hint", ""))).trim()
    val resId = input.optString("id", input.optString("resource_id", "")).trim()
    val contentText = input.optString("text", input.optString("value", ""))
    val clear = input.optBoolean("clear", false)
    val submit = input.optBoolean("submit", false) || input.optBoolean("press_enter", false) || input.optBoolean("send", false)
    if (!clear && contentText.isEmpty()) throw Exception("type_node 需要提供要输入的 text 或设置 clear=true")
    val hasExplicitQuery = ref.isNotEmpty() || targetLabel.isNotEmpty() || resId.isNotEmpty()
    if (!hasExplicitQuery) {
        val brief = formatInteractiveNodesBrief(currentDisplay)
        throw Exception("type_node 需要明确指定输入框节点 ref、target 或 id（或改用纯视觉 op=\"text\" 向当前焦点输入）；当前可交互节点: $brief")
    }
    var node = PulseAccessibilityService.getCachedSnapshot(currentDisplay)?.let {
        PulseAccessibilityService.matchNodeInSnapshot(it, ref, targetLabel, resId, preferEditable = true)
    } ?: PulseAccessibilityService.resolveTargetNode(currentDisplay, dispW, dispH, ref, targetLabel, resId, preferEditable = true)
    if (node == null) {
        readUiTreeForDisplay(currentDisplay, dispW, dispH, forceRefresh = true)
        node = PulseAccessibilityService.resolveTargetNode(currentDisplay, dispW, dispH, ref, targetLabel, resId, preferEditable = true)
    }
    if (node == null) {
        val brief = formatInteractiveNodesBrief(currentDisplay)
        throw Exception("未找到输入框节点 (ref=$ref, target=$targetLabel, id=$resId)；当前可交互节点: $brief")
    }
    val actionName = if (contentText.isEmpty() && clear) "clear_text" else "set_text"
    var a11yTyped = PulseAccessibilityService.performAccessibilityAction(currentDisplay, node, actionName, contentText)
    if (!a11yTyped) {
        runCatching {
            privilege!!.execute(JSONObject().put("op", "tap").put("display_id", currentDisplay).put("x", node.centerX).put("y", node.centerY))
            Thread.sleep(240L)
        }
        a11yTyped = PulseAccessibilityService.performAccessibilityAction(currentDisplay, node, actionName, contentText)
    }
    if (a11yTyped) {
        if (submit) {
            Thread.sleep(200)
            privilege!!.execute(JSONObject().put("op", "key").put("display_id", currentDisplay).put("key", "enter"))
        }
        val afterTree = readUiTreeForDisplay(currentDisplay, dispW, dispH, forceRefresh = true, waitMs = 450L)
        val summary = afterTree.optString("summary", "")
        val aHash = afterTree.optString("treeHash", "")
        state.update { it.copy(lastTreeSummary = summary) }
        return JSONObject()
            .put("uid", 0)
            .put("ok", true)
            .put("created", true)
            .put("displayId", currentDisplay)
            .put("display_id", currentDisplay)
            .put("width", dispW)
            .put("height", dispH)
            .put("densityDpi", afterTree.optInt("densityDpi", ((dispW * 160) / 360).coerceIn(240, 640)))
            .put("screenWidthDp", afterTree.optInt("screenWidthDp", 360))
            .put("fontScale", 1.0)
            .put("typed", true)
            .put("cleared", clear)
            .put("method", "accessibility_set_text")
            .put("ref", node.ref)
            .put("role", node.role)
            .put("characters", contentText.length)
            .put("package", afterTree.optString("package", "unknown"))
            .put("treeHash", aHash)
            .put("tree_hash", aHash)
            .put("nodeCount", afterTree.optInt("nodeCount", 0))
            .put("layoutStable", afterTree.optBoolean("layoutStable", false))
            .put("settled", afterTree.optBoolean("settled", false))
            .put("settle_ms", afterTree.optLong("settle_ms", 0L))
            .put("layout_signature", afterTree.optString("layout_signature", ""))
            .put("ui_tree", summary)
            .also { attachLatestDisplayFrame(it, currentDisplay) }
    }
    val pasteRes = privilege!!.execute(JSONObject().put("op", "text").put("display_id", currentDisplay).put("text", contentText).put("clear", clear))
    if (submit) {
        Thread.sleep(200)
        privilege!!.execute(JSONObject().put("op", "key").put("display_id", currentDisplay).put("key", "enter"))
    }
    val afterTree = readUiTreeForDisplay(currentDisplay, dispW, dispH, forceRefresh = true, waitMs = 450L)
    val summary = afterTree.optString("summary", "")
    val pHash = afterTree.optString("treeHash", "")
    state.update { it.copy(lastTreeSummary = summary) }
    return pasteRes
        .put("ok", true)
        .put("created", true)
        .put("displayId", currentDisplay)
        .put("display_id", currentDisplay)
        .put("width", dispW)
        .put("height", dispH)
        .put("densityDpi", afterTree.optInt("densityDpi", ((dispW * 160) / 360).coerceIn(240, 640)))
        .put("screenWidthDp", afterTree.optInt("screenWidthDp", 360))
        .put("fontScale", 1.0)
        .put("typed", true)
        .put("cleared", clear)
        .put("method", "clipboard_paste")
        .put("ref", node.ref)
        .put("package", afterTree.optString("package", "unknown"))
        .put("treeHash", pHash)
        .put("tree_hash", pHash)
        .put("nodeCount", afterTree.optInt("nodeCount", 0))
        .put("layoutStable", afterTree.optBoolean("layoutStable", false))
        .put("settled", afterTree.optBoolean("settled", false))
        .put("settle_ms", afterTree.optLong("settle_ms", 0L))
        .put("layout_signature", afterTree.optString("layout_signature", ""))
        .put("ui_tree", summary)
        .also { attachLatestDisplayFrame(it, currentDisplay) }
}

internal fun RootPhoneHarness.resolveLongPressNodeCoords(
    input: JSONObject,
    currentDisplay: Int,
    dispW: Int,
    dispH: Int
) {
    if (currentDisplay <= 0) throw Exception("先创建独立工作区")
    val ref = input.optString("ref", input.optString("node", "")).trim()
    val text = input.optString("text", input.optString("target", input.optString("label", ""))).trim()
    val resId = input.optString("id", input.optString("resource_id", "")).trim()
    var node = PulseAccessibilityService.resolveTargetNode(currentDisplay, dispW, dispH, ref, text, resId)
    if (node == null) {
        readUiTreeForDisplay(currentDisplay, dispW, dispH, forceRefresh = true)
        node = PulseAccessibilityService.resolveTargetNode(currentDisplay, dispW, dispH, ref, text, resId)
    }
    if (node == null) {
        throw Exception("在当前无障碍节点树中未找到长按目标节点 (ref=$ref, text=$text, id=$resId)")
    }
    input.put("x", node.centerX).put("y", node.centerY)
}

internal fun RootPhoneHarness.attachLatestDisplayFrame(target: JSONObject, displayId: Int): JSONObject {
    if (displayId <= 0 || target.optString("image").isNotBlank()) return target
    runCatching {
        val snap = privilege?.execute(JSONObject().put("op", "preview").put("since", -1).put("fresh", true)) ?: return@runCatching
        val img = snap.optString("image")
        if (img.isNotBlank()) {
            target.put("image", img)
            if (snap.optInt("width", 0) > 0) target.put("width", snap.optInt("width"))
            if (snap.optInt("height", 0) > 0) target.put("height", snap.optInt("height"))
            if (snap.optInt("image_width", 0) > 0) target.put("image_width", snap.optInt("image_width"))
            if (snap.optInt("image_height", 0) > 0) target.put("image_height", snap.optInt("image_height"))
            val fh = snap.optString("frameHash")
            if (fh.isNotBlank()) target.put("frameHash", fh)
        }
    }
    return target
}

internal fun RootPhoneHarness.readUiTreeForDisplay(
    displayId: Int,
    dispW: Int,
    dispH: Int,
    forceRefresh: Boolean,
    waitMs: Long = 750L
): JSONObject {
    val privUid = privilege?.getGrantedUid()?.takeIf { it >= 0 } ?: 0
    if (!PulseAccessibilityService.isConnected()) {
        runCatching { privilege?.probe() }
        val waitDeadline = SystemClock.uptimeMillis() + 300L
        while (!PulseAccessibilityService.isConnected() && SystemClock.uptimeMillis() < waitDeadline) {
            try { Thread.sleep(60L) } catch (ignored: InterruptedException) { break }
        }
    }
    PulseAccessibilityService.dumpDisplayTree(displayId, dispW, dispH, forceRefresh, waitMs = waitMs)?.let {
        return attachLatestDisplayFrame(it.put("uid", privUid).put("source", "accessibility_windows"), displayId)
    }
    val adbTree = runCatching {
        privilege?.execute(JSONObject().put("op", "ui_tree").put("display_id", displayId))
    }.getOrNull()
    if (adbTree != null && adbTree.optBoolean("ok", false)) {
        val rawNodes = adbTree.optJSONArray("nodes") ?: JSONArray()
        val uiNodes = mutableListOf<PulseAccessibilityService.CachedNode>()
        for (i in 0 until rawNodes.length()) {
            val item = rawNodes.optJSONObject(i) ?: continue
            val bounds = item.optJSONObject("bounds")
            val cx = bounds?.optInt("cx", item.optInt("center_x", item.optInt("cx", 0)))
                ?: item.optInt("center_x", item.optInt("cx", 0))
            val cy = bounds?.optInt("cy", item.optInt("center_y", item.optInt("cy", 0)))
                ?: item.optInt("center_y", item.optInt("cy", 0))
            val left = bounds?.optInt("left", item.optInt("left", (cx - 20).coerceAtLeast(0)))
                ?: item.optInt("left", (cx - 20).coerceAtLeast(0))
            val top = bounds?.optInt("top", item.optInt("top", (cy - 20).coerceAtLeast(0)))
                ?: item.optInt("top", (cy - 20).coerceAtLeast(0))
            val right = bounds?.optInt("right", item.optInt("right", cx + 20))
                ?: item.optInt("right", cx + 20)
            val bottom = bounds?.optInt("bottom", item.optInt("bottom", cy + 20))
                ?: item.optInt("bottom", cy + 20)
            uiNodes += PulseAccessibilityService.CachedNode(
                ref = item.optString("ref", "@e${i + 1}"),
                role = item.optString("role", "View"),
                text = item.optString("text", item.optString("label", "")),
                desc = item.optString("desc", item.optString("contentDesc", "")),
                hint = item.optString("hint", ""),
                resourceId = item.optString("id", item.optString("resourceId", "")),
                pkg = item.optString("pkg", item.optString("package", "")),
                bounds = android.graphics.Rect(left, top, right, bottom),
                centerX = cx,
                centerY = cy,
                clickable = item.optBoolean("clickable", false),
                editable = item.optBoolean("editable", false),
                scrollable = item.optBoolean("scrollable", false),
                checkable = item.optBoolean("checkable", false),
                checked = item.optBoolean("checked", false)
            )
        }
        val pkg = adbTree.optString("package", "")
        val snap = PulseAccessibilityService.updateSnapshotFromExternal(displayId, uiNodes, pkg, dispW, dispH, layoutStable = true)
        return attachLatestDisplayFrame(
            PulseAccessibilityService.snapshotToJson(snap, fromCache = false)
                .put("uid", adbTree.optInt("uid", privUid))
                .put("source", adbTree.optString("source", "uiautomator_dump")),
            displayId
        )
    }
    val emptySnap = PulseAccessibilityService.updateSnapshotFromExternal(displayId, emptyList(), "", dispW, dispH, layoutStable = false)
    return attachLatestDisplayFrame(
        PulseAccessibilityService.snapshotToJson(emptySnap, fromCache = false)
            .put("uid", privUid)
            .put("source", "empty"),
        displayId
    )
}
