package dev.bubuki.proactive

import android.content.ContentValues
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.os.SystemClock
import android.provider.MediaStore
import android.util.Base64
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/**
 * [Module] PulseActivityMedia
 * [Responsibility]
 * 多媒體、雲端桌面流與系統權限交互領域服務：封裝文件附件選擇與上傳、系統分享 Intent 接收與解析、
 * 雲端桌面 (Cloud Desktop) 畫面拉取與遠程鍵鼠觸控注入、手機獨立虛擬屏懸浮小窗啟動與無障礙權限引導、
 * 多端節點身份切換 (`toggleNodeSource`)、全局/Bot 快捷菜單構建，以及貼紙表情包緩存與加載。
 *
 * [Public API]
 * - `NativePulseModel.chooseAttachment()`, `documentPicked(...)`, `handleIncomingShareIntent(...)`, `clearAttachments()`
 * - `NativePulseModel.openCloudDesktop()`, `refreshCloudDesktopFrame(...)`, `cloudDesktopNavigate(...)`, `cloudDesktopClick(...)`, `cloudDesktopScroll(...)`, `cloudDesktopLaunchApp(...)`, `cloudDesktopType(...)`, `cloudDesktopKey(...)`
 * - `NativePulseModel.launchFloatingVirtualDisplay()`, `openAccessibilitySettings()`, `connectRootVirtualDisplay()`, `requestPhonePermission(...)`
 * - `NativePulseModel.newMenu()`, `botMenu(...)`, `actionsMenu()`, `toggleNodeSource()`
 * - `NativePulseModel.loadStickerBitmap(...)`, `fetchStickerPacks()`, `toggleStickerPack(...)`
 */

internal fun NativePulseModel.dismissCloudDesktopPip() {
    ui = ui.copy(cloudDesktopPipVisible = false)
}

internal fun NativePulseModel.showCloudDesktopPip() {
    ui = ui.copy(cloudDesktopPipVisible = true)
    scope.launch { refreshCloudDesktopFrame(forceFull = true) }
}

internal fun parseCloudActiveWindow(res: JSONObject): String {
    val winObj = res.optJSONObject("active_window")
    if (winObj != null) return winObj.optString("title").trim()
    val raw = res.optString("active_window").trim()
    if (raw.isNotBlank() && raw != "null" && !raw.startsWith("{")) return raw
    return res.optString("title").takeIf { it.isNotBlank() && it != "null" }?.trim().orEmpty()
}

internal fun parseCloudTaskRunning(res: JSONObject): Boolean? {
    if (!res.has("job") && !res.has("step_running") && !res.has("running")) return null
    val jobObj = res.optJSONObject("job")
    val jState = jobObj?.optString("state")?.lowercase().orEmpty()
    val jobRunning = jobObj != null && jState !in listOf("done", "failed", "stopped", "interrupted", "idle")
    return res.optBoolean("step_running", res.optBoolean("running", jobRunning)) || jobRunning
}

internal fun formatCloudQuota(q: JSONObject?): String {
    if (q == null) return ""
    val memLim = q.optInt("memory_limit_mb", 0)
    val memUsed = q.optInt("memory_used_mb", 0)
    val diskLim = q.optDouble("disk_limit_gb", -1.0)
    val diskUsed = q.optDouble("disk_used_gb", -1.0)
    if (memLim > 0 && diskLim > 0) {
        val cpus = q.optInt("cpus", 2).coerceAtLeast(1)
        val memLimStr = if (memLim >= 1024 && memLim % 1024 == 0) "${memLim / 1024} GB"
        else if (memLim >= 1024) String.format(Locale.US, "%.1f GB", memLim / 1024.0)
        else "${memLim} MB"
        return "$cpus vCPU · RAM ${memUsed}MB/$memLimStr · 磁盘 ${String.format(Locale.US, "%.1f", diskUsed)}/${String.format(Locale.US, "%.0f", diskLim)} GB"
    }
    return q.optString("profile").trim()
}

private fun hasCloudFrameImage(res: JSONObject): Boolean =
    res.optString("image").isNotBlank() || res.optString("frame").isNotBlank() || res.optString("screenshot").isNotBlank()

internal fun NativePulseModel.openCloudDesktop() {
    dismissMenu()
    ui = ui.copy(activePanel = "cloud_desktop", cloudDesktopLoading = ui.cloudDesktopFrame == null)
    startCloudDesktopStreamLoop(forceFull = true)
    scope.launch {
        runCatching { applyCloudDesktopMeta(request("GET", "/api/computer/status")) }
    }
}

internal fun NativePulseModel.startCloudDesktopStreamLoop(forceFull: Boolean = false) {
    cloudStreamJob?.cancel()
    cloudStreamJob = scope.launch {
        refreshCloudDesktopFrame(forceFull = forceFull)
        var round = 0
        while (isActive && active && ui.activePanel == "cloud_desktop") {
            val isBusy = ui.workingBot != null || ui.streamBotId != null || ui.busyBot != null || ui.cloudDesktopTaskRunning
            delay(if (ui.cloudDesktopUserControl) 750L else if (isBusy) 1600L else 2400L)
            if (active && ui.activePanel == "cloud_desktop" && !ui.cloudDesktopLoading) {
                refreshCloudDesktopFrame(forceFull = false)
                round++
                if (round % 3 == 0) {
                    runCatching { applyCloudDesktopMeta(request("GET", "/api/computer/status")) }
                }
            }
        }
    }
}

internal fun NativePulseModel.applyCloudDesktopMeta(res: JSONObject) {
    val ctrl = if (res.has("control")) res.optString("control").trim().equals("user", ignoreCase = true)
    else if (res.has("user_control")) res.optBoolean("user_control", ui.cloudDesktopUserControl)
    else ui.cloudDesktopUserControl
    val win = parseCloudActiveWindow(res).ifBlank { ui.cloudDesktopActiveWindow }
    val running = parseCloudTaskRunning(res) ?: ui.cloudDesktopTaskRunning
    val quotaText = formatCloudQuota(res.optJSONObject("quota")).ifBlank { ui.cloudDesktopQuota }
    val avail = res.optBoolean("available", false)
    ui = ui.copy(
        cloudDesktopUrl = res.optString("url").takeIf { it.isNotBlank() && it != "null" } ?: ui.cloudDesktopUrl,
        cloudDesktopTitle = res.optString("title").takeIf { it.isNotBlank() && it != "null" } ?: ui.cloudDesktopTitle,
        cloudDesktopUserControl = ctrl,
        cloudDesktopActiveWindow = win,
        cloudDesktopTaskRunning = running,
        cloudDesktopQuota = quotaText,
        cloudDesktopConfigured = ui.cloudDesktopConfigured || avail
    )
}

internal suspend fun NativePulseModel.applyCloudDesktopPacket(res: JSONObject) {
    if (res.has("ok") && !res.optBoolean("ok", true)) toast(res.optString("error").trim().ifBlank { "云电脑操作未生效" }.take(80))
    val unchanged = res.optBoolean("unchanged", false)
    val newHash = res.optString("hash", ui.cloudDesktopHash)
    val rawImg = res.optString("image").ifBlank { res.optString("frame") }.ifBlank { res.optString("screenshot") }.trim()
    val b64 = if (rawImg.contains(",")) rawImg.substringAfter(",") else rawImg
    val bitmap = if (!unchanged && b64.isNotBlank()) withContext(Dispatchers.Default) {
        val bytes = Base64.decode(b64, Base64.DEFAULT)
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap()
    } else ui.cloudDesktopFrame
    applyCloudDesktopMeta(res)
    ui = ui.copy(
        cloudDesktopFrame = bitmap ?: ui.cloudDesktopFrame,
        cloudDesktopHash = newHash.ifBlank { ui.cloudDesktopHash },
        cloudDesktopLoading = false
    )
}

internal suspend fun NativePulseModel.refreshCloudDesktopFrame(forceFull: Boolean = false) {
    val now = SystemClock.uptimeMillis()
    if (!forceFull && now - lastCloudFrameRefreshAtMs < (if (ui.cloudDesktopUserControl) 550L else 1500L)) return
    lastCloudFrameRefreshAtMs = now
    runCatching {
        val q = JSONObject().put("quality", 60)
        if (!forceFull && ui.cloudDesktopHash.isNotBlank()) {
            q.put("since", ui.cloudDesktopHash)
        }
        val res = request("GET", "/api/computer/frame", query = q)
        applyCloudDesktopPacket(res)
    }.onFailure { err ->
        ui = ui.copy(cloudDesktopLoading = false)
        if (forceFull && ui.activePanel == "cloud_desktop") {
            toast("云电脑画面加载失败：${err.message?.take(60) ?: "请检查网络或云桌面服务"}")
        }
    }
}

internal fun NativePulseModel.cloudDesktopTakeover() {
    val prev = ui.cloudDesktopUserControl
    ui = ui.copy(cloudDesktopUserControl = true, cloudDesktopLoading = true)
    perform {
        try {
            val res = request("POST", "/api/computer/takeover")
            applyCloudDesktopPacket(res)
            if (!res.has("control")) ui = ui.copy(cloudDesktopUserControl = true)
            if (!hasCloudFrameImage(res)) refreshCloudDesktopFrame(forceFull = true)
            toast("已获取云电脑控制权")
        } catch (e: Exception) {
            ui = ui.copy(cloudDesktopUserControl = prev, cloudDesktopLoading = false)
            throw e
        }
    }
}

internal fun NativePulseModel.cloudDesktopHandback() {
    val prev = ui.cloudDesktopUserControl
    ui = ui.copy(cloudDesktopUserControl = false, cloudDesktopLoading = true)
    perform {
        try {
            val res = request("POST", "/api/computer/handback")
            applyCloudDesktopPacket(res)
            if (!res.has("control")) ui = ui.copy(cloudDesktopUserControl = false)
            if (!hasCloudFrameImage(res)) refreshCloudDesktopFrame(forceFull = true)
            toast("已交还控制权给 AI")
        } catch (e: Exception) {
            ui = ui.copy(cloudDesktopUserControl = prev, cloudDesktopLoading = false)
            throw e
        }
    }
}

internal fun NativePulseModel.cloudDesktopStop() {
    val targetBot = ui.cloudDesktopLiveBotId ?: ui.workingBot ?: ui.streamBotId ?: ui.busyBot
    if (targetBot != null) {
        stop(targetBot)
    }
    ui = ui.copy(cloudDesktopTaskRunning = false, cloudDesktopLoading = true)
    perform {
        try {
            val res = request("POST", "/api/computer/stop")
            applyCloudDesktopPacket(res)
            if (!hasCloudFrameImage(res)) refreshCloudDesktopFrame(forceFull = true)
            toast("已停止云电脑当前任务")
        } finally {
            ui = ui.copy(cloudDesktopLoading = false, cloudDesktopTaskRunning = false)
        }
    }
}

internal fun NativePulseModel.cloudDesktopNavigate(url: String) {
    val clean = url.trim().let { if (it.isNotBlank() && !it.startsWith("http")) "https://$it" else it }
    if (clean.isBlank()) return
    ui = ui.copy(cloudDesktopLoading = true, cloudDesktopUrl = clean)
    perform {
        try {
            val res = request("POST", "/api/computer/navigate", JSONObject().put("url", clean).put("include_frame", true))
            applyCloudDesktopPacket(res)
            if (!hasCloudFrameImage(res)) refreshCloudDesktopFrame(forceFull = true)
        } finally {
            ui = ui.copy(cloudDesktopLoading = false)
        }
    }
}

internal fun NativePulseModel.cloudDesktopClick(normX: Float, normY: Float, kind: String = "click") {
    val clampedX = normX.coerceIn(0f, 1f)
    val clampedY = normY.coerceIn(0f, 1f)
    val x = (clampedX * 1279f).toInt()
    val y = (clampedY * 799f).toInt()
    ui = ui.copy(cloudDesktopTapNorm = clampedX to clampedY)
    perform {
        try {
            val res = request("POST", "/api/computer/input", JSONObject().put("kind", kind).put("x", x).put("y", y).put("include_frame", true))
            applyCloudDesktopPacket(res)
            if (!hasCloudFrameImage(res)) refreshCloudDesktopFrame(forceFull = true)
        } finally {
            delay(220)
            ui = ui.copy(cloudDesktopTapNorm = null)
        }
    }
}

internal fun NativePulseModel.cloudDesktopScroll(normX: Float, normY: Float, direction: String, amount: Int = 4) {
    val x = (normX.coerceIn(0f, 1f) * 1279f).toInt()
    val y = (normY.coerceIn(0f, 1f) * 799f).toInt()
    perform {
        val res = request("POST", "/api/computer/input", JSONObject().put("kind", "scroll").put("x", x).put("y", y).put("direction", direction).put("amount", amount).put("include_frame", true))
        applyCloudDesktopPacket(res)
        if (!hasCloudFrameImage(res)) refreshCloudDesktopFrame(forceFull = true)
    }
}

internal fun NativePulseModel.cloudDesktopLaunchApp(appKey: String, argText: String = "") {
    ui = ui.copy(cloudDesktopLoading = true)
    perform {
        try {
            val payload = JSONObject().put("kind", "launch_app").put("app", appKey).put("include_frame", true)
            if (argText.isNotBlank()) payload.put("text", argText)
            val res = request("POST", "/api/computer/input", payload)
            applyCloudDesktopPacket(res)
            if (!hasCloudFrameImage(res)) {
                refreshCloudDesktopFrame(forceFull = true)
            } else if (!res.optString("launched").endsWith("_focused")) {
                delay(320)
                refreshCloudDesktopFrame(forceFull = true)
            }
        } finally {
            ui = ui.copy(cloudDesktopLoading = false)
        }
    }
}

internal fun NativePulseModel.cloudDesktopType(text: String, submit: Boolean = false) {
    if (text.isEmpty()) return
    perform {
        val payload = if (submit) {
            val steps = JSONArray()
                .put(JSONObject().put("kind", "type").put("text", text))
                .put(JSONObject().put("kind", "key").put("key", "Return"))
            JSONObject().put("kind", "batch").put("steps", steps).put("include_frame", true)
        } else {
            JSONObject().put("kind", "type").put("text", text).put("include_frame", true)
        }
        val res = request("POST", "/api/computer/input", payload)
        applyCloudDesktopPacket(res)
        if (!hasCloudFrameImage(res)) refreshCloudDesktopFrame(forceFull = true)
    }
}

internal fun NativePulseModel.cloudDesktopKey(key: String) {
    val isCombo = key.contains("+")
    val mappedKey = when (key) {
        "Backspace" -> "BackSpace"
        "Enter" -> "Return"
        "PageDown" -> "Page_Down"
        "PageUp" -> "Page_Up"
        else -> key
    }
    perform {
        val res = request("POST", "/api/computer/input", JSONObject().put("kind", if (isCombo) "hotkey" else "key").put("key", mappedKey).put("include_frame", true))
        applyCloudDesktopPacket(res)
        if (!hasCloudFrameImage(res)) refreshCloudDesktopFrame(forceFull = true)
    }
}

internal fun NativePulseModel.toggleNodeSource() {
    dismissMenu()
    val cur = SecureTransport.getNodeSource(activity)
    val next = if (cur == "phone2") "phone" else "phone2"
    val label = if (next == "phone2") "安卓二 (phone2)" else "安卓一 (phone)"
    perform {
        val appCtx = activity.applicationContext
        withContext(Dispatchers.IO) {
            prefs = SecurePrefs.update(appCtx) { s ->
                s.put("nodeSource", next)
            }
            SecureTransport.invalidateCache()
        }
        remoteMessages = emptyList()
        optimisticList.clear()
        confirmedSyncSeq = 0L
        ui = ui.copy(messages = emptyList(), streamBotId = null, workingBot = null, streamText = "", streamThought = "")
        runCatching {
            if (ui.backgroundService) {
                AgentForegroundService.reconnectOrStart(activity)
            } else {
                activity.stopService(Intent(activity, AgentForegroundService::class.java))
            }
        }
        toast("已切换当前端标识为：$label（多端对话完全独立隔离）")
        refresh(force = true)
    }
}

internal fun NativePulseModel.newMenu() {
    val curSrc = SecureTransport.getNodeSource(activity)
    val nodeLabel = if (curSrc == "phone2") "安卓二 (平板/副机)" else "安卓一 (主力手机)"
    val vdState = RootPhoneHarness.changes.value
    val entries = mutableListOf(
        PulseMenuItem("创建新 Bot", "创建专属角色、指令与独立模型", "plus") { openBotEditor(null) },
        PulseMenuItem("切换多端节点身份 (当前：$nodeLabel)", "点击在「安卓一」与「安卓二」之间切换，各端对话独立不串台", "spark") { toggleNodeSource() },
        PulseMenuItem("手机虚拟屏 (桌面悬浮小窗)", "创建并弹出浮在系统桌面上的独立小窗", "screen") { launchFloatingVirtualDisplay() }
    )
    if (vdState.displayCreated) {
        entries += PulseMenuItem("销毁手机虚拟屏 (#${vdState.displayId})", "彻底释放后台虚拟屏并关闭悬浮小窗，不干扰主屏应用", "close") { destroyVirtualDisplay() }
    }
    entries += listOf(
        PulseMenuItem("云端桌面 (Cloud Desktop)", "查看云端浏览器实时画面与触控接管", "cloud") { openCloudDesktop() },
        PulseMenuItem("透明记忆库 (Obsidian Vault)", "查看或修改 Markdown 记忆与画像", "vault") { openVault("INDEX.md") },
        PulseMenuItem("设置中心与检查更新", "日夜主题切换、API 模型配置与一键更新客户端", "settings") { dismissMenu(); openSettings() },
        PulseMenuItem("一键下载最新客户端更新", "直接从云端拉取并安装最新 APK (当前 v${BuildConfig.VERSION_NAME})", "spark") { dismissMenu(); downloadLatestApk() }
    )
    menu("DSB 工作台 · $nodeLabel", entries)
}

internal fun NativePulseModel.botMenu(id: String = ui.botId) {
    val b = bot(id) ?: return
    val (curTok, threshTok, msgCnt) = contextStatsFor(id)
    val (cachedTok, cachePct) = contextCacheFor(id)
    val pct = ((curTok.toFloat() / threshTok.coerceAtLeast(1)) * 100).toInt()
    val entries = mutableListOf<PulseMenuItem>()
    if (!ui.chatOpen || ui.botId != id) entries += PulseMenuItem("打开对话", icon = "chat") { open(id) }
    entries += listOf(
        PulseMenuItem(
            "立即压缩上下文 (${formatTokenCount(curTok)} / ${formatTokenCount(threshTok)} · $pct%)",
            "缓存命中 ${formatTokenCount(cachedTok)} ($cachePct%) · 已积攒 $msgCnt 条消息，点击浓缩归档",
            "spark"
        ) { compactBotContext(id) },
        PulseMenuItem("自定义上下文自动压缩阈值", "当前达到 ${formatTokenCount(threshTok)} Tokens 时自动压缩", "settings") { openSettings() },
        PulseMenuItem("编辑 Bot 设定与模型", "当前模型：${b.model.ifBlank { ui.defaultModel.ifBlank { "跟随全局默认" } }}", "settings") { openBotEditor(id) },
        PulseMenuItem(if (ui.primary == id) "当前为主助理" else "设为主助理", icon = "spark") { setPrimaryBot(id) },
        PulseMenuItem(if (b.pinned) "取消置顶" else "置顶到首页", icon = "pin") { placement(id, "pinned", !b.pinned) },
        PulseMenuItem(if (b.hidden) "在首页显示" else "隐藏此 Bot", "保留对话、记忆和协作能力", "hide") { placement(id, "hidden", !b.hidden) },
        PulseMenuItem("透明记忆库", icon = "vault") { openVault("INDEX.md") }
    )
    if (ui.bots.size > 1 && ui.primary != id) {
        entries += PulseMenuItem("删除此 Bot", "移除该助手", "close") { deleteBot(id) }
    }
    menu(b.name, entries)
}

internal fun NativePulseModel.actionsMenu() {
    val (curTok, threshTok, msgCnt) = contextStatsFor(ui.botId)
    val (cachedTok, cachePct) = contextCacheFor(ui.botId)
    val pct = ((curTok.toFloat() / threshTok.coerceAtLeast(1)) * 100).toInt()
    val vdState = RootPhoneHarness.changes.value
    val entries = mutableListOf(
        PulseMenuItem(
            "压缩上下文 (${formatTokenCount(curTok)} / ${formatTokenCount(threshTok)} · $pct%)",
            "缓存命中 ${formatTokenCount(cachedTok)} ($cachePct%) · 已积攒 $msgCnt 条对话，点击浓缩归档",
            "spark"
        ) { compactBotContext(ui.botId) },
        PulseMenuItem(
            "自定义上下文压缩阈值",
            "当前满 ${formatTokenCount(threshTok)} 自动压缩，点此修改设置",
            "settings"
        ) { openSettings() },
        PulseMenuItem("上传文件到工作区", "供当前 Bot 读取、编辑或运行脚本", "file") { chooseAttachment() },
        PulseMenuItem("弹出手机虚拟屏悬浮小窗", "在桌面上悬浮独立虚拟屏，点按展开触控", "screen") {
            val raw = ui.draft.trim()
            if (raw.isNotBlank()) sendPhone(raw) else launchFloatingVirtualDisplay()
        }
    )
    if (vdState.displayCreated) {
        entries += PulseMenuItem("销毁手机虚拟屏 (#${vdState.displayId})", "彻底关闭并释放虚拟屏与悬浮小窗", "close") { destroyVirtualDisplay() }
    }
    entries += PulseMenuItem("手机工作区权限与诊断", "手动检测 Root 权限与底层隔离状态", "settings") {
        activity.startActivity(Intent(activity, NativePhoneActivity::class.java))
    }
    entries += PulseMenuItem("连接与虚拟屏实时自检", "查看长连代次/RTT 延迟、挂起 RPC 与小窗状态", "spark") {
        dismissMenu()
        openHealthDialog()
    }
    entries += listOf(
        PulseMenuItem("打开云端桌面", "查看云端浏览器实时画面或接管", "cloud") { secondary("computer") },
        PulseMenuItem("触发好奇心主动搭话", "让主助理结合记忆投递实用情报或追问", "spark") { job("curiosity") },
        PulseMenuItem("打开透明记忆库", "查看或编辑 memory_vault/*.md", "vault") { openVault("INDEX.md") },
        PulseMenuItem("编辑当前 Bot 模型与设定", icon = "settings") { openBotEditor(ui.botId) },
        PulseMenuItem("设置中心与检查更新", "切换日间/夜间模式、API 配置或更新 APK", "settings") { dismissMenu(); openSettings() },
        PulseMenuItem("免打扰 1 小时", icon = "moon") { job("snooze") }
    )
    menu("工作台与工具", entries)
}

internal fun NativePulseModel.openHealthDialog() {
    ui = ui.copy(healthDialogOpen = true)
}

internal fun NativePulseModel.closeHealthDialog() {
    ui = ui.copy(healthDialogOpen = false)
}

internal fun NativePulseModel.destroyVirtualDisplay() {
    dismissMenu()
    AgentForegroundService.onUserForceClosedVirtualDisplay(activity)
    FloatingVirtualDisplay.hide()
    perform {
        try {
            withContext(Dispatchers.IO) {
                RootPhoneHarness.initialize(activity)
                RootPhoneHarness.command(
                    activity,
                    JSONObject()
                        .put("op", "release")
                        .put("destroy", true)
                        .put("user_close", true)
                        .put("manual", true)
                )
            }
            toast("已彻底销毁手机虚拟屏")
        } catch (e: Exception) {
            toast("销毁虚拟屏失败：${PulseErrors.safeError(e)}")
        }
    }
}

internal fun NativePulseModel.launchFloatingVirtualDisplay() {
    dismissMenu()
    perform {
        try {
            withContext(Dispatchers.IO) {
                RootPhoneHarness.initialize(activity)
                var st = RootPhoneHarness.status(activity)
                if (!st.optBoolean("privileged", st.optBoolean("root"))) {
                    RootPhoneHarness.command(activity, JSONObject().put("op", "connect").put("manual", true))
                    st = RootPhoneHarness.status(activity)
                }
                val liveSt = runCatching {
                    RootPhoneHarness.privilege?.execute(JSONObject().put("op", "status"))
                }.getOrNull()
                val liveCreated = liveSt?.optBoolean("created", false) == true && liveSt.optInt("displayId", -1) > 0
                if (!liveCreated) {
                    RootPhoneHarness.command(activity, JSONObject().put("op", "create").put("manual", true))
                }
                runCatching { RootPhoneHarness.command(activity, JSONObject().put("op", "apps").put("manual", true)) }
            }
            if (!FloatingVirtualDisplay.canOverlay(activity)) {
                FloatingVirtualDisplay.requestOverlayPermission(activity)
            }
            FloatingVirtualDisplay.show(activity, forceExpand = false)
            toast(if (FloatingVirtualDisplay.canOverlay(activity)) "虚拟屏悬浮小窗已开启，退回桌面也会浮在屏幕上。" else "虚拟屏已启动（开启悬浮窗权限后可浮在系统桌面上）。")
        } catch (e: Exception) {
            if (!FloatingVirtualDisplay.canOverlay(activity)) {
                FloatingVirtualDisplay.requestOverlayPermission(activity)
            }
            toast("虚拟屏启动提示：${PulseErrors.safeError(e)}")
        }
    }
}

internal fun NativePulseModel.openUnifiedNotificationSettings() {
    val hasNotif = android.os.Build.VERSION.SDK_INT < 33 ||
        activity.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) == android.content.pm.PackageManager.PERMISSION_GRANTED
    if (!hasNotif && android.os.Build.VERSION.SDK_INT >= 33) {
        activity.requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 15)
        return
    }
    if (!FloatingVirtualDisplay.canOverlay(activity)) {
        FloatingVirtualDisplay.requestOverlayPermission(activity)
        return
    }
    runCatching {
        activity.startActivity(
            android.content.Intent(android.provider.Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, activity.packageName)
        )
    }
}

internal fun NativePulseModel.chooseAttachment() {
    if (!ensurePaired() || pickerBot != null || ui.busyBot != null) return
    chooseDocuments()
}

internal fun NativePulseModel.chooseMediaGallery() {
    if (!ensurePaired() || pickerBot != null || ui.busyBot != null) return
    pickerBot = ui.botId
    pickerSource = "workspace"
    try { activity.pickVisualMedia() } catch (e: Exception) { pickerBot = null; pickerSource = null; toast(PulseErrors.safeError(e)) }
}

internal fun NativePulseModel.chooseDocuments() {
    if (!ensurePaired() || pickerBot != null || ui.busyBot != null) return
    pickerBot = ui.botId
    pickerSource = "workspace"
    try { activity.pickDocument() } catch (e: Exception) { pickerBot = null; pickerSource = null; toast(PulseErrors.safeError(e)) }
}

internal fun NativePulseModel.captureQuickPhoto() {
    if (!ensurePaired() || pickerBot != null || ui.busyBot != null) return
    pickerBot = ui.botId
    pickerSource = "workspace"
    try {
        activity.captureQuickPhoto()
    } catch (e: Throwable) {
        pickerBot = null
        pickerSource = null
        toast("无法打开系统相机：${e.message ?: "请检查相机应用"}")
    }
}

internal fun NativePulseModel.restorePicker(state: Bundle?) {
    pickerBot = state?.getString("upload_bot")
    pickerSource = state?.getString("upload_source")
}

internal fun NativePulseModel.savePicker(state: Bundle) {
    pickerBot?.let { state.putString("upload_bot", it); state.putString("upload_source", pickerSource) }
}

private fun NativePulseModel.refreshUploadingFlag() {
    val active = ui.pendingUploads.any { it.error.isEmpty() }
    if (ui.uploading != active) ui = ui.copy(uploading = active)
}

internal fun NativePulseModel.documentsPicked(uris: List<Uri>) {
    val id = pickerBot
    val source = pickerSource
    pickerBot = null
    pickerSource = null
    if (uris.isEmpty() || id == null || source == null) return
    enqueueUploadUris(id, source, uris)
}

internal fun NativePulseModel.documentPicked(uri: Uri?) {
    documentsPicked(if (uri != null) listOf(uri) else emptyList())
}

private fun NativePulseModel.enqueueUploadUris(targetBot: String, source: String, uris: List<Uri>, prebuiltPending: List<PulsePendingUpload>? = null) {
    val batch = uris.distinct().take(8)
    if (batch.isEmpty() && prebuiltPending.isNullOrEmpty()) return
    perform {
        val pendingItems = prebuiltPending ?: withContext(Dispatchers.IO) {
            batch.mapIndexed { idx, uri ->
                val meta = CloudFiles.probeUriMeta(activity, uri)
                val name = meta.optString("name", "attachment").ifBlank { "attachment" }
                val bytes = meta.optLong("bytes", 0L)
                val isImg = meta.optBoolean("isImage", CloudFiles.isImageFilename(name))
                val thumb = if (isImg) CloudFiles.decodeThumbnailBitmap(activity, uri, 192)?.asImageBitmap() else null
                PulsePendingUpload(
                    id = "up-${SystemClock.uptimeMillis()}-$idx-${name.hashCode()}",
                    name = name,
                    bytes = bytes,
                    isImage = isImg,
                    progress = 0.04f,
                    uri = uri,
                    thumb = thumb
                )
            }
        }
        val existingIds = ui.pendingUploads.map { it.id }.toSet()
        val toAdd = pendingItems.filter { it.id !in existingIds }
        ui = ui.copy(
            pendingUploads = ui.pendingUploads + toAdd,
            uploading = true
        )
        var succeeded = 0
        for (item in pendingItems) {
            val u = item.uri ?: continue
            var lastUiTick = 0L
            try {
                val res = withContext(Dispatchers.IO) {
                    CloudFiles.upload(activity, u, source) { pct, _, _ ->
                        val nowMs = SystemClock.uptimeMillis()
                        if (pct >= 0.99f || nowMs - lastUiTick >= 90L) {
                            lastUiTick = nowMs
                            scope.launch {
                                ui = ui.copy(
                                    pendingUploads = ui.pendingUploads.map {
                                        if (it.id == item.id) it.copy(progress = pct, error = "") else it
                                    }
                                )
                            }
                        }
                    }
                }
                val file = PulseAttachment.from(res.put("source", source))
                synchronized(draftLock) {
                    val cur = attachments(targetBot)
                    if (cur.none { it.source == file.source && it.path == file.path }) {
                        attachmentDrafts[targetBot] = cur + file
                    }
                }
                localRevision++
                val nextThumbs = if (item.thumb != null) ui.attachmentThumbs + (file.path to item.thumb) else ui.attachmentThumbs
                ui = ui.copy(
                    attachments = if (ui.botId == targetBot) attachments(targetBot) else ui.attachments,
                    pendingUploads = ui.pendingUploads.filterNot { it.id == item.id },
                    attachmentThumbs = nextThumbs
                )
                refreshUploadingFlag()
                succeeded++
            } catch (err: Exception) {
                val errTxt = PulseErrors.safeError(err)
                ui = ui.copy(
                    pendingUploads = ui.pendingUploads.map {
                        if (it.id == item.id) it.copy(error = errTxt, progress = 0f) else it
                    }
                )
                refreshUploadingFlag()
            }
        }
        if (succeeded > 0) {
            withContext(Dispatchers.IO) { persistDrafts().get() }
            if (pendingItems.size == 1 && ui.pendingUploads.none { it.id == pendingItems[0].id }) {
                toast("已附加：${pendingItems[0].name}")
            }
        }
        refreshUploadingFlag()
    }
}

internal fun NativePulseModel.retryPendingUpload(uploadId: String) {
    val item = ui.pendingUploads.firstOrNull { it.id == uploadId } ?: return
    if (item.error.isBlank()) return
    val u = item.uri ?: return
    ui = ui.copy(
        pendingUploads = ui.pendingUploads.map { if (it.id == uploadId) it.copy(error = "", progress = 0.05f) else it },
        uploading = true
    )
    enqueueUploadUris(ui.botId, "workspace", listOf(u), prebuiltPending = listOf(item.copy(error = "", progress = 0.05f)))
}

internal fun NativePulseModel.dismissPendingUpload(uploadId: String) {
    ui = ui.copy(pendingUploads = ui.pendingUploads.filterNot { it.id == uploadId })
    refreshUploadingFlag()
}

internal fun NativePulseModel.removeAttachment(attachment: PulseAttachment) {
    synchronized(draftLock) {
        attachmentDrafts[ui.botId] = attachments(ui.botId).filterNot { it.path == attachment.path && it.source == attachment.source }
    }
    localRevision++
    ui = ui.copy(
        attachments = attachments(ui.botId),
        attachmentThumbs = ui.attachmentThumbs - attachment.path
    )
    scope.launch(Dispatchers.IO) { persistDrafts().get() }
}

@Suppress("DEPRECATION")
internal fun NativePulseModel.handleIncomingShareIntent(intent: Intent?) {
    if (intent == null) return
    val act = intent.action ?: return
    if (act != Intent.ACTION_SEND && act != Intent.ACTION_SEND_MULTIPLE && act != Intent.ACTION_PROCESS_TEXT) return
    intent.action = Intent.ACTION_MAIN
    val targetBot = ui.primary.ifBlank { "companion" }
    open(targetBot)
    val sharedTexts = mutableListOf<String>()
    if (act == Intent.ACTION_PROCESS_TEXT) {
        intent.getCharSequenceExtra(Intent.EXTRA_PROCESS_TEXT)?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { sharedTexts.add(it) }
    }
    intent.getStringExtra(Intent.EXTRA_SUBJECT)?.trim()?.takeIf { it.isNotEmpty() }?.let { sharedTexts.add(it) }
    intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { sharedTexts.add(it) }
    if (sharedTexts.isNotEmpty()) {
        val joined = sharedTexts.distinct().joinToString("\n")
        val cur = draft(targetBot).trim()
        setDraft(if (cur.isEmpty()) joined else "$cur\n$joined")
    }
    val uris = mutableListOf<Uri>()
    if (act == Intent.ACTION_SEND) {
        (intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM))?.let { uris.add(it) }
    } else if (act == Intent.ACTION_SEND_MULTIPLE) {
        intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)?.let { uris.addAll(it) }
    }
    if (uris.isEmpty() && intent.clipData != null) {
        val cd = intent.clipData!!
        for (i in 0 until cd.itemCount) {
            cd.getItemAt(i)?.uri?.let { uris.add(it) }
        }
    }
    val distinctUris = uris.distinct().take(6)
    if (distinctUris.isNotEmpty()) {
        enqueueUploadUris(targetBot, "workspace", distinctUris)
    } else if (sharedTexts.isNotEmpty()) {
        toast("已接收分享内容，可直接发送")
    }
}

internal fun NativePulseModel.clearAttachments() {
    val clearedPaths = attachments(ui.botId).asSequence().map { it.path }.toSet()
    synchronized(draftLock) { attachmentDrafts[ui.botId] = emptyList() }
    localRevision++
    ui = ui.copy(
        attachments = emptyList(),
        pendingUploads = emptyList(),
        uploading = false,
        attachmentThumbs = ui.attachmentThumbs - clearedPaths
    )
    scope.launch(Dispatchers.IO) { persistDrafts().get() }
}

internal fun NativePulseModel.openLink(value: String) {
    if (value.startsWith("#vault:")) {
        if (!ensurePaired()) return
        val path = Uri.decode(value.removePrefix("#vault:")).let { if (it.endsWith(".md")) it else "$it.md" }
        openVault(path)
        return
    }
    val target = Uri.parse(value)
    if (target.scheme !in listOf("http", "https") || target.host.isNullOrBlank()) { toast("只支持 HTTP(S) 网页链接。"); return }
    try { activity.startActivity(Intent(Intent.ACTION_VIEW, target)) } catch (e: Exception) { toast("没有可用的浏览器。") }
}

internal fun NativePulseModel.sendPhone(text: String) {
    if (text.length > 6000) { toast("手机任务最多 6000 字，请分成几步交代。"); return }
    val selectedBot = ui.botId
    val originalDraft = ui.draft
    val context = ui.messages.filter { it.botId == selectedBot }.takeLast(12)
    ui = ui.copy(busyBot = selectedBot)
    perform {
        try {
            RootPhoneHarness.submit(activity, selectedBot, text, context)
            if (draft(selectedBot) == originalDraft) {
                synchronized(draftLock) { drafts[selectedBot] = "" }
                localRevision++
                if (ui.botId == selectedBot) ui = ui.copy(draft = "")
                withContext(Dispatchers.IO) { persistDrafts().get() }
            }
            applyPhoneState()
            toast("手机独立工作台已开始执行，可随时查看或停止。")
        } finally {
            ui = ui.copy(busyBot = null)
        }
    }
}

internal fun NativePulseModel.openAccessibilitySettings() {
    runCatching {
        activity.startActivity(Intent(android.provider.Settings.ACTION_ACCESSIBILITY_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        toast("请在无障碍列表中找到「DSB」并开启")
    }.onFailure {
        toast("无法打开系统无障碍设置，请手动前往系统设置 -> 无障碍")
    }
}

internal fun NativePulseModel.connectRootVirtualDisplay() {
    perform {
        val shizuku = ShizukuController.get(activity)
        withContext(Dispatchers.IO) {
            shizuku.refreshState()
            RootPhoneHarness.initialize(activity)
            runCatching { RootPhoneHarness.command(activity, JSONObject().put("op", "connect").put("manual", true)) }
            RootPhoneHarness.status(activity)
        }
        val st = RootPhoneHarness.changes.value
        if (!st.root && !st.privileged && shizuku.isRunningNeedingPermission()) {
            shizuku.requestPermission()
            toast("已调起 Shizuku 授权弹窗，请点允许")
            return@perform
        }
        val msg = when {
            st.privilegeMode == "shizuku" && st.privileged -> "Shizuku 后台虚拟屏已就绪 (uid ${st.uid})"
            st.root -> "Root 后台虚拟屏权限已就绪 (uid 0)"
            st.privileged -> "无线 ADB 后台虚拟屏已就绪 (uid ${st.uid})"
            else -> "请在 Root 管理器、Shizuku（推荐免 Root 无线启动）或无线 ADB 调试中授权 DSB"
        }
        toast(msg)
    }
}

internal fun NativePulseModel.requestPhonePermission(op: String) {
    perform {
        withContext(Dispatchers.IO) {
            RootPhoneHarness.command(activity, JSONObject().put("op", op))
            RootPhoneHarness.status(activity)
        }
    }
}

internal fun resolveWorkspaceMediaUrl(rawPath: String, serverUrl: String): String {
    val clean = rawPath.trim()
    if (clean.isEmpty()) return ""
    if (clean.startsWith("http://", true) || clean.startsWith("https://", true)) return clean
    val base = serverUrl.trimEnd('/')
    val rel = when {
        clean.startsWith("/workspace-media/") || clean.startsWith("/stickers/") || clean.startsWith("/static/") || clean.startsWith("/api/") -> clean
        clean.startsWith("workspace/") -> "/workspace-media/" + clean.removePrefix("workspace/").trimStart('/')
        clean.startsWith("uploads/") -> "/workspace-media/$clean"
        clean.contains("/") -> "/workspace-media/" + clean.trimStart('/')
        else -> "/workspace-media/uploads/$clean"
    }
    return "$base$rel"
}

internal suspend fun NativePulseModel.loadMediaBitmap(rawPath: String): ImageBitmap? {
    val clean = rawPath.trim()
    if (clean.isEmpty()) return null
    val fullUrl = resolveWorkspaceMediaUrl(clean, ui.serverUrl)
    if (fullUrl.isBlank()) return null
    PulseMediaCache.getMemoryCached(fullUrl)?.let { return it }
    val base = ui.serverUrl.trimEnd('/')
    val token = if (base.isNotBlank() && fullUrl.startsWith(base)) {
        withContext(Dispatchers.IO) {
            runCatching { SecurePrefs.readFastConfig(activity).deviceToken.trim() }.getOrDefault("")
        }
    } else ""
    return PulseMediaCache.load(activity, fullUrl, token)
}

internal fun NativePulseModel.loadStickerBitmap(stickerPath: String, onLoaded: (ImageBitmap) -> Unit) {
    val clean = stickerPath.trim()
    if (clean.isEmpty()) return
    val fullUrl = resolveWorkspaceMediaUrl(clean, ui.serverUrl)
    if (fullUrl.isBlank()) return
    PulseMediaCache.getMemoryCached(fullUrl)?.let { onLoaded(it); return }
    scope.launch {
        loadMediaBitmap(clean)?.let { onLoaded(it) }
    }
}

internal fun NativePulseModel.downloadAndOpenWorkspaceFile(
    rawPath: String,
    displayName: String = "",
    onBusyChange: ((Boolean) -> Unit)? = null
) {
    val cleanRel = rawPath.trim()
        .removePrefix("/workspace-media/")
        .removePrefix("workspace/")
        .trimStart('/')
        .let { if (it.contains("/")) it else "uploads/$it" }
    val fname = displayName.ifBlank { cleanRel.substringAfterLast('/') }
    if (cleanRel.isBlank()) return
    onBusyChange?.invoke(true)
    toast("正在获取 $fname…")
    scope.launch(Dispatchers.IO) {
        val result = runCatching { CloudFiles.download(activity, "workspace", cleanRel) }
        withContext(Dispatchers.Main) {
            onBusyChange?.invoke(false)
            result.onSuccess { res ->
                val uriStr = res.optString("uri", "")
                val savedPath = res.optString("path", "")
                val ext = fname.substringAfterLast('.', "").lowercase(Locale.ROOT)
                val mime = when (ext) {
                    "pdf" -> "application/pdf"
                    "txt", "md", "log", "csv" -> "text/plain"
                    "json" -> "application/json"
                    "doc" -> "application/msword"
                    "docx" -> "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    "xls" -> "application/vnd.ms-excel"
                    "xlsx" -> "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    "ppt" -> "application/vnd.ms-powerpoint"
                    "pptx" -> "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                    "zip" -> "application/zip"
                    else -> "*/*"
                }
                val opened = if (uriStr.isNotBlank()) {
                    runCatching {
                        val viewIntent = Intent(Intent.ACTION_VIEW).apply {
                            setDataAndType(Uri.parse(uriStr), mime)
                            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                        }
                        activity.startActivity(viewIntent)
                    }.isSuccess
                } else false
                if (!opened) {
                    toast("已保存到 ${savedPath.ifBlank { "Download/DeepSeek Claw/$fname" }}")
                }
            }.onFailure { err ->
                toast("打开文件失败：${err.message ?: "请检查网络"}")
            }
        }
    }
}

internal fun NativePulseModel.currentDeviceToken(): String =
    runCatching { SecurePrefs.readFast(activity).optString("deviceToken", "").trim() }.getOrDefault("")

internal fun NativePulseModel.fetchStickerPacks() {
    perform {
        runCatching {
            val res = request("GET", "/api/plugins/stickers")
            ui = ui.copy(stickerPacks = res.optJSONArray("packs")?.objects() ?: emptyList())
        }
    }
}

internal fun NativePulseModel.toggleStickerPack(packId: String, enabled: Boolean) {
    perform {
        val res = request("POST", "/api/plugins/stickers/toggle", JSONObject().put("pack_id", packId).put("enabled", enabled))
        ui = ui.copy(stickerPacks = res.optJSONArray("packs")?.objects() ?: ui.stickerPacks)
        toast(if (enabled) "已启用表情包插件" else "已关闭表情包插件")
    }
}

internal fun NativePulseModel.saveBitmapToGallery(bitmap: ImageBitmap, rawPath: String) {
    scope.launch {
        val res = withContext(Dispatchers.IO) {
            runCatching {
                val baseName = rawPath.substringAfterLast('/').substringBefore('?').substringBeforeLast('.')
                    .replace(Regex("[^a-zA-Z0-9._-]"), "_").take(40).ifBlank { "dsb_image" }
                val fileName = "${baseName}_${System.currentTimeMillis() % 100000}.png"
                val cv = ContentValues().apply {
                    put(MediaStore.Images.Media.DISPLAY_NAME, fileName)
                    put(MediaStore.Images.Media.MIME_TYPE, "image/png")
                    put(MediaStore.Images.Media.RELATIVE_PATH, Environment.DIRECTORY_PICTURES + "/DeepSeekBot")
                    put(MediaStore.Images.Media.IS_PENDING, 1)
                }
                val resolver = activity.contentResolver
                val uri = resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, cv)
                    ?: throw IllegalStateException("系统相册拒绝创建文件")
                resolver.openOutputStream(uri)?.use { out ->
                    if (!bitmap.asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, out)) {
                        throw IllegalStateException("图片编码失败")
                    }
                } ?: throw IllegalStateException("无法写入相册文件")
                cv.clear()
                cv.put(MediaStore.Images.Media.IS_PENDING, 0)
                resolver.update(uri, cv, null, null)
                fileName
            }
        }
        res.onSuccess { toast("已保存到系统相册 (Pictures/DeepSeekBot)") }
            .onFailure { toast("保存失败：${it.message ?: "请检查存储权限"}") }
    }
}

internal fun NativePulseModel.shareBitmapImage(bitmap: ImageBitmap, rawPath: String) {
    scope.launch {
        val res = withContext(Dispatchers.IO) {
            runCatching {
                val dir = java.io.File(activity.cacheDir, "shared-media").apply { mkdirs() }
                val baseName = rawPath.substringAfterLast('/').substringBefore('?').substringBeforeLast('.')
                    .replace(Regex("[^a-zA-Z0-9._-]"), "_").take(36).ifBlank { "dsb_image" }
                val file = java.io.File(dir, "${baseName}_${System.currentTimeMillis() % 100000}.png")
                java.io.FileOutputStream(file).use { out ->
                    if (!bitmap.asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, out)) {
                        throw IllegalStateException("图片编码失败")
                    }
                }
                FileProvider.getUriForFile(activity, "${activity.packageName}.avatar-files", file)
            }
        }
        res.onSuccess { uri ->
            runCatching {
                val shareIntent = Intent(Intent.ACTION_SEND).apply {
                    type = "image/png"
                    putExtra(Intent.EXTRA_STREAM, uri)
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                }
                activity.startActivity(Intent.createChooser(shareIntent, "分享图片"))
            }.onFailure { toast("无法调起系统分享：${it.message ?: ""}") }
        }.onFailure { toast("准备分享图片失败：${it.message ?: ""}") }
    }
}
