package dev.bubuki.proactive

import android.content.Intent
import android.graphics.BitmapFactory
import android.os.SystemClock
import android.util.Base64
import androidx.compose.ui.graphics.asImageBitmap
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs

/**
 * [Module] PulseActivitySync
 * [Responsibility] Network synchronization, WebSocket realtime event state machine (`onRealtimeEvent`),
 *                  incremental `/api/messages` & `/api/status` delta syncing (`refresh`, `refreshStreamOnly`),
 *                  monotonic tool-trace merging, optimistic message deduplication, local roster/message caching,
 *                  and bot avatar loading.
 * [Public API]
 *   - NativePulseModel.hasInFlightWork()
 *   - NativePulseModel.resume() / pause() / refresh(force) / refreshStreamOnly()
 *   - NativePulseModel.onRealtimeEvent(event)
 *   - NativePulseModel.request(method, path, body, query)
 *   - NativePulseModel.combinedMessages() / mergeRemoteMessages(incoming, fullReplace)
 *   - NativePulseModel.applyPhoneState() / cacheRoster(bots, primary, serverScope)
 */

internal suspend fun NativePulseModel.request(method: String, path: String, body: JSONObject? = null, query: JSONObject? = null) =
    withContext(Dispatchers.IO) { SecureTransport(activity).request(method, path, body, query) }

internal fun NativePulseModel.requestThrottledCloudFrame() {
    val now = SystemClock.uptimeMillis()
    if (now - lastCloudFrameRefreshAtMs < 1500L) return
    lastCloudFrameRefreshAtMs = now
    scope.launch { refreshCloudDesktopFrame(forceFull = false) }
}

private val avatarFailedVersions = java.util.concurrent.ConcurrentHashMap<String, Pair<String, Long>>()

private fun NativePulseModel.mergeHandoffMessage(event: JSONObject): Boolean {
    val msgObj = event.optJSONObject("final_message") ?: event.optJSONObject("message") ?: return false
    if (msgObj.optString("id").isBlank()) return false
    val parsed = runCatching { PulseMessage.from(msgObj) }.getOrNull() ?: return false
    mergeRemoteMessages(listOf(parsed), fullReplace = false)
    return true
}

internal fun NativePulseModel.resume() {
    active = true
    NativePulseModel.setActiveInstance(this)
    scope.launch {
        val before = localRevision; val previousScope = scopeOf(prefs)
        val settings = withContext(Dispatchers.IO) { UiPreferences.decorate(activity, SecurePrefs.read(activity)) }
        prefs = settings
        if (before == localRevision) synchronized(draftLock) {
            val stored = settings.optJSONObject("drafts") ?: JSONObject()
            stored.keys().forEach { drafts[it] = stored.optString(it) }
            readAttachments(settings)
        }
        if (previousScope != scopeOf(settings)) {
            val cached = homeCache(settings); avatarVersions.clear()
            remoteMessages = cached.third
            confirmedSyncSeq = remoteMessages
                .filter { !it.optimistic && !isEphemeralMsgId(it.id) }
                .maxOfOrNull { it.orderSeq } ?: 0L
            ui = ui.copy(bots = cached.first, primary = cached.second, botId = cached.second, messages = combinedMessages(), chatOpen = false, avatars = emptyMap())
        }
        val savedApp = settings.optString("appearance", "dark").let { if (it == "light") "light" else "dark" }
        val dark = savedApp != "light"
        val config = settings.optJSONObject("config") ?: JSONObject()
        val smallM = config.optJSONObject("small_brain")?.optString("model") ?: ""
        val bigM = config.optJSONObject("big_brain")?.optString("model") ?: ""
        val defM = config.optString("default_model").ifBlank { bigM.ifBlank { smallM } }
        val savedServerUrl = settings.optString("serverUrl", SecureTransport.DEFAULT_SERVER).trim()
        val savedToken = settings.optString("deviceToken", SecureTransport.defaultToken()).ifBlank { SecureTransport.defaultToken() }.trim()
        ui = ui.copy(
            paired = savedServerUrl.isNotBlank() && savedToken.isNotBlank(), dark = dark, appearance = savedApp,
            motion = settings.optBoolean("motion", true) && !animationsOff(activity), haptics = settings.optBoolean("haptics", true),
            smallModel = smallM, bigModel = bigM, defaultModel = defM,
            draft = draft(ui.botId), attachments = attachments(ui.botId),
            mode = if (attachments(ui.botId).isNotEmpty()) draftMode(ui.botId) else ui.mode
        )
        activity.systemBarsFor(dark)
        if (active) {
            if (settings.optBoolean("background", true) && ui.paired && !AgentForegroundService.running) {
                try { activity.startForegroundService(Intent(activity, AgentForegroundService::class.java)) } catch (e: Exception) { toast(PulseErrors.safeError(e)) }
            } else if (!settings.optBoolean("background", true)) activity.stopService(Intent(activity, AgentForegroundService::class.java))
            if (ui.activePanel == "cloud_desktop" && cloudStreamJob?.isActive != true) {
                startCloudDesktopStreamLoop(forceFull = false)
            }
        }
        refresh()
    }
}

internal fun NativePulseModel.onRealtimeEvent(event: JSONObject) {
    scope.launch {
        val type = event.optString("type")
        val mySource = SecureTransport.getNodeSource(activity)
        val evSource = event.optString("source").ifBlank {
            event.optJSONObject("message")?.optString("source")
                ?: event.optJSONObject("stream")?.optString("source")
                ?: event.optJSONObject("execution")?.optString("source")
                ?: ""
        }.trim()
        if (evSource.isNotEmpty() && evSource != mySource && type in listOf("message", "stream", "execution", "stopped")) {
            return@launch
        }
        when (type) {
            "bots_changed" -> {
                event.optString("bots_ver").takeIf { it.isNotBlank() }?.let { lastBotsVer = it }
                val stSeq = event.optLong("state_seq", 0L)
                if (stSeq > lastStateSeq) lastStateSeq = stSeq
                val botsArr = event.optJSONArray("bots")
                val primaryId = event.optString("primary_bot_id", ui.primary).ifBlank { "companion" }
                if (botsArr != null && botsArr.length() > 0) {
                    val parsedBots = botsArr.objects().mapNotNull { runCatching { PulseBot.from(it, mySource) }.getOrNull() }
                    val nextBots = PulseBot.filterForNode(parsedBots, mySource)
                    if (nextBots.isNotEmpty() && !(evSource in listOf("pc", "pc2") && mySource !in listOf("pc", "pc2") && parsedBots.all { it.homeNode in listOf("pc", "pc2") })) {
                        revision++
                        val safePrimary = if (nextBots.any { it.id == primaryId }) primaryId else (nextBots.firstOrNull { it.id == ui.primary }?.id ?: nextBots.first().id)
                        val deletedId = event.optString("deleted_bot_id", "")
                        val needCloseChat = (deletedId.isNotBlank() && ui.botId == deletedId) || nextBots.none { it.id == ui.botId }
                        val validIds = nextBots.asSequence().map { it.id }.toHashSet()
                        avatarVersions.keys.retainAll(validIds)
                        ui = ui.copy(
                            bots = nextBots,
                            primary = safePrimary,
                            botId = if (needCloseChat) safePrimary else ui.botId,
                            chatOpen = if (needCloseChat) false else ui.chatOpen,
                            avatars = ui.avatars.filterKeys { it in validIds }
                        )
                        cacheRoster(nextBots, safePrimary)
                        loadAvatars()
                        return@launch
                    }
                }
                launch { refresh(force = true) }
            }
            "config_updated" -> {
                val remoteThresh = event.optInt("auto_compact_tokens", 0)
                val remoteCurMin = event.optInt("curiosity_interval_minutes", 0)
                val (ctxTokensMap, ctxMsgsMap, ctxCachedMap) = parseContextByBot(event.optJSONObject("context_by_bot"))
                if (remoteThresh >= 8000 || remoteCurMin >= 1) {
                    launch(Dispatchers.IO) {
                        syncLocalConfigPrefs(
                            autoCompact = remoteThresh.takeIf { it >= 8000 },
                            curiosityMin = remoteCurMin.takeIf { it >= 1 }
                        )
                    }
                }
                ui = ui.copy(
                    autoCompactTokens = if (remoteThresh >= 8000) remoteThresh.toString() else ui.autoCompactTokens,
                    curiosityMinutes = if (remoteCurMin >= 1) remoteCurMin.toString() else ui.curiosityMinutes,
                    contextTokensByBot = if (ctxTokensMap.isNotEmpty()) ctxTokensMap else ui.contextTokensByBot,
                    contextCachedByBot = if (ctxCachedMap.isNotEmpty()) ctxCachedMap else ui.contextCachedByBot,
                    contextMsgsByBot = if (ctxMsgsMap.isNotEmpty()) ctxMsgsMap else ui.contextMsgsByBot
                )
            }
            "messages_truncated" -> {
                val newEpoch = event.optLong("messages_epoch", 0L)
                if (newEpoch > 0L) lastMessagesEpoch = newEpoch
                val removedIds = event.optJSONArray("removed_message_ids")?.let { arr ->
                    (0 until arr.length()).mapNotNull { arr.optString(it).takeIf { s -> s.isNotBlank() } }.toHashSet()
                }.orEmpty()
                val editedMsg = event.optJSONObject("edited_message")?.let { runCatching { PulseMessage.from(it) }.getOrNull() }
                remoteMessages = remoteMessages.mapNotNull { m ->
                    if (m.id in removedIds) null
                    else if (editedMsg != null && m.id == editedMsg.id) editedMsg
                    else m
                }
                ui = ui.copy(messages = combinedMessages())
            }
            "cloud_desktop_frame" -> {
                val frameObj = event.optJSONObject("frame") ?: return@launch
                val botId = event.optString("bot_id").takeIf { it.isNotBlank() }
                val op = event.optString("op", "")
                ui = ui.copy(
                    cloudDesktopLiveBotId = botId ?: ui.cloudDesktopLiveBotId,
                    cloudDesktopLiveOp = op.ifBlank { ui.cloudDesktopLiveOp },
                    cloudDesktopUrl = frameObj.optString("url").takeIf { it.isNotBlank() } ?: ui.cloudDesktopUrl,
                    cloudDesktopTitle = frameObj.optString("title").takeIf { it.isNotBlank() } ?: ui.cloudDesktopTitle
                )
                if (ui.activePanel == "cloud_desktop") {
                    if (frameObj.has("image") || frameObj.has("frame") || frameObj.has("screenshot")) applyCloudDesktopPacket(frameObj)
                    else requestThrottledCloudFrame()
                }
            }
            "subtask_update" -> {
                event.optJSONObject("subtask")?.let { upsertSubtaskFromJson(it) }
            }
            "subtask_gc" -> {
                val arr = event.optJSONArray("removed_ids")
                if (arr != null && arr.length() > 0) {
                    val ids = (0 until arr.length()).mapNotNull { arr.optString(it).takeIf { s -> s.isNotBlank() } }.toHashSet()
                    removeSubtasksByIds(ids)
                }
            }
            "stopped" -> {
                val stoppedBot = event.optString("bot_id").takeIf { it.isNotBlank() }
                val stoppedTurn = event.optString("turn_id", "")
                val stoppedSeq = event.optLong("seq", 0L)
                if (stoppedSeq > lastStreamSeq) lastStreamSeq = stoppedSeq
                if (stoppedTurn.isNotBlank()) markTurnCompleted(stoppedTurn)
                if (activeStreamTurnId.isNotBlank() && (stoppedTurn.isBlank() || activeStreamTurnId == stoppedTurn)) {
                    markTurnCompleted(activeStreamTurnId)
                    activeStreamTurnId = ""
                }
                val mergedHandoff = mergeHandoffMessage(event)
                if (stoppedBot == null || stoppedBot == "all" || ui.streamBotId == stoppedBot || ui.workingBot == stoppedBot) {
                    streamPacerJob?.cancel()
                    targetStreamFullText = ""
                    remoteWorkingBot = null
                    sendGuardBotId = null
                    sendGuardUntilUptime = 0L
                    ui = ui.copy(
                        messages = if (mergedHandoff) combinedMessages() else ui.messages,
                        busyBot = null,
                        workingBot = null,
                        streamBotId = null,
                        streamText = "",
                        streamThought = "",
                        streamStepText = "",
                        streamTool = "",
                        streamTrace = emptyList(),
                        streamTurnSteps = emptyList(), streamTimeline = emptyList(),
                        streamChoices = emptyList(),
                        streamSubAgents = emptyList(),
                        streamTodos = emptyList(),
                        streamStartedAt = 0L
                    )
                } else if (mergedHandoff) {
                    ui = ui.copy(messages = combinedMessages())
                }
            }
            "stream" -> {
                if (event.isNull("stream")) {
                    val clearedBot = event.optString("bot_id", "")
                    val clearedTurn = event.optString("turn_id", "")
                    val clearedSeq = event.optLong("seq", 0L)
                    if (clearedSeq > lastStreamSeq) lastStreamSeq = clearedSeq
                    if (clearedTurn.isNotBlank()) markTurnCompleted(clearedTurn)
                    val mergedHandoff = mergeHandoffMessage(event)
                    if (clearedBot.isBlank() || clearedBot == ui.botId || clearedBot == ui.streamBotId || clearedBot == ui.workingBot || clearedBot == ui.busyBot) {
                        activeStreamTurnId = ""
                        streamPacerJob?.cancel()
                        targetStreamFullText = ""
                        remoteWorkingBot = null
                        sendGuardBotId = null
                        sendGuardUntilUptime = 0L
                        ui = ui.copy(
                            messages = if (mergedHandoff) combinedMessages() else ui.messages,
                            busyBot = null,
                            workingBot = null,
                            streamBotId = null,
                            streamText = "",
                            streamThought = "",
                            streamStepText = "",
                            streamTool = "",
                            streamTrace = emptyList(),
                            streamTurnSteps = emptyList(), streamTimeline = emptyList(),
                            streamChoices = emptyList(),
                            streamSubAgents = emptyList(),
                            streamTodos = emptyList(),
                            streamStartedAt = 0L
                        )
                    } else if (mergedHandoff) {
                        ui = ui.copy(messages = combinedMessages())
                    }
                    return@launch
                }
                val stream = event.optJSONObject("stream") ?: return@launch
                val seq = event.optLong("seq", 0L).let { if (it > 0L) it else stream.optLong("seq", 0L) }
                val turnId = stream.optString("turn_id", "")
                val sBot = stream.optString("bot_id").takeIf { it.isNotBlank() } ?: return@launch
                if (isSuppressedByStop(sBot, turnId)) return@launch
                if (seq in 1 until lastStreamSeq) return@launch
                if (seq > lastStreamSeq) lastStreamSeq = seq
                if (turnId.isNotBlank()) activeStreamTurnId = turnId
                val incomingTrace = stream.optJSONArray("trace")?.objects().orEmpty()
                val sameStreamBot = ui.streamBotId == sBot || ui.workingBot == sBot
                val trace = mergeMonotonicTrace(ui.streamTrace, incomingTrace, sameStreamBot)
                val rawTool = if (stream.isNull("tool")) "" else stream.optString("tool", "").takeIf { it != "null" }.orEmpty()
                val toolName = rawTool.ifBlank {
                    if (ui.streamBotId == sBot && ui.streamTool.isNotBlank()) ui.streamTool
                    else if (trace.isNotEmpty()) "已执行 ${trace.size} 步"
                    else ""
                }
                val usesCloudDesktop = toolName == "cloud_desktop" || toolName == "cloud" ||
                    trace.any { val t = it.optString("tool"); t == "cloud_desktop" || t == "cloud" }
                if (usesCloudDesktop && ui.activePanel == "cloud_desktop") {
                    requestThrottledCloudFrame()
                }
                if (ui.chatOpen && sBot != ui.botId && ui.streamBotId == ui.botId) return@launch
                val delta = stream.optString("delta", "")
                val rawText = stream.optString("text", "").ifEmpty {
                    if (delta.isNotEmpty() && sameStreamBot && !isStreamPlaceholder(ui.streamText)) ui.streamText + delta else delta
                }
                val thought = stream.optString("thought", "").ifBlank { if (sameStreamBot) ui.streamThought else "" }
                val stepText = stream.optString("step_text", "").ifBlank {
                    if (sameStreamBot) ui.streamStepText else ""
                }
                val turnSteps = stream.optJSONArray("turn_steps")?.objects()
                    ?: if (sameStreamBot) ui.streamTurnSteps else emptyList()
                val timelineItems = stream.optJSONArray("timeline")?.objects()
                    ?: if (sameStreamBot) ui.streamTimeline else emptyList()
                val subAgents = stream.optJSONArray("sub_agents")?.objects() ?: if (sameStreamBot) ui.streamSubAgents else emptyList()
                val todos = stream.optJSONArray("todos")?.objects() ?: if (sameStreamBot) ui.streamTodos else emptyList()
                val choices = PulseChoice.parseArray(stream.optJSONArray("choices")).ifEmpty {
                    if (sameStreamBot) ui.streamChoices else emptyList()
                }
                val preserved = resolveStreamText(sBot, turnId, rawText)
                val paced = applyPacedStreamText(sBot, preserved)
                val startedMs = ui.streamStartedAt.takeIf { it > 0L } ?: System.currentTimeMillis()
                sendGuardBotId = sBot
                sendGuardUntilUptime = SystemClock.uptimeMillis() + 3_000L
                remoteWorkingBot = sBot
                val liveCtx = stream.optJSONObject("context_stats")
                val liveCurTok = liveCtx?.optInt("current_tokens", 0) ?: 0
                val liveCachedTok = liveCtx?.optInt("cached_tokens", 0) ?: 0
                val liveTtft = liveCtx?.optInt("ttft_ms", 0) ?: 0
                val liveTps = liveCtx?.optDouble("tps", 0.0)?.let { if (it.isNaN()) 0.0 else it } ?: 0.0
                val liveCompTok = liveCtx?.optInt("completion_tokens", 0) ?: 0
                val nextCtxTokens = if (liveCurTok > 0) (ui.contextTokensByBot + (sBot to liveCurTok)) else ui.contextTokensByBot
                val nextCtxCached = if (liveCurTok > 0) (ui.contextCachedByBot + (sBot to liveCachedTok)) else ui.contextCachedByBot
                val nextTtft = if (liveTtft > 0) (ui.contextTtftByBot + (sBot to liveTtft)) else ui.contextTtftByBot
                val nextTps = if (liveTps > 0.0) (ui.contextTpsByBot + (sBot to liveTps)) else ui.contextTpsByBot
                val nextComp = if (liveCompTok > 0) (ui.contextCompTokensByBot + (sBot to liveCompTok)) else ui.contextCompTokensByBot
                ui = ui.copy(
                    online = true,
                    workingBot = sBot,
                    streamBotId = sBot,
                    contextTokensByBot = nextCtxTokens,
                    contextCachedByBot = nextCtxCached,
                    contextTtftByBot = nextTtft,
                    contextTpsByBot = nextTps,
                    contextCompTokensByBot = nextComp,
                    streamSpeakerId = stream.optString("speaker_id").takeIf { it.isNotBlank() },
                    streamSpeakerName = stream.optString("speaker_name", ""),
                    streamText = paced,
                    streamThought = thought,
                    streamStepText = stepText,
                    streamTool = toolName,
                    streamTrace = trace,
                    streamTurnSteps = turnSteps,
                    streamTimeline = timelineItems,
                    streamChoices = choices,
                    streamSubAgents = subAgents,
                    streamTodos = todos,
                    streamStartedAt = startedMs,
                    cloudDesktopLiveBotId = if (usesCloudDesktop) sBot else ui.cloudDesktopLiveBotId
                )
            }
            "execution" -> {
                if (event.isNull("execution")) {
                    val clearedBot = event.optString("bot_id", "")
                    val clearedTurn = event.optString("turn_id", "")
                    val clearedSeq = event.optLong("seq", 0L)
                    if (clearedTurn.isNotBlank()) markTurnCompleted(clearedTurn)
                    if (clearedSeq > lastStreamSeq) lastStreamSeq = clearedSeq
                    val mergedHandoff = mergeHandoffMessage(event)
                    if (clearedBot.isBlank() || clearedBot == ui.botId || clearedBot == ui.streamBotId || clearedBot == ui.workingBot || clearedBot == ui.busyBot) {
                        clearStreamUi(clearWorking = true)
                        if (mergedHandoff) {
                            ui = ui.copy(messages = combinedMessages())
                        }
                    } else if (mergedHandoff) {
                        ui = ui.copy(messages = combinedMessages())
                    }
                    return@launch
                }
                val exec = event.optJSONObject("execution") ?: return@launch
                val execSeq = event.optLong("seq", 0L).let { if (it > 0L) it else exec.optLong("seq", 0L) }
                val execTurnId = event.optString("turn_id", exec.optString("turn_id", ""))
                val working = exec.optString("bot_id").takeIf { it.isNotBlank() }
                if (isSuppressedByStop(working, execTurnId)) return@launch
                if (execSeq in 1 until lastStreamSeq) return@launch
                if (execSeq > lastStreamSeq) lastStreamSeq = execSeq
                if (execTurnId.isNotBlank()) activeStreamTurnId = execTurnId
                val incomingTrace = exec.optJSONArray("trace")?.objects().orEmpty()
                val sameBot = working == null || ui.workingBot == null || ui.workingBot == working || ui.streamBotId == working
                val trace = mergeMonotonicTrace(ui.streamTrace, incomingTrace, sameBot)
                val rawTool = if (exec.isNull("tool")) "" else exec.optString("tool").takeIf { it != "null" }.orEmpty()
                val tool = rawTool.ifBlank {
                    if (working != null && ui.streamTool.isNotBlank()) ui.streamTool
                    else if (trace.isNotEmpty()) "已执行 ${trace.size} 步"
                    else ""
                }
                val usesCloudDesktop = tool == "cloud_desktop" || tool == "cloud" ||
                    trace.any { val t = it.optString("tool"); t == "cloud_desktop" || t == "cloud" }
                if (usesCloudDesktop && ui.activePanel == "cloud_desktop") {
                    requestThrottledCloudFrame()
                }
                if (ui.chatOpen && working != null && working != ui.botId && ui.workingBot == ui.botId) return@launch
                val activeBot = working ?: ui.workingBot ?: ui.streamBotId ?: sendGuardBotId
                if (activeBot != null) {
                    sendGuardBotId = activeBot
                    sendGuardUntilUptime = SystemClock.uptimeMillis() + 3_000L
                }
                remoteWorkingBot = activeBot
                remoteWorkingText = if (tool.isNotBlank()) "正在调用 $tool…" else "正在思考…"
                val execStepText = exec.optString("last_step_text", "").ifBlank {
                    if (sameBot) ui.streamStepText else ""
                }
                val execTurnSteps = exec.optJSONArray("turn_steps")?.objects()
                    ?: if (sameBot) ui.streamTurnSteps else emptyList()
                val execTimeline = exec.optJSONArray("timeline")?.objects()
                    ?: if (sameBot) ui.streamTimeline else emptyList()
                val nextCloudLive = if (usesCloudDesktop) (working ?: ui.botId) else ui.cloudDesktopLiveBotId
                val nextStreamBot = ui.streamBotId ?: activeBot
                val unchanged = ui.online &&
                    ui.workingBot == activeBot &&
                    ui.streamBotId == nextStreamBot &&
                    ui.streamTool == tool &&
                    ui.streamStepText == execStepText &&
                    ui.cloudDesktopLiveBotId == nextCloudLive &&
                    ui.streamTrace.size == trace.size &&
                    ui.streamTimeline.size == execTimeline.size &&
                    ui.streamTrace.lastOrNull()?.optString("status") == trace.lastOrNull()?.optString("status") &&
                    ui.streamTrace.lastOrNull()?.optString("tool") == trace.lastOrNull()?.optString("tool")
                if (!unchanged) {
                    ui = ui.copy(
                        online = true,
                        workingBot = activeBot,
                        streamBotId = nextStreamBot,
                        workingText = remoteWorkingText,
                        streamText = ui.streamText,
                        streamStepText = execStepText,
                        streamTool = tool,
                        streamTrace = trace,
                        streamTurnSteps = execTurnSteps,
                        streamTimeline = execTimeline,
                        cloudDesktopLiveBotId = nextCloudLive
                    )
                }
            }
            "message" -> {
                val mObj = event.optJSONObject("message") ?: return@launch
                val msg = runCatching { PulseMessage.from(mObj) }.getOrNull() ?: return@launch
                val seq = event.optLong("seq", 0L)
                val turnId = mObj.optString("turn_id", "")
                if (seq > lastStreamSeq) lastStreamSeq = seq
                val isFinalAssistantMsg = msg.isTerminalAssistantReply
                if (turnId.isNotBlank() && isFinalAssistantMsg) {
                    markTurnCompleted(turnId)
                }
                mergeRemoteMessages(listOf(msg), fullReplace = false)
                val sameOrMatchingTurn = turnId.isBlank() || currentTurnId.isBlank() || turnId == currentTurnId || currentTurnId.startsWith("pending-") || msg.kind == "stopped" || msg.kind == "bot_reply"
                val finishedStreamHere = isFinalAssistantMsg && (ui.streamBotId == msg.botId || ui.workingBot == msg.botId || sendGuardBotId == msg.botId || ui.busyBot == msg.botId) &&
                    sameOrMatchingTurn
                val updatedTokensByBot = if (msg.role == "assistant" && msg.promptTokens > 0) {
                    ui.contextTokensByBot + (msg.botId to msg.promptTokens)
                } else ui.contextTokensByBot
                val updatedCachedByBot = if (msg.role == "assistant" && msg.promptTokens > 0) {
                    ui.contextCachedByBot + (msg.botId to msg.cachedTokens)
                } else ui.contextCachedByBot
                val updatedTtftByBot = if (msg.role == "assistant" && msg.ttftMs > 0) {
                    ui.contextTtftByBot + (msg.botId to msg.ttftMs)
                } else ui.contextTtftByBot
                val updatedTpsByBot = if (msg.role == "assistant" && msg.tps > 0f) {
                    ui.contextTpsByBot + (msg.botId to msg.tps)
                } else ui.contextTpsByBot
                val updatedCompByBot = if (msg.role == "assistant" && msg.completionTokens > 0) {
                    ui.contextCompTokensByBot + (msg.botId to msg.completionTokens)
                } else ui.contextCompTokensByBot
                val boundedConsumed = if (ui.consumedChoiceIds.size > 200) {
                    ui.consumedChoiceIds.drop(ui.consumedChoiceIds.size - 200).toSet()
                } else ui.consumedChoiceIds
                if (finishedStreamHere) {
                    clearStreamUi(targetBot = msg.botId, clearWorking = true)
                    ui = ui.copy(
                        messages = combinedMessages(),
                        consumedChoiceIds = boundedConsumed,
                        contextTokensByBot = updatedTokensByBot,
                        contextCachedByBot = updatedCachedByBot,
                        contextTtftByBot = updatedTtftByBot,
                        contextTpsByBot = updatedTpsByBot,
                        contextCompTokensByBot = updatedCompByBot
                    )
                } else {
                    ui = ui.copy(
                        messages = combinedMessages(),
                        consumedChoiceIds = boundedConsumed,
                        contextTokensByBot = updatedTokensByBot,
                        contextCachedByBot = updatedCachedByBot,
                        contextTtftByBot = updatedTtftByBot,
                        contextTpsByBot = updatedTpsByBot,
                        contextCompTokensByBot = updatedCompByBot
                    )
                }
                if (!active) {
                    cacheRoster(ui.bots, ui.primary)
                }
                acknowledge()
            }
            "message_updated" -> {
                val mObj = event.optJSONObject("message") ?: return@launch
                val msg = runCatching { PulseMessage.from(mObj) }.getOrNull() ?: return@launch
                val seq = event.optLong("seq", 0L)
                if (seq > lastStreamSeq) lastStreamSeq = seq
                val newEpoch = event.optLong("messages_epoch", 0L)
                if (newEpoch > 0L) lastMessagesEpoch = newEpoch
                mergeRemoteMessages(listOf(msg), fullReplace = false)
                ui = ui.copy(
                    consumedChoiceIds = ui.consumedChoiceIds - msg.id,
                    messages = combinedMessages()
                )
            }
            else -> handleStateSyncEvent(event)
        }
    }
}

internal fun NativePulseModel.lastServerMessageId(): String? =
    remoteMessages.lastOrNull { !it.id.startsWith("stopped-") && !it.id.startsWith("local-") && !it.optimistic }?.id

internal suspend fun NativePulseModel.refresh(force: Boolean = false, awaitLock: Boolean = false) {
    if (!ui.paired || (!force && !awaitLock && refreshLock.isLocked)) return
    refreshLock.withLock {
        pendingReconnectSync = false
        val before = revision; val started = SystemClock.uptimeMillis(); val serverScope = scopeOf(prefs)
        val myGen = ++refreshGeneration
        try {
            val afterId = if (force) null else lastServerMessageId()
            val syncQuery = buildSyncQuery(afterId, force)
            val sync = request("GET", "/api/sync", null, syncQuery)
            if (serverScope != withContext(Dispatchers.IO) { scopeOf(SecurePrefs.read(activity)) }) return@withLock
            val roster = sync
            val history = sync
            val status = sync
            val remoteCfg = sync.optJSONObject("config")
            val pending = history.optJSONObject("pending") ?: JSONObject()
            val unread = pending.keys().asSequence().toSet()
            val incomingEpoch = history.optLong("messages_epoch", status.optLong("messages_epoch", 0L))
            val explicitEpochReset = history.optBoolean("epoch_reset", false) || status.optBoolean("epoch_reset", false)
            if (shouldRejectStaleSyncEpoch(incomingEpoch, lastMessagesEpoch) && !explicitEpochReset) return@withLock
            val epochChanged = (incomingEpoch > 0L && lastMessagesEpoch > 0L && incomingEpoch != lastMessagesEpoch) ||
                explicitEpochReset
            if (incomingEpoch > 0L) lastMessagesEpoch = incomingEpoch
            if (force || epochChanged) confirmedSyncSeq = 0L
            val isDelta = history.optBoolean("delta", false) && !epochChanged
            val messages = (history.optJSONArray("messages")?.objects().orEmpty()).mapNotNull {
                runCatching { PulseMessage.from(it) }.getOrNull()
            }
            mergeRemoteMessages(messages, fullReplace = !isDelta)
            val maxAuthoritativeSeq = messages
                .asSequence()
                .filter { !it.optimistic && !isEphemeralMsgId(it.id) && it.orderSeq > 0L }
                .maxOfOrNull { it.orderSeq } ?: 0L
            if (maxAuthoritativeSeq > confirmedSyncSeq) confirmedSyncSeq = maxAuthoritativeSeq
            if (!epochChanged) {
                status.optJSONArray("recent_tail")?.objects()?.mapNotNull { runCatching { PulseMessage.from(it) }.getOrNull() }?.let {
                    mergeRemoteMessages(it, fullReplace = false)
                }
            }
            val lastDone = status.optJSONObject("last_completed_stream")
            val lastDoneTurn = lastDone?.optString("turn_id").orEmpty()
            if (lastDoneTurn.isNotBlank()) markTurnCompleted(lastDoneTurn)

            val bootReset = syncServerBootAndSeq(status)
            val statusSeq = status.optLong("seq", 0L)
            val isStaleStatus = !bootReset && (statusSeq in 1 until lastStreamSeq)
            if (statusSeq > lastStreamSeq) lastStreamSeq = statusSeq

            val execsByBot = status.optJSONObject("executions_by_bot")
            val streamsByBot = status.optJSONObject("streams_by_bot")
            val rawExecObj = if (ui.chatOpen && execsByBot != null) execsByBot.optJSONObject(ui.botId) else status.optJSONObject("execution")
            val rawStreamObj = if (ui.chatOpen && streamsByBot != null) streamsByBot.optJSONObject(ui.botId) else status.optJSONObject("stream")
            val execution = if (isStaleStatus) null else rawExecObj
            val stream = if (isStaleStatus) null else rawStreamObj
            val streamTurnId = stream?.optString("turn_id").orEmpty()
            val execTurnId = execution?.optString("turn_id").orEmpty()
            val execBotId = execution?.optString("bot_id")?.takeIf { it.isNotBlank() }
            val streamBotRaw = stream?.optString("bot_id")?.takeIf { it.isNotBlank() }
            val execSuppressed = isSuppressedByStop(execBotId, execTurnId)
            val streamSuppressed = isSuppressedByStop(streamBotRaw, streamTurnId)
            val hasLiveExec = execBotId != null && !execSuppressed
            val streamAlreadyDone = streamSuppressed || (!hasLiveExec && streamTurnId.isNotBlank() && streamTurnId in completedTurnIds)

            val computer = status.optJSONObject("computer")
            val rawWorking = if (execSuppressed || streamAlreadyDone) null else execBotId
            val rawSBot = if (streamAlreadyDone) null else streamBotRaw
            if (rawSBot != null && streamTurnId.isNotBlank()) activeStreamTurnId = streamTurnId
            else if (rawWorking != null && execTurnId.isNotBlank()) activeStreamTurnId = execTurnId

            val finalMsgId = lastDone?.optString("final_message_id").orEmpty()
            val holdDoneStream = rawSBot == null && rawWorking == null && finalMsgId.isNotBlank() && remoteMessages.none { it.id == finalMsgId } &&
                (!ui.chatOpen || lastDone?.optString("bot_id") == ui.botId)
            val activeBotForTerminal = sendGuardBotId ?: ui.workingBot ?: ui.streamBotId
            val lastRoomMsg = activeBotForTerminal?.let { gBot ->
                remoteMessages.lastOrNull { it.botId == gBot && !it.isSubTurn && !it.isBgResult }
            }
            val lastUserMsg = activeBotForTerminal?.let { gBot ->
                remoteMessages.lastOrNull { it.botId == gBot && it.role == "user" }
            }
            val terminalMatchesTurn = lastRoomMsg != null && lastRoomMsg.isTerminalAssistantReply && (
                (lastRoomMsg.turnId.isNotBlank() && (lastRoomMsg.turnId == currentTurnId || lastRoomMsg.turnId == activeStreamTurnId)) ||
                (lastUserMsg == null || lastRoomMsg.orderSeq >= lastUserMsg.orderSeq)
            )
            val serverIdleNow = rawExecObj == null && rawStreamObj == null
            val forceClearStuck = isStaleStatus && serverIdleNow && terminalMatchesTurn
            val effectiveStale = isStaleStatus && !forceClearStuck
            if ((rawSBot == null && rawWorking == null || forceClearStuck) && terminalMatchesTurn) {
                sendGuardBotId = null
                sendGuardUntilUptime = 0L
            }
            val inSendGuard = sendGuardBotId != null && SystemClock.uptimeMillis() < sendGuardUntilUptime &&
                (lastDoneTurn.isBlank() || currentTurnId.isBlank() || lastDoneTurn != currentTurnId) &&
                (!ui.chatOpen || sendGuardBotId == ui.botId)
            val guardBot = if (inSendGuard) sendGuardBotId else null
            val effectiveSBot = if (effectiveStale) ui.streamBotId else if (holdDoneStream) lastDone?.optString("bot_id")?.takeIf { it.isNotBlank() } else (rawSBot ?: rawWorking ?: guardBot)
            val effectiveWorking = if (effectiveStale) ui.workingBot else (rawWorking ?: guardBot)

            remoteWorkingBot = effectiveWorking
            remoteWorkingText = execution?.optString("tool")?.takeIf { it.isNotBlank() }?.let { "正在调用 $it…" } ?: "正在思考…"
            val defM = remoteCfg?.optString("default_model")?.takeIf { it.isNotBlank() } ?: ui.defaultModel
            val hasKey = remoteCfg?.optBoolean("has_api_key") ?: ui.hasApiKey
            val rawStreamText = if (holdDoneStream) lastDone?.optString("text").orEmpty() else stream?.optString("text").orEmpty()
            val preservedText = if (effectiveStale) ui.streamText else resolveStreamText(effectiveSBot ?: effectiveWorking, streamTurnId, rawStreamText)
            val pacedText = if (effectiveStale) ui.streamText else applyPacedStreamText(effectiveSBot ?: effectiveWorking, preservedText)
            val thoughtText = if (effectiveStale) ui.streamThought else stream?.optString("thought").orEmpty()
            val streamStepText = if (effectiveStale) ui.streamStepText else if (effectiveSBot == null && effectiveWorking == null) "" else stream?.optString("step_text").orEmpty().ifBlank { execution?.optString("last_step_text").orEmpty() }
            val execArr = execution?.optJSONArray("trace")?.objects().orEmpty()
            val streamArr = stream?.optJSONArray("trace")?.objects().orEmpty()
            val rawExecTrace = if (streamArr.size >= execArr.size && streamArr.isNotEmpty()) streamArr else execArr
            val sameActiveBot = (effectiveSBot != null || effectiveWorking != null) && (ui.streamBotId == (effectiveSBot ?: effectiveWorking) || ui.workingBot == (effectiveSBot ?: effectiveWorking))
            val execTrace = if (effectiveStale) ui.streamTrace else if (effectiveSBot == null && effectiveWorking == null) emptyList() else mergeMonotonicTrace(ui.streamTrace, rawExecTrace, sameActiveBot)
            val streamTurnSteps = if (effectiveStale) ui.streamTurnSteps else if (effectiveSBot == null && effectiveWorking == null) emptyList() else (stream?.optJSONArray("turn_steps")?.objects() ?: execution?.optJSONArray("turn_steps")?.objects() ?: if (sameActiveBot) ui.streamTurnSteps else emptyList())
            val streamTimeline = if (effectiveStale) ui.streamTimeline else if (effectiveSBot == null && effectiveWorking == null) emptyList() else (stream?.optJSONArray("timeline")?.objects() ?: execution?.optJSONArray("timeline")?.objects() ?: if (sameActiveBot) ui.streamTimeline else emptyList())
            val streamChoices = if (effectiveStale) ui.streamChoices else if (effectiveSBot == null && effectiveWorking == null) emptyList() else PulseChoice.parseArray(stream?.optJSONArray("choices")).ifEmpty { if (sameActiveBot) ui.streamChoices else emptyList() }
            val sTool = if (stream == null || stream.isNull("tool")) "" else stream.optString("tool").takeIf { it != "null" }.orEmpty()
            val eTool = if (execution == null || execution.isNull("tool")) "" else execution.optString("tool").takeIf { it != "null" }.orEmpty()
            val curTool = if (effectiveStale) ui.streamTool else (sTool.ifBlank { eTool }.ifBlank { if ((effectiveSBot != null || effectiveWorking != null) && execTrace.isNotEmpty()) ui.streamTool.ifBlank { "已执行 ${execTrace.size} 步" } else "" })
            val usesCloudDesktop = curTool == "cloud_desktop" || curTool == "cloud" ||
                execTrace.any { val t = it.optString("tool"); t == "cloud_desktop" || t == "cloud" }
            val startedSec = execution?.optDouble("started_at", 0.0) ?: 0.0
            val startedMs = if (startedSec > 0) (startedSec * 1000).toLong() else if (effectiveSBot != null || effectiveWorking != null) (ui.streamStartedAt.takeIf { it > 0L } ?: System.currentTimeMillis()) else 0L
            val routinesCount = (status.optInt("routine_count", -1)).let { if (it >= 0) it else ui.routineCount }
            val (ctxTokensMap, ctxMsgsMap, ctxCachedMap) = parseContextByBot(status.optJSONObject("context_by_bot"))
            val remoteCompactThresh = status.optInt("auto_compact_tokens", remoteCfg?.optInt("auto_compact_tokens", 0) ?: 0)
            val remoteCuriosityMin = status.optInt("curiosity_interval_minutes", remoteCfg?.optInt("curiosity_interval_minutes", 0) ?: 0)
            if (remoteCompactThresh >= 8000 || remoteCuriosityMin >= 1 || defM.isNotBlank()) {
                withContext(Dispatchers.IO) {
                    syncLocalConfigPrefs(
                        autoCompact = remoteCompactThresh.takeIf { it >= 8000 },
                        curiosityMin = remoteCuriosityMin.takeIf { it >= 1 },
                        defaultModel = defM.takeIf { it.isNotBlank() }
                    )
                }
            }
            val myNodeSource = SecureTransport.getNodeSource(activity)
            applyDeltaVersions(sync)
            val botsUnchanged = roster.optBoolean("bots_unchanged", false)
            val nextBots = if (before == revision && !botsUnchanged) {
                roster.optJSONArray("bots")?.objects()?.mapNotNull { runCatching { PulseBot.from(it, myNodeSource) }.getOrNull() }?.let { PulseBot.filterForNode(it, myNodeSource) } ?: ui.bots
            } else PulseBot.filterForNode(ui.bots, myNodeSource)
            val validIds = nextBots.asSequence().map { it.id }.toHashSet()
            avatarVersions.keys.retainAll(validIds)
            val nextPins = sync.optJSONArray("pins")?.objects() ?: ui.pinnedMemories
            ui = ui.copy(
                bots = nextBots,
                avatars = ui.avatars.filterKeys { it in validIds },
                primary = if (before == revision) roster.optString("primary_bot_id", ui.primary) else ui.primary,
                pinnedMemories = nextPins,
                messages = combinedMessages(), unread = unread,
                online = true, latency = SystemClock.uptimeMillis() - started, workingBot = effectiveWorking, computerBot = null,
                workingText = remoteWorkingText,
                streamBotId = effectiveSBot,
                streamSpeakerId = if (effectiveStale) ui.streamSpeakerId else stream?.optString("speaker_id")?.takeIf { it.isNotBlank() },
                streamSpeakerName = if (effectiveStale) ui.streamSpeakerName else stream?.optString("speaker_name").orEmpty(),
                streamText = pacedText,
                streamThought = thoughtText,
                streamStepText = streamStepText,
                streamTool = curTool,
                streamTrace = execTrace,
                streamTurnSteps = streamTurnSteps,
                streamTimeline = streamTimeline,
                streamChoices = streamChoices,
                streamStartedAt = startedMs,
                cloudDesktopTitle = computer?.optString("title")?.takeIf { it.isNotBlank() && it != "null" } ?: ui.cloudDesktopTitle,
                cloudDesktopUrl = computer?.optString("url")?.takeIf { it.isNotBlank() && it != "null" } ?: ui.cloudDesktopUrl,
                cloudDesktopUserControl = if (computer?.has("control") == true) computer.optString("control").equals("user", ignoreCase = true) else ui.cloudDesktopUserControl,
                cloudDesktopActiveWindow = computer?.let(::parseCloudActiveWindow)?.takeIf { it.isNotBlank() } ?: ui.cloudDesktopActiveWindow,
                cloudDesktopTaskRunning = computer?.let(::parseCloudTaskRunning) ?: ui.cloudDesktopTaskRunning,
                cloudDesktopQuota = formatCloudQuota(computer?.optJSONObject("quota")).ifBlank { ui.cloudDesktopQuota },
                cloudDesktopLiveBotId = if (usesCloudDesktop) (effectiveSBot ?: effectiveWorking) else ui.cloudDesktopLiveBotId,
                routineCount = routinesCount,
                contextTokensByBot = if (ctxTokensMap.isNotEmpty()) ctxTokensMap else ui.contextTokensByBot,
                contextCachedByBot = if (ctxCachedMap.isNotEmpty()) ctxCachedMap else ui.contextCachedByBot,
                contextMsgsByBot = if (ctxMsgsMap.isNotEmpty()) ctxMsgsMap else ui.contextMsgsByBot,
                autoCompactTokens = if (remoteCompactThresh >= 8000 && ui.activePanel !in listOf("settings", "bot_edit")) remoteCompactThresh.toString() else ui.autoCompactTokens,
                curiosityMinutes = if (remoteCuriosityMin >= 1 && ui.activePanel !in listOf("settings", "bot_edit")) remoteCuriosityMin.toString() else ui.curiosityMinutes,
                snoozed = status.optDouble("snooze_until", 0.0) * 1000 > System.currentTimeMillis(),
                smallReady = status.optBoolean("small_ready"), bigReady = status.optBoolean("big_ready"),
                defaultModel = defM, hasApiKey = hasKey,
                syncLoadedOnce = true,
                onboardingCompleted = sync.optBoolean("onboarding_completed", ui.onboardingCompleted),
                cloudDesktopConfigured = sync.optBoolean("cloud_desktop_configured", ui.cloudDesktopConfigured)
            )
            if (ui.activePanel == "cloud_desktop" && cloudStreamJob?.isActive != true) requestThrottledCloudFrame()
            refreshSubtasks()
            if (before == revision && myGen == refreshGeneration) cacheRoster(ui.bots, ui.primary, serverScope)
            val requested = pendingOpenBot
            if (requested != null) { pendingOpenBot = null; if (bot(requested) != null) open(requested) }
            else if (ui.chatOpen && bot() == null) ui = ui.copy(chatOpen = false, botId = ui.primary, draft = draft(ui.primary), attachments = attachments(ui.primary))
            applyPhoneState(); loadAvatars(); acknowledge()
        } catch (e: Exception) { if (e is CancellationException) throw e; ui = ui.copy(online = false) }
    }
    if (pendingReconnectSync && ui.paired) {
        scope.launch { runCatching { refresh(force = !ui.syncLoadedOnce, awaitLock = false) } }
    }
}

internal suspend fun NativePulseModel.refreshStreamOnly() {
    if (!ui.paired || streamLock.isLocked) return
    val startGen = refreshGeneration
    streamLock.withLock {
        runCatching {
            val started = SystemClock.uptimeMillis()
            val afterId = lastServerMessageId()
            val statusQuery = buildStatusQuery(afterId)
            val status = request("GET", "/api/status", null, statusQuery)
            if (refreshGeneration != startGen) return@withLock
            val rtt = SystemClock.uptimeMillis() - started
            val incomingEpoch = status.optLong("messages_epoch", 0L)
            val explicitEpochReset = status.optBoolean("epoch_reset", false)
            if (shouldRejectStaleSyncEpoch(incomingEpoch, lastMessagesEpoch) && !explicitEpochReset) return@withLock
            val epochReset = explicitEpochReset ||
                (incomingEpoch > 0L && lastMessagesEpoch > 0L && incomingEpoch != lastMessagesEpoch)
            if (incomingEpoch > 0L) lastMessagesEpoch = incomingEpoch
            if (epochReset) confirmedSyncSeq = 0L
            if (epochReset && status.has("messages")) {
                val fullMsgs = status.optJSONArray("messages")?.objects()?.mapNotNull { runCatching { PulseMessage.from(it) }.getOrNull() }.orEmpty()
                mergeRemoteMessages(fullMsgs, fullReplace = true)
            } else {
                status.optJSONArray("recent_tail")?.objects()?.mapNotNull { runCatching { PulseMessage.from(it) }.getOrNull() }?.let {
                    mergeRemoteMessages(it, fullReplace = false)
                }
            }
            val lastDone = status.optJSONObject("last_completed_stream")
            val lastDoneTurn = lastDone?.optString("turn_id").orEmpty()
            if (lastDoneTurn.isNotBlank()) markTurnCompleted(lastDoneTurn)

            val execsByBot = status.optJSONObject("executions_by_bot")
            val streamsByBot = status.optJSONObject("streams_by_bot")
            val rawExecObj = if (ui.chatOpen && execsByBot != null) execsByBot.optJSONObject(ui.botId) else status.optJSONObject("execution")
            val rawStreamObj = if (ui.chatOpen && streamsByBot != null) streamsByBot.optJSONObject(ui.botId) else status.optJSONObject("stream")

            val bootReset = syncServerBootAndSeq(status)
            val statusSeq = status.optLong("seq", 0L)
            if (!bootReset && statusSeq in 1 until lastStreamSeq) {
                val activeBotStale = sendGuardBotId ?: ui.workingBot ?: ui.streamBotId
                val lastRoomStale = activeBotStale?.let { g -> remoteMessages.lastOrNull { it.botId == g && !it.isSubTurn && !it.isBgResult } }
                val lastUserStale = activeBotStale?.let { g -> remoteMessages.lastOrNull { it.botId == g && it.role == "user" } }
                val terminalStale = lastRoomStale != null && lastRoomStale.isTerminalAssistantReply && (
                    (lastRoomStale.turnId.isNotBlank() && (lastRoomStale.turnId == currentTurnId || lastRoomStale.turnId == activeStreamTurnId)) ||
                    (lastUserStale == null || lastRoomStale.orderSeq >= lastUserStale.orderSeq)
                )
                if (rawExecObj == null && rawStreamObj == null && terminalStale) {
                    sendGuardBotId = null; sendGuardUntilUptime = 0L; remoteWorkingBot = null; activeStreamTurnId = ""
                    streamPacerJob?.cancel(); targetStreamFullText = ""
                    ui = ui.copy(online = true, latency = rtt, messages = combinedMessages(), busyBot = null, workingBot = null, streamBotId = null, streamText = "", streamThought = "", streamStepText = "", streamTool = "", streamTrace = emptyList(), streamTurnSteps = emptyList(), streamTimeline = emptyList(), streamChoices = emptyList(), streamStartedAt = 0L)
                } else {
                    ui = ui.copy(online = true, latency = rtt, messages = combinedMessages())
                }
                return@withLock
            }
            if (statusSeq > lastStreamSeq) lastStreamSeq = statusSeq

            val execution = rawExecObj
            val stream = rawStreamObj
            val streamTurnId = stream?.optString("turn_id").orEmpty()
            val execTurnId = execution?.optString("turn_id").orEmpty()
            val execBotId = execution?.optString("bot_id")?.takeIf { it.isNotBlank() }
            val streamBotRaw = stream?.optString("bot_id")?.takeIf { it.isNotBlank() }
            val execSuppressed = isSuppressedByStop(execBotId, execTurnId)
            val streamSuppressed = isSuppressedByStop(streamBotRaw, streamTurnId)
            val hasLiveExec = execBotId != null && !execSuppressed
            val streamAlreadyDone = streamSuppressed || (!hasLiveExec && streamTurnId.isNotBlank() && streamTurnId in completedTurnIds)

            val working = if (execSuppressed || streamAlreadyDone) null else execBotId
            val sBot = if (streamAlreadyDone) null else streamBotRaw
            if (sBot != null && streamTurnId.isNotBlank()) activeStreamTurnId = streamTurnId
            else if (working != null && execTurnId.isNotBlank()) activeStreamTurnId = execTurnId

            val finalMsgId = lastDone?.optString("final_message_id").orEmpty()
            val holdDoneStream = sBot == null && working == null && finalMsgId.isNotBlank() && remoteMessages.none { it.id == finalMsgId } &&
                (!ui.chatOpen || lastDone?.optString("bot_id") == ui.botId)
            val activeBotForTerminal = sendGuardBotId ?: ui.workingBot ?: ui.streamBotId
            val lastRoomMsg = activeBotForTerminal?.let { gBot -> remoteMessages.lastOrNull { it.botId == gBot && !it.isSubTurn && !it.isBgResult } }
            val lastUserMsg = activeBotForTerminal?.let { gBot -> remoteMessages.lastOrNull { it.botId == gBot && it.role == "user" } }
            val terminalMatchesTurn = lastRoomMsg != null && lastRoomMsg.isTerminalAssistantReply && (
                (lastRoomMsg.turnId.isNotBlank() && (lastRoomMsg.turnId == currentTurnId || lastRoomMsg.turnId == activeStreamTurnId)) ||
                (lastUserMsg == null || lastRoomMsg.orderSeq >= lastUserMsg.orderSeq)
            )
            if (sBot == null && working == null && terminalMatchesTurn) {
                sendGuardBotId = null
                sendGuardUntilUptime = 0L
            }
            val inSendGuard = sendGuardBotId != null && SystemClock.uptimeMillis() < sendGuardUntilUptime &&
                (lastDoneTurn.isBlank() || currentTurnId.isBlank() || lastDoneTurn != currentTurnId) &&
                (!ui.chatOpen || sendGuardBotId == ui.botId)
            val guardBot = if (inSendGuard) sendGuardBotId else null
            val effectiveWorking = working ?: guardBot
            val effectiveSBot = if (holdDoneStream) lastDone?.optString("bot_id")?.takeIf { it.isNotBlank() } else (sBot ?: effectiveWorking)

            remoteWorkingBot = effectiveWorking
            remoteWorkingText = execution?.optString("tool")?.takeIf { it.isNotBlank() }?.let { "正在调用 $it…" } ?: "正在思考…"
            val rawStreamText = if (holdDoneStream) lastDone?.optString("text").orEmpty() else stream?.optString("text").orEmpty()
            val preservedText = resolveStreamText(effectiveSBot ?: effectiveWorking, streamTurnId, rawStreamText)
            val pacedText = applyPacedStreamText(effectiveSBot ?: effectiveWorking, preservedText)
            val thoughtText = stream?.optString("thought").orEmpty().ifBlank { if (effectiveSBot != null && stream == null) ui.streamThought else "" }
            val streamStepText = if (effectiveSBot == null && effectiveWorking == null) "" else stream?.optString("step_text").orEmpty().ifBlank { execution?.optString("last_step_text").orEmpty() }
            val execArr = execution?.optJSONArray("trace")?.objects().orEmpty()
            val streamArr = stream?.optJSONArray("trace")?.objects().orEmpty()
            val rawExecTrace = if (streamArr.size >= execArr.size && streamArr.isNotEmpty()) streamArr else execArr
            val sameActiveBot = (effectiveSBot != null || effectiveWorking != null) && (ui.streamBotId == (effectiveSBot ?: effectiveWorking) || ui.workingBot == (effectiveSBot ?: effectiveWorking))
            val execTrace = if (effectiveSBot == null && effectiveWorking == null) emptyList() else mergeMonotonicTrace(ui.streamTrace, rawExecTrace, sameActiveBot)
            val streamTurnSteps = if (effectiveSBot == null && effectiveWorking == null) emptyList() else (stream?.optJSONArray("turn_steps")?.objects() ?: execution?.optJSONArray("turn_steps")?.objects() ?: if (sameActiveBot) ui.streamTurnSteps else emptyList())
            val streamTimeline = if (effectiveSBot == null && effectiveWorking == null) emptyList() else (stream?.optJSONArray("timeline")?.objects() ?: execution?.optJSONArray("timeline")?.objects() ?: if (sameActiveBot) ui.streamTimeline else emptyList())
            val streamChoices = if (effectiveSBot == null && effectiveWorking == null) emptyList() else PulseChoice.parseArray(stream?.optJSONArray("choices")).ifEmpty { if (sameActiveBot) ui.streamChoices else emptyList() }
            val subAgents = stream?.optJSONArray("sub_agents")?.objects() ?: if (effectiveSBot != null || effectiveWorking != null) ui.streamSubAgents else emptyList()
            val todos = stream?.optJSONArray("todos")?.objects() ?: if (effectiveSBot != null || effectiveWorking != null) ui.streamTodos else emptyList()
            val sTool = if (stream == null || stream.isNull("tool")) "" else stream.optString("tool").takeIf { it != "null" }.orEmpty()
            val eTool = if (execution == null || execution.isNull("tool")) "" else execution.optString("tool").takeIf { it != "null" }.orEmpty()
            val curTool = sTool.ifBlank { eTool }.ifBlank { if ((effectiveSBot != null || effectiveWorking != null) && execTrace.isNotEmpty()) ui.streamTool.ifBlank { "已执行 ${execTrace.size} 步" } else "" }
            val usesCloudDesktop = curTool == "cloud_desktop" || curTool == "cloud" ||
                execTrace.any { val t = it.optString("tool"); t == "cloud_desktop" || t == "cloud" }
            val startedSec = execution?.optDouble("started_at", 0.0) ?: 0.0
            val startedMs = if (startedSec > 0) (startedSec * 1000).toLong() else if (effectiveSBot != null || effectiveWorking != null) (ui.streamStartedAt.takeIf { it > 0L } ?: System.currentTimeMillis()) else 0L
            val (ctxTokensMap, ctxMsgsMap, ctxCachedMap) = parseContextByBot(status.optJSONObject("context_by_bot"))
            val remoteCompactThresh = status.optInt("auto_compact_tokens", 0)
            val remoteCuriosityMin = status.optInt("curiosity_interval_minutes", 0)
            if (remoteCompactThresh >= 8000 || remoteCuriosityMin >= 1) {
                withContext(Dispatchers.IO) {
                    syncLocalConfigPrefs(
                        autoCompact = remoteCompactThresh.takeIf { it >= 8000 },
                        curiosityMin = remoteCuriosityMin.takeIf { it >= 1 }
                    )
                }
            }
            val myNodeSource = SecureTransport.getNodeSource(activity)
            applyDeltaVersions(status)
            val statusBots = if (!status.optBoolean("bots_unchanged", true)) {
                status.optJSONArray("bots")?.objects()?.mapNotNull { runCatching { PulseBot.from(it, myNodeSource) }.getOrNull() }?.let { PulseBot.filterForNode(it, myNodeSource) }?.takeIf { it.isNotEmpty() }
            } else null
            val effectiveBots = statusBots ?: ui.bots
            val validBotIds = effectiveBots.asSequence().map { it.id }.toHashSet()
            avatarVersions.keys.retainAll(validBotIds)
            val statusPins = status.optJSONArray("pins")?.objects()
            val statusPrimary = status.optString("primary_bot_id").takeIf { it.isNotBlank() } ?: ui.primary
            val statusRoutinesCount = status.optInt("routine_count", -1).let { if (it >= 0) it else ui.routineCount }
            val stComp = status.optJSONObject("computer")
            ui = ui.copy(
                online = true,
                latency = rtt,
                bots = effectiveBots,
                avatars = ui.avatars.filterKeys { it in validBotIds },
                primary = statusPrimary,
                pinnedMemories = statusPins ?: ui.pinnedMemories,
                routineCount = statusRoutinesCount,
                messages = combinedMessages(),
                workingBot = effectiveWorking,
                workingText = remoteWorkingText,
                streamBotId = effectiveSBot,
                streamSpeakerId = stream?.optString("speaker_id")?.takeIf { it.isNotBlank() },
                streamSpeakerName = stream?.optString("speaker_name").orEmpty(),
                streamText = pacedText,
                streamThought = thoughtText,
                streamStepText = streamStepText,
                streamTool = curTool,
                streamTrace = execTrace,
                streamTurnSteps = streamTurnSteps,
                streamTimeline = streamTimeline,
                streamChoices = streamChoices,
                streamSubAgents = subAgents,
                streamTodos = todos,
                streamStartedAt = startedMs,
                contextTokensByBot = if (ctxTokensMap.isNotEmpty()) ctxTokensMap else ui.contextTokensByBot,
                contextCachedByBot = if (ctxCachedMap.isNotEmpty()) ctxCachedMap else ui.contextCachedByBot,
                contextMsgsByBot = if (ctxMsgsMap.isNotEmpty()) ctxMsgsMap else ui.contextMsgsByBot,
                autoCompactTokens = if (remoteCompactThresh >= 8000 && ui.activePanel !in listOf("settings", "bot_edit")) remoteCompactThresh.toString() else ui.autoCompactTokens,
                curiosityMinutes = if (remoteCuriosityMin >= 1 && ui.activePanel !in listOf("settings", "bot_edit")) remoteCuriosityMin.toString() else ui.curiosityMinutes,
                cloudDesktopLiveBotId = if (usesCloudDesktop) (effectiveSBot ?: effectiveWorking) else ui.cloudDesktopLiveBotId,
                cloudDesktopUserControl = if (stComp?.has("control") == true) stComp.optString("control").equals("user", ignoreCase = true) else ui.cloudDesktopUserControl,
                cloudDesktopActiveWindow = stComp?.let(::parseCloudActiveWindow)?.takeIf { it.isNotBlank() } ?: ui.cloudDesktopActiveWindow,
                cloudDesktopTaskRunning = stComp?.let(::parseCloudTaskRunning) ?: ui.cloudDesktopTaskRunning,
                cloudDesktopUrl = stComp?.optString("url")?.takeIf { it.isNotBlank() && it != "null" } ?: ui.cloudDesktopUrl
            )
            if (statusBots != null) cacheRoster(statusBots, statusPrimary)
            if (ui.activePanel == "cloud_desktop" && cloudStreamJob?.isActive != true) requestThrottledCloudFrame()
            refreshSubtasks()
        }
    }
}

internal fun NativePulseModel.loadAvatars() {
    val nowUptime = SystemClock.uptimeMillis()
    for (bot in ui.bots) {
        if (bot.avatarVersion.isBlank()) {
            if (bot.id in avatarVersions) { avatarVersions.remove(bot.id); ui = ui.copy(avatars = ui.avatars - bot.id) }
            continue
        }
        val prevFail = avatarFailedVersions[bot.id]
        if (prevFail != null && prevFail.first == bot.avatarVersion && nowUptime - prevFail.second < 60_000L) continue
        if (avatarVersions[bot.id] == bot.avatarVersion || bot.id in avatarLoading || avatarLoading.size >= 2) continue
        avatarLoading.add(bot.id)
        scope.launch {
            try {
                val image = request("GET", "/api/bots/${bot.id}/avatar")
                val bitmap = withContext(Dispatchers.Default) {
                    val value = image.getString("image"); require(value.startsWith("data:image/png;base64,"))
                    val bytes = Base64.decode(value.substringAfter(','), Base64.DEFAULT)
                    requireNotNull(BitmapFactory.decodeByteArray(bytes, 0, bytes.size)).asImageBitmap()
                }
                avatarFailedVersions.remove(bot.id)
                if (ui.bots.any { it.id == bot.id && it.avatarVersion == image.optString("version") }) {
                    avatarVersions[bot.id] = image.getString("version"); ui = ui.copy(avatars = ui.avatars + (bot.id to bitmap))
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                avatarFailedVersions[bot.id] = bot.avatarVersion to SystemClock.uptimeMillis()
            } finally {
                avatarLoading.remove(bot.id)
                val checkNow = SystemClock.uptimeMillis()
                val hasMore = ui.bots.any { b ->
                    val f = avatarFailedVersions[b.id]
                    b.avatarVersion.isNotBlank() && avatarVersions[b.id] != b.avatarVersion && b.id !in avatarLoading &&
                        (f == null || f.first != b.avatarVersion || checkNow - f.second >= 60_000L)
                }
                if (hasMore && avatarLoading.size < 2) loadAvatars()
            }
        }
    }
}

internal fun NativePulseModel.acknowledge() {
    if (!active || !ui.chatOpen || !ui.online) return
    val ids = ui.messages.filter { it.botId == ui.botId && it.id in ui.unread && it.kind != "escalation_demo" }.map { it.id }
    if (ids.isEmpty()) return
    perform { request("POST", "/api/ack", JSONObject().put("ids", JSONArray(ids))); ui = ui.copy(unread = ui.unread - ids.toSet()) }
}

internal fun NativePulseModel.pause() {
    active = false
    cloudStreamJob?.cancel()
    streamPacerJob?.cancel()
}
