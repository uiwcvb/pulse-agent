package dev.bubuki.proactive

import android.os.SystemClock
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs

/**
 * [Module] PulseDeltaEngine
 * [Responsibility] Deterministic message ordering, versioned delta queries (`/api/sync`, `/api/status`),
 *                  realtime state-sync event handling (`pins_changed`, `vault_changed`, `schedules_changed`, `connected`),
 *                  and monotonic trace/assistant deduplication.
 */

internal val pulseMessageComparator = Comparator<PulseMessage> { a, b ->
    if (a.orderSeq > 0L && b.orderSeq > 0L && a.orderSeq != b.orderSeq) {
        return@Comparator a.orderSeq.compareTo(b.orderSeq)
    }
    val c = a.created.compareTo(b.created)
    if (c != 0) c else a.seq.compareTo(b.seq)
}

internal fun isEphemeralMsgId(id: String): Boolean =
    id.isBlank() || id.startsWith("stopped-") || id.startsWith("pending-") || id.startsWith("local-") || id.startsWith("temp-")

internal fun deduplicateAssistantList(rawMerged: List<PulseMessage>): ArrayList<PulseMessage> {
    val out = ArrayList<PulseMessage>(rawMerged.size)
    for (msg in rawMerged) {
        val prev = out.lastOrNull()
        val canDedupById = prev != null && (prev.id == msg.id || isEphemeralMsgId(prev.id) || isEphemeralMsgId(msg.id))
        if (prev != null &&
            canDedupById &&
            prev.botId == msg.botId &&
            prev.role == msg.role &&
            prev.role == "assistant" &&
            !prev.isBgResult &&
            !msg.isBgResult &&
            prev.text.trim().isNotEmpty() &&
            prev.text.trim() == msg.text.trim() &&
            abs(prev.created - msg.created) < 30.0
        ) {
            val keepMsg = when {
                prev.id.startsWith("stopped-") && !msg.id.startsWith("stopped-") -> msg
                !prev.id.startsWith("stopped-") && msg.id.startsWith("stopped-") -> prev
                msg.trace.length > prev.trace.length || msg.choices.size > prev.choices.size -> msg
                else -> prev
            }
            out[out.lastIndex] = keepMsg.copy(
                trace = if (msg.trace.length > prev.trace.length) msg.trace else prev.trace,
                turnSteps = if (msg.turnSteps.length > prev.turnSteps.length) msg.turnSteps else prev.turnSteps,
                choices = if (msg.choices.isNotEmpty()) msg.choices else prev.choices,
                thought = if (msg.thought.isNotBlank()) msg.thought else prev.thought,
                sticker = if (msg.sticker.isNotBlank()) msg.sticker else prev.sticker,
                attachments = if (msg.attachments.isNotEmpty()) msg.attachments else prev.attachments
            )
        } else {
            out.add(msg)
        }
    }
    return out
}

internal fun mergeMonotonicTrace(
    prev: List<JSONObject>,
    incoming: List<JSONObject>,
    sameBot: Boolean
): List<JSONObject> {
    if (!sameBot || prev.isEmpty()) return incoming
    if (incoming.isEmpty()) return prev
    return incoming.mapIndexed { idx, item ->
        val prevItem = prev.getOrNull(idx)
        val prevDone = prevItem?.optString("status") == "done"
        val incDone = item.optString("status") == "done"
        if (prevItem != null && prevDone && !incDone && prevItem.optString("tool") == item.optString("tool")) {
            prevItem
        } else {
            item
        }
    }
}

internal fun NativePulseModel.buildSyncQuery(afterId: String?, force: Boolean): JSONObject = JSONObject().apply {
    val maxOrderSeq = if (force) 0L else {
        remoteMessages.asSequence()
            .filter { !it.optimistic && !isEphemeralMsgId(it.id) }
            .maxOfOrNull { it.orderSeq } ?: 0L
    }
    put("since_seq", maxOrderSeq)
    put("limit", 120)
    if (maxOrderSeq <= 0L && !force && !afterId.isNullOrBlank()) put("after_id", afterId)
    if (lastMessagesEpoch > 0L) put("messages_epoch", lastMessagesEpoch)
    if (!force && lastBotsVer.isNotBlank()) put("bots_ver", lastBotsVer)
    put("pins_ver", if (force) "" else lastPinsVer)
    if (!force && lastConfigVer.isNotBlank()) put("config_ver", lastConfigVer)
}

internal fun NativePulseModel.buildStatusQuery(afterId: String?): JSONObject? = JSONObject().apply {
    if (!afterId.isNullOrBlank()) put("after_id", afterId)
    if (lastMessagesEpoch > 0L) put("messages_epoch", lastMessagesEpoch)
    if (lastBotsVer.isNotBlank()) put("bots_ver", lastBotsVer)
    put("pins_ver", lastPinsVer)
    if (lastSchedulesVer.isNotBlank()) put("schedules_ver", lastSchedulesVer)
}.takeIf { it.length() > 0 }

internal fun NativePulseModel.applyDeltaVersions(payload: JSONObject) {
    val stSeq = payload.optLong("state_seq", 0L)
    if (stSeq > lastStateSeq) lastStateSeq = stSeq
    val sv = payload.optJSONObject("sync_versions")
    val nextBotsVer = payload.optString("bots_ver").ifBlank { sv?.optString("bots_ver") ?: "" }
    val nextPinsVer = payload.optString("pins_ver").ifBlank { sv?.optString("pins_ver") ?: "" }
    val nextCfgVer = payload.optString("config_ver").ifBlank { sv?.optString("config_ver") ?: "" }
    val nextSchedVer = payload.optString("schedules_ver").ifBlank { sv?.optString("schedules_ver") ?: "" }
    if (nextBotsVer.isNotBlank()) lastBotsVer = nextBotsVer
    if (nextCfgVer.isNotBlank()) lastConfigVer = nextCfgVer
    if (nextPinsVer.isNotBlank() && nextPinsVer != lastPinsVer) {
        val hadPrevPins = lastPinsVer.isNotBlank()
        lastPinsVer = nextPinsVer
        if (hadPrevPins && !payload.has("pinned_memories") && !payload.has("pins")) {
            loadMemoryPins()
        }
    }
    if (nextSchedVer.isNotBlank() && nextSchedVer != lastSchedulesVer) {
        val hadPrev = lastSchedulesVer.isNotBlank()
        lastSchedulesVer = nextSchedVer
        if (hadPrev && ui.activePanel == "schedules") {
            scope.launch { refreshSchedulesSilently() }
        }
    }
}

internal suspend fun NativePulseModel.refreshSchedulesSilently() {
    runCatching {
        val rRes = runCatching { request("GET", "/api/routines") }.getOrNull()
        val dRes = runCatching { request("GET", "/api/deadlines") }.getOrNull()
        val wRes = runCatching { request("GET", "/api/watchdogs") }.getOrNull()
        val merged = JSONObject()
            .put("routines", rRes?.optJSONArray("routines") ?: (ui.schedulesData?.optJSONArray("routines") ?: JSONArray()))
            .put("deadlines", dRes?.optJSONArray("deadlines") ?: (ui.schedulesData?.optJSONArray("deadlines") ?: JSONArray()))
            .put("timers", rRes?.optJSONArray("timers") ?: (ui.schedulesData?.optJSONArray("timers") ?: JSONArray()))
        ui = ui.copy(
            schedulesData = merged,
            watchdogs = wRes?.optJSONArray("watchdogs")?.objects() ?: ui.watchdogs,
            schedulesLoading = false
        )
    }
}

internal suspend fun NativePulseModel.refreshVaultSilently(changedPath: String) {
    runCatching {
        val listRes = request("GET", "/api/vault")
        val filesArr = listRes.optJSONArray("files") ?: JSONArray()
        val paths = (0 until filesArr.length()).map { filesArr.getJSONObject(it).getString("path") }
        val target = if (ui.selectedVaultPath in paths) ui.selectedVaultPath else (paths.firstOrNull() ?: "INDEX.md")
        val shouldReloadDoc = !ui.vaultSaving && (changedPath.isBlank() || changedPath == target || target != ui.selectedVaultPath)
        val content = if (shouldReloadDoc) {
            request("GET", "/api/vault", query = JSONObject().put("path", target)).optString("content", ui.vaultContent)
        } else {
            ui.vaultContent
        }
        ui = ui.copy(vaultFiles = paths, selectedVaultPath = target, vaultContent = content, vaultLoading = false)
    }
}

internal fun NativePulseModel.syncServerBootAndSeq(payload: JSONObject): Boolean {
    val incomingBootId = payload.optString("boot_id", "")
    val incomingSeq = payload.optLong("seq", 0L)
    val bootChanged = incomingBootId.isNotBlank() && lastServerBootId.isNotBlank() && incomingBootId != lastServerBootId
    val drasticDrop = incomingSeq > 0L && lastStreamSeq > 0L && (lastStreamSeq - incomingSeq) > 1000L && incomingBootId.isBlank()
    if (bootChanged || drasticDrop) {
        lastStreamSeq = incomingSeq.coerceAtLeast(0L)
        if (incomingBootId.isNotBlank()) lastServerBootId = incomingBootId
        activeStreamTurnId = ""
        return true
    }
    if (incomingBootId.isNotBlank()) lastServerBootId = incomingBootId
    return false
}

internal fun NativePulseModel.syncDeltaOnReconnect() {
    pendingReconnectSync = true
    scope.launch {
        runCatching { refresh(force = !ui.syncLoadedOnce, awaitLock = true) }
    }
}

internal fun NativePulseModel.handleStateSyncEvent(event: JSONObject) {
    val type = event.optString("type")
    val stSeq = event.optLong("state_seq", 0L)
    if (stSeq > lastStateSeq) lastStateSeq = stSeq
    when (type) {
        "connected" -> {
            syncServerBootAndSeq(event)
            event.optJSONArray("pins")?.objects()?.let { pins ->
                ui = ui.copy(pinnedMemories = pins)
                event.optString("pins_ver").takeIf { it.isNotBlank() }?.let { lastPinsVer = it }
            }
        }
        "pins_changed" -> {
            event.optString("pins_ver").takeIf { it.isNotBlank() }?.let { lastPinsVer = it }
            val pinsArr = event.optJSONArray("pins")
            if (pinsArr != null) {
                ui = ui.copy(pinnedMemories = pinsArr.objects())
            } else {
                loadMemoryPins()
            }
        }
        "vault_changed" -> {
            val changedPath = event.optString("path", "")
            if (changedPath == "profile/pinned_memories.md") {
                loadMemoryPins()
            }
            if (ui.activePanel == "vault") {
                scope.launch { refreshVaultSilently(changedPath) }
            }
        }
        "schedules_changed" -> {
            event.optString("schedules_ver").takeIf { it.isNotBlank() }?.let { lastSchedulesVer = it }
            val rc = event.optInt("routine_count", -1)
            if (rc >= 0) ui = ui.copy(routineCount = rc)
            if (ui.activePanel == "schedules") {
                scope.launch { refreshSchedulesSilently() }
            }
        }
        "curiosity_changed" -> {
            val remoteCurMin = event.optInt("curiosity_interval_minutes", 0)
            val botId = event.optString("bot_id", "")
            val hasFlag = event.has("curiosity_enabled") || event.has("enabled")
            val enabledFlag = event.optBoolean("curiosity_enabled", event.optBoolean("enabled", true))
            if (remoteCurMin >= 1) {
                scope.launch(Dispatchers.IO) { syncLocalConfigPrefs(curiosityMin = remoteCurMin) }
            }
            val updatedBots = if (botId.isNotBlank() && hasFlag) {
                ui.bots.map { if (it.id == botId) it.copy(curiosityEnabled = enabledFlag) else it }
            } else ui.bots
            ui = ui.copy(
                bots = updatedBots,
                curiosityMinutes = if (remoteCurMin >= 1) remoteCurMin.toString() else ui.curiosityMinutes
            )
            if (event.has("message") || event.optBoolean("refresh", false)) {
                scope.launch { refresh(force = false) }
            }
        }
    }
}

internal fun NativePulseModel.hasInFlightWork(): Boolean {
    return inFlightSends > 0 || ui.busyBot != null || ui.workingBot != null || ui.streamBotId != null ||
        (sendGuardBotId != null && SystemClock.uptimeMillis() < sendGuardUntilUptime)
}

internal fun NativePulseModel.combinedMessages(): List<PulseMessage> {
    val phoneMsgs = RootPhoneHarness.changes.value.messages.filter { it.role != "user" && it.kind != "phone_task" }
    val rawMerged = if (phoneMsgs.isEmpty()) {
        remoteMessages.sortedWith(pulseMessageComparator)
    } else {
        (remoteMessages + phoneMsgs).distinctBy { it.id }.sortedWith(pulseMessageComparator)
    }
    val baseList = deduplicateAssistantList(rawMerged)
    if (optimisticList.isEmpty()) return baseList
    val existingIds = baseList.asSequence().map { it.id }.toHashSet()
    val tailOptimistic = optimisticList.filter { opt ->
        opt.id !in existingIds && baseList.takeLast(12).none {
            (it.clientMsgId.isNotBlank() && it.clientMsgId == opt.id) ||
                (it.role == "user" && it.botId == opt.botId && it.text.trim() == opt.text.trim())
        }
    }
    return baseList + tailOptimistic
}

internal fun NativePulseModel.applyPhoneState() {
    val phone = RootPhoneHarness.changes.value
    val activeWorking = if (phone.active) {
        phone.botId
    } else {
        remoteWorkingBot ?: ui.streamBotId ?: (if (sendGuardBotId != null && SystemClock.uptimeMillis() < sendGuardUntilUptime) sendGuardBotId else null)
    }
    ui = ui.copy(
        messages = combinedMessages(),
        workingBot = activeWorking,
        workingText = if (phone.active) when (phone.phase) {
            "waiting_user" -> "手机工作区需要你接管"
            "stopping" -> "正在停止手机执行…"
            else -> "正在操作独立手机工作区…"
        } else remoteWorkingText
    )
}

internal fun NativePulseModel.scopeOf(settings: JSONObject): String {
    val value = settings.optString("serverUrl", SecureTransport.DEFAULT_SERVER).trim().trimEnd('/') + "\u0000" + settings.optString("publicKey") + "\u0000" + settings.optString("deviceToken", SecureTransport.defaultToken()).ifBlank { SecureTransport.defaultToken() } + "\u0000" + SecureTransport.getNodeSource(activity)
    return java.security.MessageDigest.getInstance("SHA-256").digest(value.toByteArray()).joinToString("") { "%02x".format(it.toInt() and 255) }
}

internal fun NativePulseModel.homeCache(settings: JSONObject): Triple<List<PulseBot>, String, List<PulseMessage>> {
    val cache = settings.optJSONObject("nativeHomeCache")
    if (cache?.optString("scope") == scopeOf(settings)) runCatching {
        val myNodeSource = SecureTransport.getNodeSource(activity)
        val bots = cache.getJSONArray("bots").objects().map { PulseBot.from(it, myNodeSource) }
        val primary = cache.getString("primary")
        val msgs = (cache.optJSONArray("messages") ?: JSONArray()).objects().map(PulseMessage::from)
        return Triple(bots, primary, msgs)
    }
    return Triple(
        listOf(
            PulseBot(
                "companion",
                "默认助手",
                "聊聊近况，帮你打理日常配置、手机与电脑。",
                "#2EC4B6",
                "circle",
                "small",
                false,
                false,
                0.0,
                "",
                instructions = "",
                curiosityEnabled = true
            ),
            PulseBot("research", "研究助手", "负责查资料、跑脚本、写文件，带着可验收成果汇报。", "#FF8A3D", "circle", "big", false, false, 0.0, "", curiosityEnabled = false)
        ),
        "companion",
        emptyList()
    )
}

internal fun NativePulseModel.cacheRoster(bots: List<PulseBot>, primary: String, serverScope: String = scopeOf(prefs)) {
    val recentMsgsJson = JSONArray()
    remoteMessages.takeLast(80).forEach { m ->
        recentMsgsJson.put(
            JSONObject()
                .put("id", m.id).put("bot_id", m.botId).put("role", m.role)
                .put("text", m.text).put("kind", m.kind).put("engine", m.engine)
                .put("created_at", m.created).put("decision", m.decision)
                .put("order_seq", m.orderSeq).put("seq", m.seq)
                .put("turn_id", m.turnId).put("client_msg_id", m.clientMsgId)
                .put("trace", runCatching { JSONArray(m.trace) }.getOrDefault(JSONArray()))
                .put("choices", JSONArray(m.choices.map { it.json() }))
                .put("from_bot_id", m.fromBotId).put("from_bot_name", m.fromBotName)
                .put("thought", m.thought).put("sticker", m.sticker)
                .put("attachments", JSONArray(m.attachments.map { a ->
                    JSONObject().put("path", a.path).put("name", a.name).put("size", a.size).put("ext", a.ext).put("mime", a.mime)
                }))
        )
    }
    val cache = JSONObject().put("scope", serverScope).put("primary", primary)
        .put("bots", JSONArray(bots.map { it.json() }))
        .put("messages", recentMsgsJson)
    draftWriter.submit { synchronized(SecurePrefs::class.java) {
        val fresh = SecurePrefs.read(activity)
        if (scopeOf(fresh) == serverScope) SecurePrefs.save(activity, fresh.put("nativeHomeCache", cache))
    } }
}

internal fun NativePulseModel.readAttachments(settings: JSONObject) {
    val stored = settings.optJSONObject("attachmentDrafts") ?: JSONObject()
    synchronized(draftLock) { stored.keys().forEach { id -> attachmentDrafts[id] = (stored.optJSONArray(id) ?: JSONArray()).objects().map(PulseAttachment::from) } }
}

internal fun NativePulseModel.attachments(id: String) = synchronized(draftLock) { attachmentDrafts[id] ?: emptyList() }

internal fun NativePulseModel.draftMode(id: String): String = attachments(id).firstOrNull()?.let { if (it.source == "computer") "computer" else "auto" } ?: "auto"

internal fun NativePulseModel.isStreamPlaceholder(t: String): Boolean {
    return t.isBlank() || t == "正在思考…" || t == "正在思考与执行任务…" || t == "正在整理回复…" || t == "正在重新生成…" || t.startsWith("正在调用 ") || t.startsWith("正在并行执行 ") || t.startsWith("⏳ ") || t.startsWith("⏰ 正在执行") || t.startsWith("📦 ")
}

internal fun NativePulseModel.markTurnCompleted(turnId: String) {
    if (turnId.isBlank()) return
    completedTurnIds.add(turnId)
    while (completedTurnIds.size > 240) {
        val first = completedTurnIds.firstOrNull() ?: break
        completedTurnIds.remove(first)
    }
}

internal fun NativePulseModel.isSuppressedByStop(botId: String?, turnId: String): Boolean {
    if (turnId.isNotBlank() && turnId in completedTurnIds) return true
    val gBot = stoppedGuardBotId ?: return false
    if (SystemClock.uptimeMillis() >= stoppedGuardUntilUptime) return false
    if (botId != null && botId != gBot) return false
    return turnId.isBlank() || turnId == currentTurnId || turnId == activeStreamTurnId || turnId in completedTurnIds
}

internal fun NativePulseModel.mergeRemoteMessages(incoming: List<PulseMessage>, fullReplace: Boolean = false) {
    if (incoming.isEmpty() && !fullReplace) return
    val oldIds = remoteMessages.asSequence().map { it.id }.toHashSet()
    val rawMerged = if (fullReplace) {
        incoming
    } else {
        val byId = LinkedHashMap<String, PulseMessage>(remoteMessages.size + incoming.size)
        remoteMessages.forEach { local ->
            val replacedByServerStop = local.id.startsWith("stopped-") && incoming.any { inc ->
                !inc.id.startsWith("stopped-") && inc.botId == local.botId && inc.role == "assistant" &&
                    (inc.text.trim() == local.text.trim() || abs(inc.created - local.created) < 20.0)
            }
            if (!replacedByServerStop) {
                byId[local.id] = local
            }
        }
        incoming.forEach { byId[it.id] = it }
        byId.values.toList()
    }
    val merged = deduplicateAssistantList(rawMerged.sortedWith(pulseMessageComparator))
    remoteMessages = merged
    optimisticList.removeAll { opt ->
        merged.takeLast(18).any {
            (it.clientMsgId.isNotBlank() && it.clientMsgId == opt.id) ||
                (it.role == "user" && it.botId == opt.botId && it.text.trim() == opt.text.trim())
        }
    }
    if (!fullReplace && oldIds.isNotEmpty()) {
        val newestAssistant = incoming.lastOrNull { it.isTerminalAssistantReply && it.id !in oldIds }
        if (newestAssistant != null && newestAssistant.id == merged.lastOrNull { it.botId == newestAssistant.botId }?.id) {
            if (sendGuardBotId == newestAssistant.botId) {
                sendGuardBotId = null
                sendGuardUntilUptime = 0L
            }
        }
    }
}

internal fun NativePulseModel.applyPacedStreamText(sBot: String?, resolvedText: String): String {
    if (sBot == null) {
        streamPacerJob?.cancel()
        targetStreamFullText = ""
        return ""
    }
    if (isStreamPlaceholder(resolvedText)) {
        streamPacerJob?.cancel()
        return resolvedText
    }
    targetStreamFullText = resolvedText
    streamPacerJob?.cancel()
    return resolvedText
}

internal fun NativePulseModel.resolveStreamText(targetBot: String?, turnId: String, rawStreamText: String): String {
    if (targetBot == null) return ""
    val prevBot = ui.streamBotId ?: ui.workingBot
    val prevFull = targetStreamFullText.takeIf { it.isNotBlank() && !isStreamPlaceholder(it) } ?: ui.streamText
    val sameTurn = turnId.isBlank() || currentTurnId.isBlank() || turnId == currentTurnId || currentTurnId.startsWith("pending-")
    if (turnId.isNotBlank() && turnId != currentTurnId) {
        if (!currentTurnId.startsWith("pending-")) {
            targetStreamFullText = ""
        }
        currentTurnId = turnId
    }
    if (isStreamPlaceholder(rawStreamText)) {
        return if (prevBot == targetBot && sameTurn && !isStreamPlaceholder(prevFull)) prevFull else rawStreamText
    }
    val isThought = rawStreamText.startsWith("💭 ")
    val prevWasThought = prevFull.startsWith("💭 ")
    if (prevBot == targetBot && sameTurn && !isStreamPlaceholder(prevFull) && !prevWasThought) {
        if (isThought) return prevFull
        if (rawStreamText.length < prevFull.length && prevFull.startsWith(rawStreamText)) {
            return prevFull
        }
    }
    return rawStreamText
}
