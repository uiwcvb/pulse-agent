package dev.bubuki.proactive

import android.content.Context
import android.content.Intent
import android.os.SystemClock
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/**
 * [Module] RootPhoneHarnessLoop
 * [Responsibility] Live VirtualDisplay preview polling, local RootPhoneAgent task loop,
 *                  active-control keep-screen-on policy, `submit`, `submitFromBridge`, `stop`, and `resume`.
 */

internal const val ACTIVE_CONTROL_KEEP_SCREEN_WINDOW_MS = 90_000L

internal fun RootPhoneHarness.isScreenInteractiveNow(base: Context): Boolean {
    val live = runCatching {
        val pm = base.getSystemService(android.os.PowerManager::class.java)
        val km = base.getSystemService(android.app.KeyguardManager::class.java)
        (pm?.isInteractive != false) && (km?.isKeyguardLocked != true)
    }.getOrElse { screenInteractive }
    screenInteractive = live
    return live
}

internal fun shouldKeepScreenOnDuringControl(
    screenInteractive: Boolean,
    displayCreated: Boolean,
    taskActive: Boolean,
    sinceLastControlMs: Long,
    overlayExpanded: Boolean
): Boolean {
    if (!screenInteractive || !displayCreated) return false
    if (taskActive) return true
    if (sinceLastControlMs in 0L..ACTIVE_CONTROL_KEEP_SCREEN_WINDOW_MS) return true
    return overlayExpanded && sinceLastControlMs in 0L..(ACTIVE_CONTROL_KEEP_SCREEN_WINDOW_MS * 2)
}

internal fun RootPhoneHarness.syncControlScreenKeepOn(appContext: Context, pokeDaemon: Boolean = false) {
    FloatingVirtualDisplay.syncKeepScreenOn(appContext)
    val keepOn = shouldKeepScreenOn(overlayExpanded = FloatingVirtualDisplay.expanded)
    if (!keepOn) {
        releaseKeepAwake()
        return
    }
    acquireKeepAwake(appContext)
    if (!pokeDaemon) return
    val now = SystemClock.elapsedRealtime()
    if (lastDaemonPokeElapsed != 0L && now - lastDaemonPokeElapsed < 12_000L) return
    lastDaemonPokeElapsed = now
    scope.launch(Dispatchers.IO) {
        runCatching {
            if (screenInteractive && privilege?.isPrivilegedReady() == true) {
                privilege?.execute(JSONObject().put("op", "keep_awake"))
            }
        }
    }
}

internal fun RootPhoneHarness.startLivePreviewLoop(appContext: Context) {
    if (previewJob?.isActive == true) return
    lastPreviewSequence = -1L
    previewJob = scope.launch {
        while (isActive) {
            val sinceTouch = SystemClock.elapsedRealtime() - lastLiveTouchElapsed
            val sinceAct = SystemClock.elapsedRealtime() - lastActivityElapsed
            val pipVisible = screenInteractive && FloatingVirtualDisplay.isAnyPreviewShowing()
            if (!pipVisible) {
                if (!state.value.active) break
                delay(2500L)
                val disp = runCatching { JSONObject(state.value.display) }.getOrNull()
                if (disp == null || !disp.optBoolean("created")) break
                continue
            }
            val powerSave = PowerSaveController.isPowerSaveActive(appContext)
            val pollInterval = when {
                sinceTouch < 1000L -> if (powerSave) 110L else 70L
                sinceAct < 3000L -> if (powerSave) 900L else 550L
                else -> if (powerSave) 2600L else 1500L
            }
            delay(pollInterval)
            val disp = runCatching { JSONObject(state.value.display) }.getOrNull()
            if (disp == null || !disp.optBoolean("created")) break
            if (!screenInteractive || !FloatingVirtualDisplay.isAnyPreviewShowing()) {
                if (!state.value.active) break
                continue
            }
            runCatching {
                val res = privilege?.execute(JSONObject().put("op", "preview").put("since", lastPreviewSequence)) ?: return@runCatching
                if (!res.optBoolean("created")) return@runCatching
                val seq = res.optLong("frameSequence", -1L)
                if (seq >= 0) lastPreviewSequence = seq
                val img = res.optString("image").ifBlank { res.optString("_image_b64") }
                if (img.isNotBlank() && img != state.value.image) {
                    val clean = shallowCopyWithoutImage(res)
                    state.update { it.copy(image = img, imageInfo = clean.toString()) }
                }
            }
        }
    }
}

internal suspend fun RootPhoneHarness.submitTaskImpl(
    base: Context,
    bot: String,
    goal: String,
    history: List<PulseMessage>,
    recordUserMessage: Boolean
): String = withContext(Dispatchers.IO) {
    initialize(base); loaded.await(); submitLock.withLock {
        require(goal.isNotBlank() && goal.length <= 6000) { "请交代一件 6000 字以内的手机任务" }
        if (state.value.active || job?.isActive == true) {
            job?.cancel(); agent?.cancel()
            withTimeoutOrNull(1500L) { job?.join() }
            state.update { it.copy(phase = "idle") }
        }
        val config = modelConfig(SecurePrefs.read(base)); require(ready(config)) { "先在设置中保存 API Key 并选择模型" }
        var privSt = privilege!!.status()
        if (!privSt.optBoolean("privileged", privSt.optBoolean("root"))) {
            privSt = privilege!!.probe()
        }
        require(privSt.optBoolean("privileged", privSt.optBoolean("root"))) { "尚未取得 Root 或无线 ADB 调试授权，请在设置中连接" }
        val workspace = command(base, JSONObject().put("op", "create"))
        val wsUid = workspace.optInt("uid", -1)
        require(workspace.optInt("displayId", -1) > 0 && (wsUid == 0 || wsUid == 2000))
        val id = "phone-" + UUID.randomUUID()
        state.update { it.copy(phase = "running", botId = bot, goal = goal, result = "", error = "", trace = "[]", model = config.optString("model"), modelReady = true) }
        if (recordUserMessage) {
            append(PulseMessage(id, bot, "user", goal, "phone_task", "root-phone", System.currentTimeMillis() / 1000.0, "", "[]"))
        }
        persist()
        try { withContext(Dispatchers.Main) { base.startForegroundService(Intent(base, RootPhoneService::class.java)) } }
        catch (error: Exception) { state.update { it.copy(phase = "failed", error = PulseErrors.safeError(error)) }; persist(); throw error }
        if (state.value.phase != "running") throw CancellationException("手机任务已取消")
        startAgentLoop(base.applicationContext, bot, goal, config, workspace, history)
        id
    }
}

internal fun RootPhoneHarness.startAgentLoop(
    base: Context,
    bot: String,
    goal: String,
    config: JSONObject,
    workspace: JSONObject,
    history: List<PulseMessage>
) {
    agent = RootPhoneAgent()
    job = scope.launch {
        var waiting = false
        try {
            val outcome = agent!!.run(goal, config, workspace, history,
                execute = { name, arguments ->
                    if (state.value.phase != "running") throw CancellationException("手机任务已停止")
                    val op = when (name) {
                        "phone_apps" -> "apps"
                        "phone_status" -> "status"
                        "phone_launch" -> "launch"
                        "phone_ui_tree" -> "ui_tree"
                        "phone_tap_node" -> "tap_node"
                        "phone_type_node" -> "type_node"
                        "phone_capture" -> "capture"
                        "phone_tap" -> "tap"
                        "phone_swipe" -> "swipe"
                        "phone_text" -> "text"
                        "phone_key" -> "key"
                        "phone_read_otp" -> "read_otp"
                        else -> throw Exception("未知手机工具：$name")
                    }
                    command(base, JSONObject(arguments.toString()).put("op", op))
                },
                onStep = { trace ->
                    if (trace.optString("tool") in listOf("phone_text", "phone_type_node")) {
                        val args = trace.getJSONObject("arguments")
                        args.put("characters", args.optString("text").length)
                        args.remove("text")
                    }
                    val all = JSONArray(state.value.trace).put(trace)
                    state.update { it.copy(trace = all.toString()) }
                    persist(); broadcast()
                })
            waiting = outcome.needsUser
            state.update { it.copy(phase = if (waiting) "waiting_user" else "done", result = outcome.text) }
            append(message(bot, "assistant", outcome.text, if (waiting) "phone_question" else "phone_result", state.value.trace))
        } catch (cancelled: CancellationException) {
            withContext(NonCancellable) {
                state.update { it.copy(phase = "idle", result = "手机执行已停止。") }
                append(message(bot, "assistant", "手机执行已停止。", "phone_stopped", state.value.trace))
            }
        } catch (error: Exception) {
            state.update { it.copy(phase = "failed", error = PulseErrors.safeError(error), result = "手机执行中断：${PulseErrors.safeError(error)}") }
            append(message(bot, "assistant", state.value.result, "phone_stopped", state.value.trace))
        } finally {
            withContext(NonCancellable) {
                persist(); broadcast()
                if (!waiting) {
                    scheduleAutoRelease(base, 2500L)
                    if (!state.value.displayCreated && !FloatingVirtualDisplay.overlayVisible.value) {
                        withContext(Dispatchers.Main) { base.stopService(Intent(base, RootPhoneService::class.java)) }
                    }
                }
            }
        }
    }
}

internal fun RootPhoneHarness.stopTaskImpl(base: Context, closeDisplay: Boolean) {
    initialize(base)
    if (closeDisplay) {
        FloatingVirtualDisplay.hide()
    }
    val active = job
    val curDisp = runCatching { JSONObject(state.value.display).optInt("displayId", -1) }.getOrDefault(-1)
    if (active?.isActive != true && !state.value.active && (!closeDisplay || (!state.value.displayCreated && curDisp <= 0))) {
        return
    }
    state.update { it.copy(phase = if (closeDisplay) "stopping" else "idle") }
    if (active?.isActive == true) { active.cancel(); agent?.cancel() }
    if (closeDisplay) {
        scope.launch {
            try {
                runCatching { command(base, JSONObject().put("op", "release").put("user_close", true).put("destroy", true)) }
                markDisplayDestroyed(base, alreadyReleased = true)
            } finally {
                state.update { it.copy(phase = "stopped") }
                persist(); broadcast()
                base.stopService(Intent(base, RootPhoneService::class.java))
            }
        }
    } else {
        persist(); broadcast()
        base.stopService(Intent(base, RootPhoneService::class.java))
    }
}

internal fun RootPhoneHarness.resumeTaskImpl(base: Context) {
    initialize(base); require(state.value.phase == "waiting_user") { "当前没有等待继续的手机任务" }
    val old = state.value
    scope.launch {
        job?.join()
        state.update { it.copy(phase = "idle") }
        runCatching {
            submitTaskImpl(
                base,
                old.botId ?: "companion",
                "继续完成上次的任务：${old.goal}\n用户已处理好需要接管的事项，请先调用 phone_capture（或 phone_ui_tree）核对当前界面状态后继续。",
                old.messages.filter { it.botId == old.botId },
                recordUserMessage = true
            )
        }.onFailure { error ->
            state.update { it.copy(phase = "waiting_user", error = PulseErrors.safeError(error)) }
        }
    }
}
