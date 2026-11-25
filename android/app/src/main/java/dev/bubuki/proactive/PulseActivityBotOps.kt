package dev.bubuki.proactive

import android.content.Intent
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/**
 * [Module] PulseActivityBotOps
 * [Responsibility]
 * Bot與記憶庫管理領域服務：封裝 Bot CRUD（創建、編輯、刪除、置頂/隱藏、主助理切換）、
 * 全局設置與模型拉取、透明記憶庫 (Obsidian Vault) 讀寫（客户端 OTA 更新已抽离至 [PulseApkUpdater]）、
 * 釘住記憶 (Memory Pins)、定時任務 (Routines/Schedules) 與哨兵 (Watchdogs) 管控，
 * 以及上下文 Token 統計與流式濃縮歸檔 (`compactBotContext`)。
 *
 * [Public API]
 * - `NativePulseModel.openSettings()`, `saveSettings()`, `updateSettingsField(...)`, `fetchModels(...)`
 * - `NativePulseModel.openBotEditor(...)`, `updateBotEditField(...)`, `saveBotEditor()`, `setPrimaryBot(...)`, `deleteBot(...)`, `placement(...)`
 * - `NativePulseModel.openVault(...)`, `selectVaultFile(...)`, `updateVaultContent(...)`, `saveVaultFile()`
 * - `NativePulseModel.openSchedules()`, `toggleRoutineItem(...)`, `runRoutineItem(...)`
 * - `NativePulseModel.loadMemoryPins()`, `addMemoryPin(...)`, `removeMemoryPin(...)`, `loadWatchdogs()`, `stopWatchdog(...)`
 * - `NativePulseModel.parseContextByBot(...)`, `contextPerfFor(...)`, `contextStatsFor(...)`, `contextCacheFor(...)`, `formatTokenCount(...)`, `compactBotContext(...)`
 */

internal fun NativePulseModel.syncLocalConfigPrefs(
    autoCompact: Int? = null,
    curiosityMin: Int? = null,
    defaultModel: String? = null,
    apiBaseUrl: String? = null,
) {
    runCatching {
        val appCtx = activity.applicationContext
        val fastCfg = SecurePrefs.readFast(appCtx).optJSONObject("config") ?: JSONObject()
        val needUpdate =
            (autoCompact != null && autoCompact in 8000..1000000 && fastCfg.optInt("auto_compact_tokens", 0) != autoCompact) ||
                (curiosityMin != null && curiosityMin in 1..1440 && fastCfg.optInt("curiosity_interval_minutes", 0) != curiosityMin) ||
                (!defaultModel.isNullOrBlank() && fastCfg.optString("default_model") != defaultModel) ||
                (!apiBaseUrl.isNullOrBlank() && fastCfg.optString("api_base_url") != apiBaseUrl)
        if (!needUpdate) return@runCatching
        prefs = SecurePrefs.update(appCtx) { s ->
            val localCfg = s.optJSONObject("config") ?: JSONObject()
            if (autoCompact != null && autoCompact in 8000..1000000) {
                localCfg.put("auto_compact_tokens", autoCompact)
            }
            if (curiosityMin != null && curiosityMin in 1..1440) {
                localCfg.put("curiosity_interval_minutes", curiosityMin)
            }
            if (!defaultModel.isNullOrBlank()) {
                localCfg.put("default_model", defaultModel)
            }
            if (!apiBaseUrl.isNullOrBlank()) {
                localCfg.put("api_base_url", apiBaseUrl)
            }
            s.put("config", localCfg)
        }
    }
}

internal fun NativePulseModel.closePanel() {
    cloudStreamJob?.cancel()
    ui = ui.copy(activePanel = null)
}

internal fun NativePulseModel.openSettings() {
    dismissMenu()
    val saved = runCatching { SecurePrefs.read(activity) }.getOrElse { JSONObject() }
    val localCfg = saved.optJSONObject("config") ?: JSONObject()
    val storedKey = localCfg.optString("api_key").ifBlank {
        localCfg.optJSONObject("big_brain")?.optString("api_key").orEmpty().ifBlank {
            localCfg.optJSONObject("small_brain")?.optString("api_key").orEmpty()
        }
    }
    val storedUrl = localCfg.optString("api_base_url").ifBlank {
        localCfg.optJSONObject("big_brain")?.optString("base_url").orEmpty().ifBlank { "https://api.openai.com/v1" }
    }
    val storedModel = localCfg.optString("default_model").ifBlank {
        localCfg.optJSONObject("big_brain")?.optString("model").orEmpty().ifBlank {
            localCfg.optJSONObject("small_brain")?.optString("model").orEmpty()
        }
    }
    val storedFbEnabled = localCfg.optBoolean("fallback_enabled", false)
    val storedFbUrl = localCfg.optString("fallback_base_url", "")
    val storedFbKey = localCfg.optString("fallback_api_key", "")
    val storedFbModel = localCfg.optString("fallback_model", "")
    val savedApp = saved.optString("appearance", "dark").let { if (it == "light") "light" else "dark" }
    val savedCompact = localCfg.optInt("auto_compact_tokens", 0).takeIf { it >= 8000 }?.toString() ?: ui.autoCompactTokens
    val savedCurMin = localCfg.optInt("curiosity_interval_minutes", 0).takeIf { it >= 1 }?.toString() ?: ui.curiosityMinutes
    ui = ui.copy(
        activePanel = "settings",
        serverUrl = saved.optString("serverUrl", SecureTransport.DEFAULT_SERVER),
        apiBaseUrl = storedUrl,
        apiKeyInput = storedKey,
        defaultModel = storedModel.ifBlank { ui.defaultModel },
        fallbackEnabled = storedFbEnabled,
        fallbackBaseUrl = storedFbUrl.ifBlank { ui.fallbackBaseUrl },
        fallbackApiKeyInput = storedFbKey,
        fallbackModel = storedFbModel.ifBlank { ui.fallbackModel },
        curiosityMinutes = savedCurMin,
        autoCompactTokens = savedCompact,
        backgroundService = saved.optBoolean("background", true),
        appearance = savedApp,
        dark = savedApp != "light"
    )
    checkForUpdates(silent = true)
    perform {
        val remote = request("GET", "/api/config")
        val rUrl = remote.optString("api_base_url").ifBlank { remote.optJSONObject("big_brain")?.optString("base_url").orEmpty() }
        val rModel = remote.optString("default_model").ifBlank { remote.optJSONObject("big_brain")?.optString("model").orEmpty() }
        val rFbEnabled = remote.optBoolean("fallback_enabled", false)
        val rFbUrl = remote.optString("fallback_base_url", "")
        val rFbModel = remote.optString("fallback_model", "")
        val rCompact = remote.optInt("auto_compact_tokens", 200000).coerceIn(8000, 1000000)
        val rCurMin = remote.optInt("curiosity_interval_minutes", 30).coerceIn(1, 1440)
        val rChannels = PulseApiChannel.listFromJson(remote.optJSONArray("channels"))
        withContext(Dispatchers.IO) {
            syncLocalConfigPrefs(autoCompact = rCompact, curiosityMin = rCurMin, defaultModel = rModel, apiBaseUrl = rUrl)
        }
        val mergedModels = linkedSetOf<String>().apply {
            addAll(ui.availableModels)
            rChannels.filter { it.enabled }.forEach { ch -> addAll(ch.models) }
        }.toList()
        ui = ui.copy(
            apiBaseUrl = if (ui.apiBaseUrl == "https://api.openai.com/v1" && rUrl.isNotBlank()) rUrl else ui.apiBaseUrl,
            defaultModel = ui.defaultModel.ifBlank { rModel },
            hasApiKey = remote.optBoolean("has_api_key", storedKey.isNotBlank()),
            fallbackEnabled = rFbEnabled,
            fallbackBaseUrl = rFbUrl.ifBlank { ui.fallbackBaseUrl },
            fallbackModel = rFbModel.ifBlank { ui.fallbackModel },
            hasFallbackApiKey = remote.optBoolean("has_fallback_api_key", storedFbKey.isNotBlank()),
            channels = rChannels,
            availableModels = mergedModels,
            modelEntries = if (ui.modelEntries.isNotEmpty()) ui.modelEntries else PulseModelEntry.synthesize(mergedModels, rChannels),
            curiosityEnabledGlobal = remote.optBoolean("enabled", true),
            curiosityMinutes = rCurMin.toString(),
            autoCompactTokens = rCompact.toString(),
            workerModel = remote.optString("worker_model", ui.workerModel),
            embeddingTier = remote.optString("embedding_tier", ui.embeddingTier).ifBlank { "local_gte_small_zh" },
            cacheKeepaliveEnabled = remote.optBoolean("cache_keepalive_enabled", ui.cacheKeepaliveEnabled),
            cacheKeepaliveIntervalSec = remote.optInt("cache_keepalive_interval_sec", 260).coerceIn(60, 600).toString(),
            cacheKeepaliveMaxPings = remote.optInt("cache_keepalive_max_pings", 1).coerceIn(0, 5).toString(),
            idleCompactMinutes = remote.optInt("idle_compact_minutes", 9).coerceIn(0, 120).toString(),
            sleepConsolidateIdleMinutes = (remote.optInt("sleep_consolidate_idle_seconds", 480) / 60).coerceIn(1, 60).toString()
        )
    }
}

internal fun NativePulseModel.updateSettingsField(
    serverUrl: String = ui.serverUrl,
    deviceTokenInput: String = ui.deviceTokenInput,
    apiBaseUrl: String = ui.apiBaseUrl,
    apiKeyInput: String = ui.apiKeyInput,
    defaultModel: String = ui.defaultModel,
    fallbackEnabled: Boolean = ui.fallbackEnabled,
    fallbackBaseUrl: String = ui.fallbackBaseUrl,
    fallbackApiKeyInput: String = ui.fallbackApiKeyInput,
    fallbackModel: String = ui.fallbackModel,
    curiosityEnabled: Boolean = ui.curiosityEnabledGlobal,
    curiosityMinutes: String = ui.curiosityMinutes,
    autoCompactTokens: String = ui.autoCompactTokens,
    workerModel: String = ui.workerModel,
    embeddingTier: String = ui.embeddingTier,
    cacheKeepaliveEnabled: Boolean = ui.cacheKeepaliveEnabled,
    cacheKeepaliveIntervalSec: String = ui.cacheKeepaliveIntervalSec,
    cacheKeepaliveMaxPings: String = ui.cacheKeepaliveMaxPings,
    idleCompactMinutes: String = ui.idleCompactMinutes, sleepConsolidateIdleMinutes: String = ui.sleepConsolidateIdleMinutes,
    backgroundService: Boolean = ui.backgroundService,
    appearance: String = ui.appearance,
    motion: Boolean = ui.motion,
    haptics: Boolean = ui.haptics,
    powerSave: Boolean = ui.powerSave
) {
    val nextApp = if (appearance == "light") "light" else "dark"
    val nextDark = nextApp != "light"
    if (nextDark != ui.dark) {
        activity.systemBarsFor(nextDark)
    }
    val serverChanged = serverUrl != ui.serverUrl || deviceTokenInput != ui.deviceTokenInput
    ui = ui.copy(
        serverUrl = serverUrl, deviceTokenInput = deviceTokenInput, apiBaseUrl = apiBaseUrl, apiKeyInput = apiKeyInput,
        defaultModel = defaultModel,
        fallbackEnabled = fallbackEnabled,
        fallbackBaseUrl = fallbackBaseUrl, fallbackApiKeyInput = fallbackApiKeyInput, fallbackModel = fallbackModel,
        curiosityEnabledGlobal = curiosityEnabled,
        curiosityMinutes = curiosityMinutes, autoCompactTokens = autoCompactTokens,
        workerModel = workerModel, embeddingTier = embeddingTier,
        cacheKeepaliveEnabled = cacheKeepaliveEnabled, cacheKeepaliveIntervalSec = cacheKeepaliveIntervalSec,
        cacheKeepaliveMaxPings = cacheKeepaliveMaxPings, idleCompactMinutes = idleCompactMinutes, sleepConsolidateIdleMinutes = sleepConsolidateIdleMinutes,
        backgroundService = backgroundService,
        appearance = nextApp, dark = nextDark, motion = motion, haptics = haptics, powerSave = powerSave
    )
    settingsWriteJob?.cancel()
    settingsWriteJob = scope.launch {
        delay(350L)
        flushSettingsPersistence(forceServerSync = serverChanged)
    }
}

internal fun NativePulseModel.flushSettingsPersistence(forceServerSync: Boolean = false) {
    settingsWriteJob?.cancel()
    settingsWriteJob = null
    val appCtx = activity.applicationContext
    val snap = ui
    val cleanServer = snap.serverUrl.trim().trimEnd('/')
    val cleanToken = snap.deviceTokenInput.trim()
    val nextApp = if (snap.appearance == "light") "light" else "dark"
    val mot = snap.motion
    val hap = snap.haptics
    val pwr = snap.powerSave
    draftWriter.submit {
        runCatching {
            prefs = SecurePrefs.update(appCtx) { s ->
                val srvChanged = forceServerSync ||
                    s.optString("serverUrl", "").trim().trimEnd('/') != cleanServer ||
                    s.optString("deviceToken", "").trim() != cleanToken
                s.put("appearance", nextApp).put("motion", mot).put("haptics", hap).put("power_save", pwr)
                if (srvChanged) {
                    s.put("serverUrl", cleanServer)
                    s.put("deviceToken", cleanToken)
                    SecureTransport.invalidateCache()
                }
            }
            UiPreferences.update(appCtx, JSONObject().put("appearance", nextApp).put("motion", mot).put("haptics", hap).put("power_save", pwr))
        }
    }
}

internal fun NativePulseModel.fetchModels(forBotEditor: Boolean = false) {
    if (ui.fetchingModels) return
    ui = ui.copy(fetchingModels = true)
    perform {
        try {
            val cleanSrv = ui.serverUrl.trim().ifBlank { SecureTransport.DEFAULT_SERVER }
            val appCtx = activity.applicationContext
            withContext(Dispatchers.IO) {
                prefs = SecurePrefs.update(appCtx) { s ->
                    s.put("serverUrl", cleanSrv)
                }
            }
            val payload = JSONObject().put("base_url", ui.apiBaseUrl.trim())
            if (ui.apiKeyInput.trim().isNotBlank()) payload.put("api_key", ui.apiKeyInput.trim())
            val res = request("POST", "/api/models", payload)
            val arr = res.optJSONArray("models") ?: JSONArray()
            val list = (0 until arr.length()).map { arr.getString(it) }.filter { it.isNotBlank() }
            val updatedChannels = res.optJSONArray("channels")?.let { PulseApiChannel.listFromJson(it) } ?: ui.channels
            val parsedEntries = PulseModelEntry.listFromJson(res.optJSONArray("model_entries"))
                .ifEmpty { PulseModelEntry.synthesize(list, updatedChannels) }
            val pickedDefault = if (ui.defaultModel.isBlank() && list.isNotEmpty()) list.first() else ui.defaultModel
            val pickedBotModel = if (forBotEditor && ui.editBotModel.isBlank() && list.isNotEmpty()) list.first() else ui.editBotModel
            ui = ui.copy(availableModels = list, modelEntries = parsedEntries, channels = updatedChannels, defaultModel = pickedDefault, editBotModel = pickedBotModel)
            toast(if (list.isEmpty()) "接口未返回模型列表，可手动填写模型名" else "已聚合 ${parsedEntries.size} 个可用模型！")
        } finally {
            ui = ui.copy(fetchingModels = false)
        }
    }
}

internal fun NativePulseModel.saveSettings() {
    if (ui.savingSettings) return
    settingsWriteJob?.cancel()
    settingsWriteJob = null
    val cleanServer = ui.serverUrl.trim().ifBlank { SecureTransport.DEFAULT_SERVER }.trimEnd('/')
    val cleanToken = ui.deviceTokenInput.trim()
    val cleanBase = ui.apiBaseUrl.trim().ifBlank { "https://api.openai.com/v1" }.trimEnd('/')
    val cleanKey = ui.apiKeyInput.trim()
    val cleanModel = ui.defaultModel.trim()
    val cleanFbBase = ui.fallbackBaseUrl.trim().trimEnd('/')
    val cleanFbKey = ui.fallbackApiKeyInput.trim()
    val cleanFbModel = ui.fallbackModel.trim()
    val minutes = (ui.curiosityMinutes.toIntOrNull() ?: 30).coerceIn(1, 1440)
    val compactTokens = (ui.autoCompactTokens.toIntOrNull() ?: 200000).coerceIn(8000, 1000000)
    ui = ui.copy(savingSettings = true, autoCompactTokens = compactTokens.toString())
    perform {
        try {
            val appCtx = activity.applicationContext
            withContext(Dispatchers.IO) {
                prefs = SecurePrefs.update(appCtx) { s ->
                    s.put("serverUrl", cleanServer)
                    val effToken = cleanToken.ifBlank { s.optString("deviceToken").ifBlank { SecureTransport.defaultToken() } }
                    s.put("deviceToken", effToken)
                    SecureTransport.invalidateCache()
                    s.put("background", ui.backgroundService)
                    val normApp = if (ui.appearance == "light") "light" else "dark"
                    s.put("appearance", normApp)
                    UiPreferences.update(appCtx, JSONObject().put("appearance", normApp).put("motion", ui.motion).put("haptics", ui.haptics).put("power_save", ui.powerSave))
                    val localCfg = s.optJSONObject("config") ?: JSONObject()
                    localCfg.put("api_base_url", cleanBase)
                    localCfg.put("auto_compact_tokens", compactTokens)
                    localCfg.put("curiosity_interval_minutes", minutes)
                    if (cleanKey.isNotBlank()) localCfg.put("api_key", cleanKey)
                    if (cleanModel.isNotBlank()) localCfg.put("default_model", cleanModel)
                    localCfg.put("fallback_enabled", ui.fallbackEnabled)
                    localCfg.put("fallback_base_url", cleanFbBase)
                    if (cleanFbKey.isNotBlank()) localCfg.put("fallback_api_key", cleanFbKey)
                    localCfg.put("fallback_model", cleanFbModel)
                    val brainObj = JSONObject().put("base_url", cleanBase).put("model", cleanModel)
                    if (cleanKey.isNotBlank()) brainObj.put("api_key", cleanKey)
                    else {
                        val oldKey = localCfg.optJSONObject("big_brain")?.optString("api_key").orEmpty()
                        if (oldKey.isNotBlank()) brainObj.put("api_key", oldKey)
                    }
                    localCfg.put("small_brain", JSONObject(brainObj.toString()))
                    localCfg.put("big_brain", JSONObject(brainObj.toString()))
                    s.put("config", localCfg)
                }
            }
            val updatePayload = JSONObject()
                .put("fallback_enabled", ui.fallbackEnabled)
                .put("fallback_base_url", cleanFbBase)
                .put("fallback_model", cleanFbModel)
                .put("enabled", ui.curiosityEnabledGlobal)
                .put("curiosity_interval_minutes", minutes)
                .put("auto_compact_tokens", compactTokens)
                .put("worker_model", ui.workerModel.trim())
                .put("embedding_tier", ui.embeddingTier.trim().ifBlank { "local_gte_small_zh" })
                .put("cache_keepalive_enabled", ui.cacheKeepaliveEnabled)
                .put("cache_keepalive_interval_sec", (ui.cacheKeepaliveIntervalSec.toIntOrNull() ?: 260).coerceIn(60, 600))
                .put("cache_keepalive_max_pings", (ui.cacheKeepaliveMaxPings.toIntOrNull() ?: 1).coerceIn(0, 5))
                .put("idle_compact_minutes", (ui.idleCompactMinutes.toIntOrNull() ?: 9).coerceIn(0, 120))
                .put("sleep_consolidate_idle_seconds", ((ui.sleepConsolidateIdleMinutes.toIntOrNull() ?: 8).coerceIn(1, 60)) * 60)
            if (cleanBase.isNotBlank() && (cleanBase != "https://api.openai.com/v1" || cleanKey.isNotBlank())) {
                updatePayload.put("api_base_url", cleanBase)
            }
            if (cleanKey.isNotBlank()) updatePayload.put("api_key", cleanKey)
            if (cleanModel.isNotBlank()) updatePayload.put("default_model", cleanModel)
            if (cleanFbKey.isNotBlank()) updatePayload.put("fallback_api_key", cleanFbKey)
            val cfgRes = request("POST", "/api/config", updatePayload)
            val (ctxTokensMap, ctxMsgsMap, ctxCachedMap) = parseContextByBot(cfgRes.optJSONObject("context_by_bot"))
            val dark = ui.appearance != "light"
            activity.systemBarsFor(dark)
            RootPhoneHarness.initialize(activity)
            if (ui.backgroundService) {
                runCatching { AgentForegroundService.reconnectOrStart(activity) }
            } else {
                activity.stopService(Intent(activity, AgentForegroundService::class.java))
            }
            ui = ui.copy(
                dark = dark,
                smallModel = cleanModel,
                bigModel = cleanModel,
                defaultModel = cleanModel,
                fallbackBaseUrl = cleanFbBase,
                fallbackModel = cleanFbModel,
                hasApiKey = cleanKey.isNotBlank() || ui.hasApiKey,
                hasFallbackApiKey = cleanFbKey.isNotBlank() || ui.hasFallbackApiKey,
                contextTokensByBot = if (ctxTokensMap.isNotEmpty()) ctxTokensMap else ui.contextTokensByBot,
                contextCachedByBot = if (ctxCachedMap.isNotEmpty()) ctxCachedMap else ui.contextCachedByBot,
                contextMsgsByBot = if (ctxMsgsMap.isNotEmpty()) ctxMsgsMap else ui.contextMsgsByBot,
                activePanel = null
            )
            refresh()
            toast("已保存并同步到云端！Harness 已就绪。")
        } finally {
            ui = ui.copy(savingSettings = false)
        }
    }
}

internal fun NativePulseModel.openBotEditor(botId: String? = null) {
    dismissMenu()
    val existing = botId?.let { bot(it) }
    ui = ui.copy(
        activePanel = "bot_edit",
        editingBotId = existing?.id,
        editBotName = existing?.name ?: "",
        editBotDesc = existing?.description ?: "",
        editBotModel = existing?.model ?: "",
        editBotInstructions = existing?.instructions ?: "",
        editBotColor = existing?.color ?: "#B568F7",
        editBotShape = existing?.shape ?: "flower",
        editBotCuriosity = existing?.curiosityEnabled ?: false,
        editBotNotify = existing?.notify ?: true,
        promptPreview = null
    )
    loadMemoryPins()
    fetchStickerPacks()
    fetchPromptPreview(existing?.id ?: ui.botId)
}

internal fun NativePulseModel.fetchPromptPreview(botId: String? = null) {
    if (!ensurePaired()) return
    val targetId = (botId ?: ui.editingBotId ?: ui.botId).trim().ifBlank { "companion" }
    ui = ui.copy(promptPreviewLoading = true)
    perform {
        try {
            val myNodeSource = SecureTransport.getNodeSource(activity)
            val res = request(
                "GET",
                "/api/system/prompt_preview",
                query = JSONObject()
                    .put("bot_id", targetId)
                    .put("source", myNodeSource)
            )
            ui = ui.copy(promptPreview = res)
        } finally {
            ui = ui.copy(promptPreviewLoading = false)
        }
    }
}

internal fun NativePulseModel.updateBotEditField(
    name: String = ui.editBotName,
    desc: String = ui.editBotDesc,
    modelName: String = ui.editBotModel,
    instructions: String = ui.editBotInstructions,
    color: String = ui.editBotColor,
    shape: String = ui.editBotShape,
    curiosity: Boolean = ui.editBotCuriosity,
    notify: Boolean = ui.editBotNotify
) {
    ui = ui.copy(
        editBotName = name, editBotDesc = desc, editBotModel = modelName,
        editBotInstructions = instructions, editBotColor = color, editBotShape = shape,
        editBotCuriosity = curiosity, editBotNotify = notify
    )
}

internal fun NativePulseModel.saveBotEditor() {
    val name = ui.editBotName.trim()
    if (name.isEmpty()) { toast("请填写 Bot 名字"); return }
    if (ui.savingBot) return
    ui = ui.copy(savingBot = true)
    perform {
        try {
            val myNodeSource = SecureTransport.getNodeSource(activity)
            val payload = JSONObject()
                .put("name", name)
                .put("description", ui.editBotDesc.trim())
                .put("model", ui.editBotModel.trim())
                .put("instructions", ui.editBotInstructions.trim())
                .put("color", ui.editBotColor)
                .put("shape", ui.editBotShape)
                .put("curiosity_enabled", ui.editBotCuriosity)
                .put("notify", ui.editBotNotify)
                .put("source", myNodeSource)
                .put("engine", "small")
            val editingId = ui.editingBotId
            val savedBot = if (editingId == null) {
                PulseBot.from(request("POST", "/api/bots", payload), myNodeSource)
            } else {
                PulseBot.from(request("PUT", "/api/bots/$editingId", payload), myNodeSource)
            }
            revision++
            val nextBots = if (editingId == null) ui.bots + savedBot else ui.bots.map { if (it.id == editingId) savedBot else it }
            ui = ui.copy(bots = nextBots, botId = savedBot.id, chatOpen = true, activePanel = null)
            cacheRoster(ui.bots, ui.primary)
            toast(if (editingId == null) "已创建新 Bot：${savedBot.name}" else "已更新 Bot：${savedBot.name}")
            refresh()
        } finally {
            ui = ui.copy(savingBot = false)
        }
    }
}

internal fun NativePulseModel.setPrimaryBot(id: String) {
    dismissMenu()
    perform {
        request("PUT", "/api/bots/primary", JSONObject().put("bot_id", id))
        revision++
        ui = ui.copy(primary = id)
        cacheRoster(ui.bots, id)
        toast("已将「${bot(id)?.name ?: id}」设为主助理")
        refresh()
    }
}

internal fun NativePulseModel.deleteBot(id: String) {
    dismissMenu()
    perform {
        request("DELETE", "/api/bots/$id")
        revision++
        val remaining = ui.bots.filterNot { it.id == id }
        ui = ui.copy(bots = remaining, botId = ui.primary, chatOpen = false, activePanel = null)
        cacheRoster(remaining, ui.primary)
        toast("已删除该 Bot")
        refresh()
    }
}

internal fun NativePulseModel.placement(id: String, key: String, value: Boolean) {
    dismissMenu()
    if (!ensurePaired()) return
    val prevBots = ui.bots
    val prevChatOpen = ui.chatOpen
    val optBots = ui.bots.map {
        if (it.id != id) it
        else when (key) {
            "pinned" -> it.copy(pinned = value, pinnedAt = if (value) System.currentTimeMillis() / 1000.0 else 0.0)
            "hidden" -> it.copy(hidden = value)
            else -> it
        }
    }
    revision++
    ui = ui.copy(bots = optBots, chatOpen = if (key == "hidden" && value && ui.botId == id) false else ui.chatOpen)
    perform {
        try {
            val myNodeSource = SecureTransport.getNodeSource(activity)
            val updated = PulseBot.from(request("PATCH", "/api/bots/$id/placement", JSONObject().put(key, value).put("source", myNodeSource)), myNodeSource)
            revision++
            ui = ui.copy(bots = ui.bots.map { if (it.id == id) updated else it }, chatOpen = if (key == "hidden" && value && ui.botId == id) false else ui.chatOpen)
            cacheRoster(ui.bots, ui.primary)
            toast(if (key == "hidden") if (value) "已隐藏。" else "已恢复显示。" else if (value) "已置顶到首页。" else "已取消置顶。")
        } catch (e: Exception) {
            revision++
            ui = ui.copy(bots = prevBots, chatOpen = prevChatOpen)
            throw e
        }
    }
}

internal fun NativePulseModel.openVault(initialPath: String = "INDEX.md") {
    dismissMenu()
    ui = ui.copy(activePanel = "vault", selectedVaultPath = initialPath, vaultLoading = true, vaultDirty = false)
    perform {
        try {
            val listRes = request("GET", "/api/vault")
            val filesArr = listRes.optJSONArray("files") ?: JSONArray()
            val paths = (0 until filesArr.length()).mapNotNull { filesArr.optJSONObject(it)?.optString("path")?.takeIf { p -> p.isNotBlank() } }
            val target = if (initialPath in paths) initialPath else (paths.firstOrNull() ?: "INDEX.md")
            val docRes = request("GET", "/api/vault", query = JSONObject().put("path", target))
            ui = ui.copy(vaultFiles = paths, selectedVaultPath = target, vaultContent = docRes.optString("content", ""), vaultDirty = false)
        } finally {
            ui = ui.copy(vaultLoading = false)
        }
    }
}

internal fun NativePulseModel.selectVaultFile(path: String) {
    ui = ui.copy(selectedVaultPath = path, vaultLoading = true, vaultDirty = false)
    perform {
        try {
            val docRes = request("GET", "/api/vault", query = JSONObject().put("path", path))
            ui = ui.copy(vaultContent = docRes.optString("content", ""), vaultDirty = false)
        } finally {
            ui = ui.copy(vaultLoading = false)
        }
    }
}

internal fun NativePulseModel.updateVaultContent(content: String) { ui = ui.copy(vaultContent = content, vaultDirty = true) }

internal fun NativePulseModel.saveVaultFile() {
    if (ui.vaultSaving) return
    ui = ui.copy(vaultSaving = true)
    perform {
        try {
            request("PUT", "/api/vault", JSONObject().put("path", ui.selectedVaultPath).put("content", ui.vaultContent))
            ui = ui.copy(vaultDirty = false)
            toast("已保存记忆：${ui.selectedVaultPath}")
        } finally {
            ui = ui.copy(vaultSaving = false)
        }
    }
}

internal fun NativePulseModel.askPrimaryToHealError(errorMsg: PulseMessage) {
    val primaryId = ui.primary
    val targetBot = bot(errorMsg.botId)?.name ?: errorMsg.botId
    val healPrompt = "Bot「$targetBot (${errorMsg.botId})」刚刚报错了：\n${errorMsg.text}\n\n请直接调用 list_models 查看可用模型，再用 manage_bot(action='update', bot_id='${errorMsg.botId}', model=...) 修复它的模型配置，然后用 send_message 让它重试刚才的任务！"
    open(primaryId)
    setDraft(healPrompt)
    send()
}

internal fun NativePulseModel.openSchedules() {
    dismissMenu()
    ui = ui.copy(activePanel = "schedules", schedulesLoading = true)
    perform {
        try {
            val rRes = runCatching { request("GET", "/api/routines") }.getOrNull()
            val dRes = runCatching { request("GET", "/api/deadlines") }.getOrNull()
            val wRes = runCatching { request("GET", "/api/watchdogs") }.getOrNull()
            val pRes = runCatching { request("GET", "/api/memories/pins") }.getOrNull()
            val merged = JSONObject()
                .put("routines", rRes?.optJSONArray("routines") ?: JSONArray())
                .put("deadlines", dRes?.optJSONArray("deadlines") ?: JSONArray())
                .put("timers", rRes?.optJSONArray("timers") ?: JSONArray())
            ui = ui.copy(
                schedulesData = merged,
                watchdogs = wRes?.optJSONArray("watchdogs")?.objects() ?: ui.watchdogs,
                pinnedMemories = pRes?.optJSONArray("pins")?.objects() ?: ui.pinnedMemories,
                schedulesLoading = false
            )
        } finally {
            ui = ui.copy(schedulesLoading = false)
        }
    }
}

internal fun NativePulseModel.toggleRoutineItem(id: String, enabled: Boolean) {
    val prevSchedules = ui.schedulesData
    if (prevSchedules != null) {
        runCatching {
            val clone = JSONObject(prevSchedules.toString())
            val arr = clone.optJSONArray("routines")
            if (arr != null) {
                for (i in 0 until arr.length()) {
                    val obj = arr.optJSONObject(i) ?: continue
                    if (obj.optString("id") == id) obj.put("enabled", enabled)
                }
            }
            ui = ui.copy(schedulesData = clone)
        }
    }
    perform {
        try {
            request("PUT", "/api/routines/$id", JSONObject().put("enabled", enabled))
            toast(if (enabled) "已启用该例行任务" else "已暂停该例行任务")
            refreshSchedulesSilently()
        } catch (e: Exception) {
            ui = ui.copy(schedulesData = prevSchedules)
            throw e
        }
    }
}

internal fun NativePulseModel.runRoutineItem(id: String) {
    perform {
        toast("已触发执行例行任务…")
        closePanel()
        request("POST", "/api/routines/$id/run")
        refresh()
    }
}

internal fun NativePulseModel.parseContextByBot(obj: JSONObject?): Triple<Map<String, Int>, Map<String, Int>, Map<String, Int>> {
    if (obj == null) return Triple(emptyMap(), emptyMap(), emptyMap())
    val tokMap = mutableMapOf<String, Int>()
    val msgMap = mutableMapOf<String, Int>()
    val cacheMap = mutableMapOf<String, Int>()
    val ttftMap = mutableMapOf<String, Int>()
    val tpsMap = mutableMapOf<String, Double>()
    val compMap = mutableMapOf<String, Int>()
    obj.keys().forEach { bid ->
        val sub = obj.optJSONObject(bid) ?: return@forEach
        val cur = sub.optInt("current_tokens", 0)
        val cnt = sub.optInt("message_count", 0)
        val cached = sub.optInt("cached_tokens", 0)
        val ttft = sub.optInt("ttft_ms", 0)
        val tps = sub.optDouble("tps", 0.0).let { if (it.isNaN()) 0.0 else it }
        val comp = sub.optInt("completion_tokens", 0)
        if (cur > 0) tokMap[bid] = cur
        if (cnt >= 0) msgMap[bid] = cnt
        if (cached >= 0) cacheMap[bid] = cached
        if (ttft > 0) ttftMap[bid] = ttft
        if (tps > 0.0) tpsMap[bid] = tps
        if (comp > 0) compMap[bid] = comp
    }
    if (ttftMap.isNotEmpty() || tpsMap.isNotEmpty() || compMap.isNotEmpty()) {
        ui = ui.copy(
            contextTtftByBot = ui.contextTtftByBot + ttftMap,
            contextTpsByBot = ui.contextTpsByBot + tpsMap,
            contextCompTokensByBot = ui.contextCompTokensByBot + compMap
        )
    }
    return Triple(tokMap, msgMap, cacheMap)
}

internal fun NativePulseModel.contextPerfFor(botId: String = ui.botId): Triple<Int, Double, Int> {
    val lastBotMsg = ui.messages.lastOrNull { it.botId == botId && it.role == "assistant" && (it.ttftMs > 0 || it.tps > 0.0) }
    val ttft = (ui.contextTtftByBot[botId] ?: lastBotMsg?.ttftMs ?: 0).coerceAtLeast(0)
    val tps = (ui.contextTpsByBot[botId] ?: lastBotMsg?.tps ?: 0.0).coerceAtLeast(0.0)
    val comp = (ui.contextCompTokensByBot[botId] ?: lastBotMsg?.completionTokens ?: 0).coerceAtLeast(0)
    return Triple(ttft, tps, comp)
}

internal fun NativePulseModel.estimateLocalTextTokens(text: String): Int {
    if (text.isEmpty()) return 0
    var cjk = 0
    for (ch in text) {
        if (ch in '\u4e00'..'\u9fff') cjk++
    }
    val other = (text.length - cjk).coerceAtLeast(0)
    return cjk + (other / 3)
}

internal fun NativePulseModel.contextStatsFor(botId: String = ui.botId): Triple<Int, Int, Int> {
    val thresh = (ui.autoCompactTokens.toIntOrNull() ?: 100000).coerceIn(8000, 1000000)
    val serverTok = ui.contextTokensByBot[botId] ?: 0
    val serverMsgCnt = ui.contextMsgsByBot[botId]
    val localMsgs by lazy(LazyThreadSafetyMode.NONE) { ui.messages.filter { it.botId == botId } }
    val curTok = if (serverTok > 0) serverTok else 14800 + localMsgs.sumOf { estimateLocalTextTokens(it.text) + 12 }
    val msgCnt = serverMsgCnt ?: localMsgs.size
    return Triple(curTok, thresh, msgCnt)
}

internal fun NativePulseModel.contextCacheFor(botId: String = ui.botId, precomputedCurTok: Int? = null): Pair<Int, Int> {
    val curTok = precomputedCurTok ?: contextStatsFor(botId).first
    val cachedTok = (ui.contextCachedByBot[botId] ?: 0).coerceIn(0, curTok.coerceAtLeast(0))
    val pct = if (curTok > 0 && cachedTok > 0) ((cachedTok.toDouble() / curTok.toDouble()) * 100.0).toInt().coerceIn(0, 100) else 0
    return cachedTok to pct
}

internal fun NativePulseModel.formatTokenCount(tokens: Int): String {
    return when {
        tokens >= 10_000 -> String.format(Locale.US, "%.1fK", tokens / 1000.0).removeSuffix(".0K") + (if (String.format(Locale.US, "%.1fK", tokens / 1000.0).endsWith(".0K")) "K" else "")
        tokens >= 1_000 -> String.format(Locale.US, "%.1fK", tokens / 1000.0)
        else -> tokens.toString()
    }
}

internal fun NativePulseModel.applyContextWindowTokens(tokens: Int, botId: String = ui.editingBotId ?: ui.botId) {
    val cleanTokens = tokens.coerceIn(8_000, 1_000_000)
    ui = ui.copy(autoCompactTokens = cleanTokens.toString())
    val appCtx = activity.applicationContext
    draftWriter.submit {
        runCatching {
            prefs = SecurePrefs.update(appCtx) { s ->
                val localCfg = s.optJSONObject("config") ?: JSONObject()
                localCfg.put("auto_compact_tokens", cleanTokens)
                s.put("config", localCfg)
            }
        }
    }
    perform {
        val mySource = SecureTransport.getNodeSource(activity)
        val res = request(
            "POST",
            "/api/config",
            JSONObject().put("auto_compact_tokens", cleanTokens),
            JSONObject().put("source", mySource)
        )
        val (ctxTokensMap, ctxMsgsMap, ctxCachedMap) = parseContextByBot(res.optJSONObject("context_by_bot"))
        val serverCompact = res.optInt("auto_compact_tokens", cleanTokens).coerceIn(8_000, 1_000_000)
        ui = ui.copy(
            autoCompactTokens = serverCompact.toString(),
            contextTokensByBot = if (ctxTokensMap.isNotEmpty()) ctxTokensMap else ui.contextTokensByBot,
            contextCachedByBot = if (ctxCachedMap.isNotEmpty()) ctxCachedMap else ui.contextCachedByBot,
            contextMsgsByBot = if (ctxMsgsMap.isNotEmpty()) ctxMsgsMap else ui.contextMsgsByBot
        )
        val compactedArr = res.optJSONArray("compacted_bots")
        val autoCompacted = compactedArr != null && compactedArr.length() > 0
        if (autoCompacted) {
            refresh(force = true)
            toast("上下文窗口已调至 ${formatTokenCount(serverCompact)} Tokens，并自动浓缩超限对话")
        } else {
            val (curTok, _, _) = contextStatsFor(botId)
            if (curTok > serverCompact && !ui.compactingContext) {
                toast("上下文窗口已调至 ${formatTokenCount(serverCompact)} Tokens，正在浓缩超限历史…")
                compactBotContext(botId)
            } else {
                toast("上下文窗口已实时调节为 ${formatTokenCount(serverCompact)} Tokens")
            }
        }
    }
}

internal fun NativePulseModel.compactBotContext(botId: String = ui.botId) {
    if (ui.compactingContext) return
    dismissMenu()
    val thresh = (ui.autoCompactTokens.toIntOrNull() ?: 120000).coerceIn(8000, 1000000)
    ui = ui.copy(
        compactingContext = true,
        activePanel = null,
        botId = botId,
        chatOpen = true,
        workingBot = botId,
        streamBotId = botId,
        streamText = "📦 正在流式压缩上下文，请查看实时进度..."
    )
    toast("📦 正在流式压缩上下文，请查看实时进度...")
    perform {
        try {
            val payload = JSONObject()
                .put("bot_id", botId)
            val res = request("POST", "/api/context/compact", payload)
            val (ctxTokensMap, ctxMsgsMap, ctxCachedMap) = parseContextByBot(res.optJSONObject("context_by_bot"))
            if (ctxTokensMap.isNotEmpty()) {
                ui = ui.copy(contextTokensByBot = ctxTokensMap, contextCachedByBot = ctxCachedMap, contextMsgsByBot = ctxMsgsMap)
            }
            val alreadyCompacted = res.optBoolean("already_compacted", false) ||
                res.optJSONObject("result")?.optBoolean("already_compacted", false) == true
            val compacted = res.optBoolean("compacted", false) ||
                res.optJSONObject("result")?.optBoolean("compacted", false) == true ||
                (res.optJSONObject("result")?.optInt("compacted_messages", 0) ?: 0) > 0
            val archivedCount = res.optInt(
                "archived_count",
                res.optJSONObject("result")?.optInt(
                    "archived_count",
                    res.optJSONObject("result")?.optInt("compacted_messages", 0) ?: 0
                ) ?: 0
            )
            if (alreadyCompacted) {
                toast("当前上下文已是最新压缩状态，无需重复压缩")
            } else if (compacted) {
                val msgResp = request("GET", "/api/messages", null, JSONObject().put("limit", 120))
                val freshMsgs = msgResp.optJSONArray("messages")?.objects()?.mapNotNull {
                    runCatching { PulseMessage.from(it) }.getOrNull()
                }.orEmpty()
                mergeRemoteMessages(freshMsgs, fullReplace = true)
                ui = ui.copy(messages = combinedMessages())
                toast("✅ 上下文压缩完成 (归档 $archivedCount 条消息)")
            } else {
                toast(res.optString("message").ifBlank { "当前对话条数较少，暂无需压缩" })
            }
        } finally {
            ui = ui.copy(
                compactingContext = false,
                workingBot = if (ui.workingBot == botId) null else ui.workingBot,
                streamBotId = if (ui.streamBotId == botId && ui.streamText.startsWith("📦")) null else ui.streamBotId,
                streamText = if (ui.streamBotId == botId && ui.streamText.startsWith("📦")) "" else ui.streamText
            )
        }
    }
}

internal fun NativePulseModel.loadMemoryPins() {
    perform {
        val res = request("GET", "/api/memories/pins")
        ui = ui.copy(pinnedMemories = res.optJSONArray("pins")?.objects() ?: emptyList())
    }
}

internal fun NativePulseModel.addMemoryPin(text: String) {
    val clean = text.trim()
    if (clean.isEmpty()) return
    val prevPins = ui.pinnedMemories
    val srcTag = if (SecureTransport.getNodeSource(activity) == "pc") "[来源: PC端]" else "[来源: 手机端]"
    val tempPin = JSONObject().put("id", "pending-${System.currentTimeMillis()}").put("text", "$srcTag $clean").put("source", SecureTransport.getNodeSource(activity))
    ui = ui.copy(pinnedMemories = prevPins + tempPin)
    perform {
        try {
            val res = request("POST", "/api/memories/pins", JSONObject().put("action", "add").put("text", clean))
            res.optString("pins_ver").takeIf { it.isNotBlank() }?.let { lastPinsVer = it }
            val nextPins = res.optJSONArray("pins")?.objects()
            if (nextPins != null) ui = ui.copy(pinnedMemories = nextPins)
            toast("已钉住记忆")
        } catch (e: Exception) {
            ui = ui.copy(pinnedMemories = prevPins)
            throw e
        }
    }
}

internal fun NativePulseModel.removeMemoryPin(pinId: String) {
    val prevPins = ui.pinnedMemories
    ui = ui.copy(pinnedMemories = prevPins.filterNot { it.optString("id") == pinId })
    perform {
        try {
            val res = request("POST", "/api/memories/pins", JSONObject().put("action", "remove").put("pin_id", pinId))
            res.optString("pins_ver").takeIf { it.isNotBlank() }?.let { lastPinsVer = it }
            val nextPins = res.optJSONArray("pins")?.objects()
            if (nextPins != null) ui = ui.copy(pinnedMemories = nextPins)
            toast("已取消钉住")
        } catch (e: Exception) {
            ui = ui.copy(pinnedMemories = prevPins)
            throw e
        }
    }
}

internal fun NativePulseModel.loadWatchdogs() {
    perform {
        val res = request("GET", "/api/watchdogs")
        ui = ui.copy(watchdogs = res.optJSONArray("watchdogs")?.objects() ?: emptyList())
    }
}

internal fun NativePulseModel.stopWatchdog(watchdogId: String) {
    val prevWatchdogs = ui.watchdogs
    ui = ui.copy(watchdogs = prevWatchdogs.filterNot {
        it.optString("task_id") == watchdogId || it.optString("watchdog_id") == watchdogId || it.optString("id") == watchdogId
    })
    perform {
        try {
            request("POST", "/api/watchdogs/$watchdogId/stop")
            refreshSchedulesSilently()
            toast("已停止哨兵监控：$watchdogId")
        } catch (e: Exception) {
            ui = ui.copy(watchdogs = prevWatchdogs)
            throw e
        }
    }
}

internal fun NativePulseModel.switchAdjacentBot(delta: Int) {
    val list = visibleBots().ifEmpty { ui.bots.filter { !it.hidden } }
    if (list.size <= 1) return
    val curIdx = list.indexOfFirst { it.id == ui.botId }.coerceAtLeast(0)
    val nextIdx = (curIdx + delta + list.size) % list.size
    val nextBot = list[nextIdx]
    ui = ui.copy(lastSwitchDir = if (delta >= 0) 1 else -1)
    open(nextBot.id)
}

internal fun NativePulseModel.togglePinBot(targetBotId: String) {
    val target = bot(targetBotId) ?: return
    placement(target.id, "pinned", !target.pinned)
}

internal fun NativePulseModel.toggleAppearanceQuick() {
    val next = if (ui.appearance == "light") "dark" else "light"
    updateSettingsField(appearance = next)
}

internal fun NativePulseModel.togglePowerSave(enabled: Boolean) {
    PowerSaveController.setManualPowerSave(activity, enabled)
    updateSettingsField(powerSave = enabled)
}

internal fun NativePulseModel.stopBotById(targetBotId: String) {
    stop(targetBotId)
}

internal fun NativePulseModel.saveApiChannel(
    id: String,
    name: String,
    baseUrl: String,
    apiKey: String,
    priority: Int,
    enabled: Boolean = true,
    autoFetch: Boolean = true,
    onDone: () -> Unit = {}
) {
    val cleanUrl = baseUrl.trim().trimEnd('/')
    if (cleanUrl.isBlank()) {
        toast("请填写渠道 API Base URL")
        return
    }
    perform {
        val body = JSONObject()
            .put("id", id.trim())
            .put("name", name.trim())
            .put("base_url", cleanUrl)
            .put("api_key", apiKey.trim())
            .put("priority", priority.coerceIn(0, 1000))
            .put("enabled", enabled)
            .put("auto_fetch", autoFetch)
        val res = request("POST", "/api/channels", body)
        val updated = PulseApiChannel.listFromJson(res.optJSONArray("channels"))
        val testObj = res.optJSONObject("test")
        val mergedModels = linkedSetOf<String>().apply {
            addAll(ui.availableModels)
            updated.filter { it.enabled }.forEach { ch -> addAll(ch.models) }
        }.toList()
        ui = ui.copy(channels = updated, availableModels = mergedModels, modelEntries = PulseModelEntry.synthesize(mergedModels, updated))
        val testTip = when {
            testObj == null -> "已保存渠道"
            testObj.optBoolean("ok", false) -> "渠道已保存并拉取 ${testObj.optJSONArray("models")?.length() ?: 0} 个模型 (${testObj.optInt("latency_ms", 0)}ms)"
            else -> "渠道已保存，测速提示：${testObj.optString("error", "拉取失败").take(50)}"
        }
        toast(testTip)
        onDone()
    }
}

internal fun NativePulseModel.toggleApiChannel(ch: PulseApiChannel, enabled: Boolean) {
    saveApiChannel(
        id = ch.id,
        name = ch.name,
        baseUrl = ch.baseUrl,
        apiKey = "",
        priority = ch.priority,
        enabled = enabled,
        autoFetch = false
    )
}

internal fun NativePulseModel.testApiChannel(channelId: String) {
    if (ui.testingChannelId != null) return
    ui = ui.copy(testingChannelId = channelId)
    perform {
        try {
            val res = request("POST", "/api/channels/$channelId/test")
            val updated = res.optJSONArray("channels")?.let { PulseApiChannel.listFromJson(it) } ?: ui.channels
            val mergedModels = linkedSetOf<String>().apply {
                addAll(ui.availableModels)
                updated.filter { it.enabled }.forEach { ch -> addAll(ch.models) }
            }.toList()
            ui = ui.copy(channels = updated, availableModels = mergedModels, modelEntries = PulseModelEntry.synthesize(mergedModels, updated))
            if (res.optBoolean("ok", false)) {
                toast("连通成功 · ${res.optInt("count", 0)} 个模型 · ${res.optInt("latency_ms", 0)}ms")
            } else {
                toast("连通失败：${res.optString("error", "未知错误").take(60)}")
            }
        } finally {
            ui = ui.copy(testingChannelId = null)
        }
    }
}

internal fun NativePulseModel.deleteApiChannel(channelId: String) {
    perform {
        val res = request("DELETE", "/api/channels/$channelId")
        val updated = PulseApiChannel.listFromJson(res.optJSONArray("channels"))
        ui = ui.copy(channels = updated, modelEntries = PulseModelEntry.synthesize(ui.availableModels, updated)); toast("已删除渠道")
    }
}
