package dev.bubuki.proactive

import android.os.SystemClock
import kotlinx.coroutines.CancellationException
import org.json.JSONObject

/**
 * [Module] PulseChatActions
 * [Responsibility]
 * Message-level rewrite/regeneration/branch-switching actions (`editMessageAndResend`,
 * `rerollMessage`, `switchMessageVariant`) extracted from [NativePulseModel] with
 * optimistic snapshots and full rollback on failure.
 */

internal fun NativePulseModel.editMessageAndResend(message: PulseMessage, newText: String) {
    val clean = newText.trim()
    if (clean.isEmpty()) return
    if (!ensurePaired()) return
    val id = message.botId
    val prevRemote = remoteMessages
    val prevOptimistic = optimisticList.toList()
    val prevDraft = draft(id)
    draftJob?.cancel()
    synchronized(draftLock) { drafts[id] = "" }
    localRevision++
    persistDrafts()
    val targetIdx = remoteMessages.indexOfFirst { it.id == message.id }
    if (targetIdx >= 0) {
        val updatedList = ArrayList<PulseMessage>(remoteMessages.size)
        for (i in remoteMessages.indices) {
            val cur = remoteMessages[i]
            if (i == targetIdx) {
                updatedList.add(cur.copy(text = clean))
            } else if (i > targetIdx && cur.botId == id) {
                continue
            } else {
                updatedList.add(cur)
            }
        }
        remoteMessages = updatedList
    }
    optimisticList.removeAll { it.botId == id }
    streamPacerJob?.cancel()
    targetStreamFullText = ""
    stoppedGuardBotId = null
    stoppedGuardUntilUptime = 0L
    activeStreamTurnId = ""
    currentTurnId = "pending-${System.currentTimeMillis()}"
    sendGuardBotId = id
    sendGuardUntilUptime = SystemClock.uptimeMillis() + 10_000L
    ui = ui.copy(
        editingMessage = null,
        draft = if (ui.botId == id) "" else ui.draft,
        busyBot = id,
        workingBot = id,
        workingText = "正在思考…",
        streamBotId = id,
        streamText = "",
        streamThought = "",
        streamStepText = "",
        streamTool = "",
        streamTrace = emptyList(),
        streamTurnSteps = emptyList(),
        streamTimeline = emptyList(),
        streamChoices = emptyList(),
        streamSubAgents = emptyList(),
        streamTodos = emptyList(),
        streamStartedAt = System.currentTimeMillis(),
        messages = combinedMessages()
    )
    perform {
        try {
            val res = request(
                "POST",
                "/api/messages/edit",
                JSONObject()
                    .put("message_id", message.id)
                    .put("text", clean)
                    .put("bot_id", id)
                    .put("source", SecureTransport.getNodeSource(activity))
            )
            val newEpoch = res.optLong("messages_epoch", 0L)
            if (newEpoch > 0L) lastMessagesEpoch = newEpoch
            res.optString("turn_id", "").takeIf { it.isNotBlank() }?.let {
                completedTurnIds.remove(it)
                currentTurnId = it
            }
            val removedIds = res.optJSONArray("removed_message_ids")?.let { arr ->
                (0 until arr.length()).mapNotNull { arr.optString(it).takeIf { s -> s.isNotBlank() } }.toHashSet()
            }.orEmpty()
            val editedMsg = res.optJSONObject("edited_message")?.let { runCatching { PulseMessage.from(it) }.getOrNull() }
            if (removedIds.isNotEmpty() || editedMsg != null) {
                remoteMessages = remoteMessages.mapNotNull { m ->
                    if (m.id in removedIds) null
                    else if (editedMsg != null && m.id == editedMsg.id) editedMsg
                    else m
                }
                ui = ui.copy(messages = combinedMessages())
            }
            refresh(force = true)
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            remoteMessages = prevRemote
            optimisticList.clear()
            optimisticList.addAll(prevOptimistic)
            if (prevDraft.isNotBlank() && draft(id).isBlank()) {
                synchronized(draftLock) { drafts[id] = prevDraft }
                if (ui.botId == id) ui = ui.copy(draft = prevDraft)
                persistDrafts()
            }
            clearStreamUi(targetBot = id, clearWorking = true)
            ui = ui.copy(messages = combinedMessages())
            throw e
        } finally {
            if (inFlightSends == 0 && ui.busyBot == id) ui = ui.copy(busyBot = null)
        }
    }
}

internal fun NativePulseModel.rerollMessage(message: PulseMessage) {
    if (!ensurePaired()) return
    val id = message.botId
    val prevRemote = remoteMessages
    val prevOptimistic = optimisticList.toList()
    val msgIdx = remoteMessages.indexOfFirst { it.id == message.id }
    val userIdx = if (msgIdx >= 0) {
        if (remoteMessages[msgIdx].role == "user") msgIdx
        else (msgIdx downTo 0).firstOrNull { remoteMessages[it].botId == id && remoteMessages[it].role == "user" } ?: -1
    } else {
        remoteMessages.indexOfLast { it.botId == id && it.role == "user" }
    }
    if (userIdx >= 0) {
        remoteMessages = remoteMessages.filterIndexed { idx, cur ->
            !(idx > userIdx && cur.botId == id)
        }
    }
    optimisticList.removeAll { it.botId == id }
    streamPacerJob?.cancel()
    targetStreamFullText = ""
    stoppedGuardBotId = null
    stoppedGuardUntilUptime = 0L
    activeStreamTurnId = ""
    currentTurnId = "pending-${System.currentTimeMillis()}"
    sendGuardBotId = id
    sendGuardUntilUptime = SystemClock.uptimeMillis() + 10_000L
    ui = ui.copy(
        busyBot = id,
        workingBot = id,
        workingText = "正在重新生成…",
        streamBotId = id,
        streamText = "正在重新生成…",
        streamThought = "",
        streamStepText = "",
        streamTool = "",
        streamTrace = emptyList(),
        streamTurnSteps = emptyList(),
        streamTimeline = emptyList(),
        streamChoices = emptyList(),
        streamSubAgents = emptyList(),
        streamTodos = emptyList(),
        streamStartedAt = System.currentTimeMillis(),
        messages = combinedMessages()
    )
    perform {
        try {
            val res = request(
                "POST",
                "/api/messages/reroll",
                JSONObject()
                    .put("bot_id", id)
                    .put("message_id", message.id)
                    .put("source", SecureTransport.getNodeSource(activity))
            )
            val newEpoch = res.optLong("messages_epoch", 0L)
            if (newEpoch > 0L) lastMessagesEpoch = newEpoch
            res.optString("turn_id", "").takeIf { it.isNotBlank() }?.let {
                completedTurnIds.remove(it)
                currentTurnId = it
            }
            val removedIds = res.optJSONArray("removed_message_ids")?.let { arr ->
                (0 until arr.length()).mapNotNull { arr.optString(it).takeIf { s -> s.isNotBlank() } }.toHashSet()
            }.orEmpty()
            if (removedIds.isNotEmpty()) {
                remoteMessages = remoteMessages.filterNot { it.id in removedIds }
                ui = ui.copy(messages = combinedMessages())
            }
            refresh(force = true)
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            remoteMessages = prevRemote
            optimisticList.clear()
            optimisticList.addAll(prevOptimistic)
            clearStreamUi(targetBot = id, clearWorking = true)
            ui = ui.copy(messages = combinedMessages())
            throw e
        } finally {
            if (inFlightSends == 0 && ui.busyBot == id) ui = ui.copy(busyBot = null)
        }
    }
}

internal fun NativePulseModel.switchMessageVariant(message: PulseMessage, variantIdx: Int) {
    if (!ensurePaired()) return
    val vars = message.variants
    if (variantIdx !in vars.indices) return
    if (!switchingVariantMsgIds.add(message.id)) return
    val sel = vars[variantIdx]
    val idxInRemote = remoteMessages.indexOfFirst { it.id == message.id }
    val prevMsg = if (idxInRemote >= 0) remoteMessages[idxInRemote] else null
    val prevConsumed = ui.consumedChoiceIds
    if (idxInRemote >= 0 && prevMsg != null) {
        remoteMessages = remoteMessages.toMutableList().also { list ->
            list[idxInRemote] = prevMsg.copy(
                text = sel.text,
                thought = sel.thought,
                trace = sel.trace,
                turnSteps = sel.turnSteps,
                timeline = sel.timeline,
                sticker = sel.sticker,
                attachments = sel.attachments,
                choices = sel.choices,
                apiCalls = sel.apiCalls,
                ttftMs = sel.ttftMs,
                tps = sel.tps,
                cacheHitPct = sel.cacheHitPct,
                totalMs = sel.totalMs,
                activeVariantIdx = variantIdx
            )
        }
        ui = ui.copy(
            consumedChoiceIds = ui.consumedChoiceIds - message.id,
            messages = combinedMessages()
        )
    }
    perform {
        try {
            val res = request(
                "POST",
                "/api/messages/variant",
                JSONObject()
                    .put("message_id", message.id)
                    .put("variant_idx", variantIdx)
                    .put("bot_id", message.botId)
                    .put("source", SecureTransport.getNodeSource(activity))
            )
            val newEpoch = res.optLong("messages_epoch", 0L)
            if (newEpoch > 0L) lastMessagesEpoch = newEpoch
            res.optJSONObject("message")?.let { mObj ->
                val updated = PulseMessage.from(mObj)
                val pos = remoteMessages.indexOfFirst { it.id == updated.id }
                if (pos >= 0) {
                    remoteMessages = remoteMessages.toMutableList().also { it[pos] = updated }
                }
            }
            res.optJSONObject("context_by_bot")?.let { rawCtx ->
                val (ctxTokensMap, ctxMsgsMap, ctxCachedMap) = parseContextByBot(rawCtx)
                if (ctxTokensMap.isNotEmpty()) {
                    ui = ui.copy(
                        contextTokensByBot = ctxTokensMap,
                        contextCachedByBot = ctxCachedMap,
                        contextMsgsByBot = ctxMsgsMap
                    )
                }
            }
            ui = ui.copy(messages = combinedMessages())
        } catch (e: Exception) {
            if (e is CancellationException) throw e
            if (prevMsg != null) {
                val rollbackIdx = remoteMessages.indexOfFirst { it.id == prevMsg.id }
                if (rollbackIdx >= 0) {
                    remoteMessages = remoteMessages.toMutableList().also { it[rollbackIdx] = prevMsg }
                }
            }
            ui = ui.copy(
                consumedChoiceIds = prevConsumed,
                messages = combinedMessages()
            )
            throw e
        } finally {
            switchingVariantMsgIds.remove(message.id)
        }
    }
}
