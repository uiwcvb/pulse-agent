package dev.bubuki.proactive

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.os.Bundle
import android.os.SystemClock
import android.window.BackEvent
import android.window.OnBackAnimationCallback
import android.window.OnBackInvokedCallback
import android.window.OnBackInvokedDispatcher
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.compose.setContent
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.graphics.ImageBitmap
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Mutex
import org.json.JSONArray
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.Executors
import java.util.concurrent.Future

/**
 * [Module] NativePulseActivity
 * [Responsibility]
 * 原生宿主 Activity、領域數據模型與核心對話會話控制器：管理 Activity 生命周期與預測式返回手勢、
 * 定義不可變 UI 狀態樹 (`NativePulseState`, `PulseBot`, `PulseMessage`, `PulseAttachment`, `PulseChoice`)，
 * 並封裝核心對話會話操作（打開對話、草稿持久化、發送消息、中止回合、編輯重發與重新生成）。
 * 網絡同步與流式狀態機委派至 [PulseActivitySync]，Bot/Vault/設置管理委派至 [PulseActivityBotOps]，
 * 消息編輯/重生成/分支切換委派至 [PulseChatActions]，多媒體與雲端桌面/權限交互委派至 [PulseActivityMedia]。
 *
 * [Public API]
 * - `NativePulseActivity`
 * - `PulseBot`, `PulseAttachment`, `PulseChoice`, `PulseMessage`, `PulseMenuItem`, `PulseMenu`, `NativePulseState`
 * - `NativePulseModel`: `open(...)`, `back()`, `setDraft(...)`, `send()`, `stop(...)`, `editMessageAndResend(...)`, `rerollMessage(...)`
 */
class NativePulseActivity : ComponentActivity() {
    internal lateinit var model: NativePulseModel
    private var resumed = false
    private var poll: Job? = null
    private var backHandler: OnBackInvokedCallback? = null
    private val documentPicker = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris -> model.documentsPicked(uris) }
    private val visualMediaPicker = registerForActivityResult(ActivityResultContracts.PickMultipleVisualMedia(8)) { uris -> model.documentsPicked(uris) }
    private var pendingCameraUri: android.net.Uri? = null
    private val cameraLauncher = registerForActivityResult(ActivityResultContracts.TakePicture()) { ok ->
        val uri = pendingCameraUri
        pendingCameraUri = null
        model.documentsPicked(if (ok && uri != null) listOf(uri) else emptyList())
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        pendingCameraUri = savedInstanceState?.getString("pending_camera_uri")?.let(android.net.Uri::parse)
        if (android.os.Build.VERSION.SDK_INT >= 33 && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 1001)
        }
        model = NativePulseModel(this, savedInstanceState?.getString("chat_bot") ?: intent.getStringExtra("openBot"))
        model.restorePicker(savedInstanceState)
        if (savedInstanceState == null) {
            model.handleIncomingShareIntent(intent)
        }
        setContent { NativePulseScreen(model) }
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            val callback = if (android.os.Build.VERSION.SDK_INT >= 34) object : OnBackAnimationCallback {
                override fun onBackStarted(event: BackEvent) { if (model.ui.chatOpen && model.ui.menu == null && model.ui.activePanel == null) model.predictiveBack = 0f }
                override fun onBackProgressed(event: BackEvent) { if (model.predictiveBack != null) model.predictiveBack = event.progress }
                override fun onBackCancelled() { model.cancelBack() }
                override fun onBackInvoked() { handleNativeBack() }
            } else OnBackInvokedCallback { handleNativeBack() }
            backHandler = callback
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT, callback)
        } else onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() { handleNativeBack() }
        })
    }
    fun pickDocument() { documentPicker.launch(arrayOf("*/*")) }
    fun pickVisualMedia() { visualMediaPicker.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)) }
    fun captureQuickPhoto() {
        val dir = java.io.File(cacheDir, "camera-captures").apply { mkdirs() }
        val photoFile = java.io.File(dir, "photo_${System.currentTimeMillis()}.jpg")
        val uri = androidx.core.content.FileProvider.getUriForFile(this, "${packageName}.avatar-files", photoFile)
        pendingCameraUri = uri
        try {
            cameraLauncher.launch(uri)
        } catch (err: Throwable) {
            pendingCameraUri = null
            throw err
        }
    }
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        intent.getStringExtra("openBot")?.let(model::requestOpenBot)
        if (intent.getStringExtra("entryRoute") == "settings") model.openSettings()
        model.handleIncomingShareIntent(intent)
    }
    private fun handleNativeBack() {
        when {
            model.ui.menu != null -> model.dismissMenu()
            model.ui.activePanel != null -> model.closePanel()
            model.ui.searching -> model.search(null)
            model.ui.chatOpen -> model.back()
            else -> finish()
        }
    }
    override fun onResume() {
        super.onResume(); resumed = true
        AgentForegroundService.ensureHealthyConnection(applicationContext)
        model.resume(); model.resumePendingApkInstallIfReady()
        FloatingVirtualDisplay.promoteToSystemOverlayIfPermitted(applicationContext)
        poll?.cancel()
        poll = model.scope.launch {
            var tick = 0
            while (isActive && (resumed || model.hasActiveRemoteTurn())) {
                if (resumed && tick > 0 && tick % 5 == 0) {
                    AgentForegroundService.ensureHealthyConnection(applicationContext)
                }
                val wsOnline = AgentForegroundService.connected
                val fast = model.hasInFlightWork()
                if (resumed && model.ui.activePanel == "cloud_desktop") {
                    if (!wsOnline && tick % 2 == 0) {
                        model.refreshStreamOnly()
                    }
                    tick++
                    delay(if (wsOnline) 4000L else 1800L)
                } else if (fast) {
                    tick++
                    if (wsOnline) {
                        delay(4_000L)
                        if (isActive && resumed && model.active) {
                            model.refreshStreamOnly()
                        }
                    } else {
                        model.refreshStreamOnly()
                        delay(if (!resumed) 4500L else 800L)
                    }
                } else {
                    if (!resumed) break
                    tick++
                    if (!wsOnline) {
                        if (tick % 4 == 1) {
                            model.refresh()
                        } else {
                            model.refreshStreamOnly()
                        }
                        delay(5500L)
                    } else {
                        delay(if (PowerSaveController.isPowerSaveActive(this@NativePulseActivity)) 180_000L else 120_000L)
                        if (isActive && resumed) {
                            model.refreshStreamOnly()
                        }
                    }
                }
            }
        }
    }
    override fun onPause() { resumed = false; if (!model.hasActiveRemoteTurn()) poll?.cancel(); model.pause(); model.persistDrafts(); FloatingVirtualDisplay.promoteToSystemOverlayIfPermitted(applicationContext); super.onPause() }
    override fun onStop() { resumed = false; if (!model.hasActiveRemoteTurn()) poll?.cancel(); model.pause(); FloatingVirtualDisplay.promoteToSystemOverlayIfPermitted(applicationContext); super.onStop() }
    override fun onSaveInstanceState(outState: Bundle) { if (model.ui.chatOpen) outState.putString("chat_bot", model.ui.botId); pendingCameraUri?.let { outState.putString("pending_camera_uri", it.toString()) }; model.savePicker(outState); super.onSaveInstanceState(outState) }
    override fun onDestroy() {
        if (android.os.Build.VERSION.SDK_INT >= 33) backHandler?.let { getOnBackInvokedDispatcher().unregisterOnBackInvokedCallback(it) }
        model.close(); super.onDestroy()
    }
}

internal class NativePulseModel(internal val activity: NativePulseActivity, restoredBot: String?) {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    var ui by mutableStateOf(NativePulseState(botId = restoredBot ?: "companion", chatOpen = restoredBot != null)); internal set
    var predictiveBack by mutableStateOf<Float?>(null)
    var backFrom: Float? = null
    var backSettlement by mutableIntStateOf(0)
    var navigationRunning = false
    @Volatile internal var prefs = JSONObject()
    internal val drafts = mutableMapOf<String, String>()
    internal val attachmentDrafts = mutableMapOf<String, List<PulseAttachment>>()
    internal var localRevision = 0
    internal var pendingOpenBot: String? = restoredBot
    internal var pickerBot: String? = null
    internal var pickerSource: String? = null
    internal val draftLock = Any()
    internal val draftWriter = Executors.newSingleThreadExecutor { task -> Thread(task, "native-drafts").apply { priority = Thread.MIN_PRIORITY } }
    internal var draftJob: Job? = null
    internal var settingsWriteJob: Job? = null
    internal var revision = 0
    internal var active = false
    internal val refreshLock = Mutex()
    internal val streamLock = Mutex()
    internal var lastStreamSeq: Long = 0L
    internal var confirmedSyncSeq: Long = 0L
    @Volatile internal var refreshGeneration: Long = 0L
    internal var lastServerBootId: String = ""
    internal var pendingReconnectSync: Boolean = false
    internal var lastMessagesEpoch: Long = 0L
    internal var lastStateSeq: Long = 0L
    internal var lastBotsVer: String = ""
    internal var lastPinsVer: String = ""
    internal var lastConfigVer: String = ""
    internal var lastSchedulesVer: String = ""
    internal var currentTurnId: String = ""
    internal var activeStreamTurnId: String = ""
    internal val completedTurnIds = LinkedHashSet<String>()
    internal val switchingVariantMsgIds = mutableSetOf<String>()
    internal val liveProcessExpandedByBot = mutableMapOf<String, Boolean>()
    internal var sendGuardUntilUptime: Long = 0L
    internal var sendGuardBotId: String? = null
    internal var stoppedGuardBotId: String? = null
    internal var stoppedGuardUntilUptime: Long = 0L
    internal var streamPacerJob: Job? = null
    internal var targetStreamFullText: String = ""
    internal val avatarLoading = mutableSetOf<String>()
    internal val avatarVersions = mutableMapOf<String, String>()
    internal val optimisticList = mutableListOf<PulseMessage>()
    internal var inFlightSends = 0
    internal var remoteMessages: List<PulseMessage> = emptyList()
    internal var remoteWorkingBot: String? = null
    internal var remoteWorkingText = ""
    internal var cloudStreamJob: Job? = null
    @Volatile internal var pendingApkInstallPath: String? = null
    @Volatile internal var lastCloudFrameRefreshAtMs: Long = 0L
    internal var effectiveUpdateServerUrl: String? = null
    internal var resolvedApkDownloadUrl: String? = null
    internal val clock = SimpleDateFormat("HH:mm", Locale.CHINA).apply { timeZone = TimeZone.getTimeZone("Asia/Shanghai") }
    internal val calendar = SimpleDateFormat("M月d日", Locale.CHINA).apply { timeZone = TimeZone.getTimeZone("Asia/Shanghai") }
    companion object {
        @Volatile private var activeInstance: NativePulseModel? = null
        @JvmStatic internal fun setActiveInstance(inst: NativePulseModel?) {
            activeInstance = inst
        }
        @JvmStatic internal fun clearActiveInstanceIf(inst: NativePulseModel) {
            if (activeInstance === inst) activeInstance = null
        }
        @JvmStatic fun dispatchRealtimeEvent(event: JSONObject) {
            activeInstance?.onRealtimeEvent(event)
        }
        @JvmStatic fun isForeground(): Boolean = activeInstance?.active == true
        @JvmStatic fun syncOnReconnect() {
            activeInstance?.let { inst ->
                inst.scope.launch { runCatching { inst.syncDeltaOnReconnect() } }
            }
        }
    }
    init {
        activeInstance = this
        prefs = runCatching { UiPreferences.decorate(activity, SecurePrefs.read(activity)) }.getOrElse { JSONObject() }
        val stored = prefs.optJSONObject("drafts") ?: JSONObject()
        stored.keys().forEach { drafts[it] = stored.optString(it) }
        readAttachments(prefs)
        val cached = homeCache(prefs)
        remoteMessages = cached.third
        confirmedSyncSeq = remoteMessages
            .filter { !it.optimistic && !isEphemeralMsgId(it.id) }
            .maxOfOrNull { it.orderSeq } ?: 0L
        val initialBot = cached.first.firstOrNull { it.id == restoredBot }?.id ?: cached.second
        val savedAppearance = prefs.optString("appearance", "dark").let { if (it == "light") "light" else "dark" }
        val dark = savedAppearance != "light"
        val cfg = prefs.optJSONObject("config") ?: JSONObject()
        val defModel = cfg.optString("default_model").ifBlank { cfg.optJSONObject("big_brain")?.optString("model").orEmpty().ifBlank { cfg.optJSONObject("small_brain")?.optString("model").orEmpty() } }
        val defUrl = cfg.optString("api_base_url").ifBlank { cfg.optJSONObject("big_brain")?.optString("base_url").orEmpty().ifBlank { "https://api.openai.com/v1" } }
        val savedServerUrl = prefs.optString("serverUrl", SecureTransport.DEFAULT_SERVER).trim()
        val savedToken = prefs.optString("deviceToken", SecureTransport.defaultToken()).ifBlank { SecureTransport.defaultToken() }.trim()
        ui = ui.copy(
            bots = cached.first, primary = cached.second, botId = initialBot,
            messages = combinedMessages(),
            chatOpen = restoredBot != null && cached.first.any { it.id == restoredBot },
            activePanel = null,
            dark = dark, paired = savedServerUrl.isNotBlank() && savedToken.isNotBlank(),
            draft = draft(initialBot), attachments = attachments(initialBot), mode = draftMode(initialBot),
            defaultModel = defModel, apiBaseUrl = defUrl, serverUrl = savedServerUrl, deviceTokenInput = savedToken,
            backgroundService = prefs.optBoolean("background", true), appearance = savedAppearance,
            motion = prefs.optBoolean("motion", true) && !animationsOff(activity), haptics = prefs.optBoolean("haptics", true),
            powerSave = prefs.optBoolean("power_save", false)
        )
        activity.systemBarsFor(dark)
        RootPhoneHarness.initialize(activity)
        scope.launch {
            var lastPhoneSig = ""
            RootPhoneHarness.changes.collect { st ->
                val sig = "${st.active}|${st.botId}|${st.phase}|${st.messages.size}|${st.messages.lastOrNull()?.id}|${st.messages.lastOrNull()?.text?.length}"
                if (sig != lastPhoneSig) {
                    lastPhoneSig = sig
                    applyPhoneState()
                }
            }
        }
    }
    fun visibleBots(): List<PulseBot> = ui.bots.filter { bot ->
        !bot.hidden && (ui.query.isBlank() || "${bot.name} ${bot.description} ${ui.messages.lastOrNull { it.botId == bot.id }?.text.orEmpty()}".contains(ui.query, true))
    }.sortedWith(compareByDescending<PulseBot> { it.pinned }.thenByDescending { if (it.pinned) it.pinnedAt else 0.0 }
        .thenByDescending { b ->
            val lastCreated = ui.messages.lastOrNull { it.botId == b.id }?.created ?: 0.0
            val activeNow = ui.workingBot == b.id || ui.streamBotId == b.id || ui.busyBot == b.id
            if (activeNow) maxOf(lastCreated, System.currentTimeMillis() / 1000.0) else lastCreated
        })
    fun bot(id: String = ui.botId): PulseBot? = ui.bots.find { it.id == id }
    fun time(created: Double): String = clock.format(Date((created * 1000).toLong()))
    fun day(created: Double): String = calendar.format(Date((created * 1000).toLong()))
    fun timeSeparator(created: Double): String {
        val ms = (created * 1000).toLong()
        val nowDay = calendar.format(Date(System.currentTimeMillis()))
        val msgDay = calendar.format(Date(ms))
        return if (nowDay == msgDay) time(created) else "$msgDay ${time(created)}"
    }
    fun open(id: String) {
        if (bot(id) == null) return
        val switchingBot = ui.botId != id
        if (switchingBot) {
            streamPacerJob?.cancel()
            targetStreamFullText = ""
        }
        ui = ui.copy(
            botId = id, chatOpen = true, activePanel = null, mode = draftMode(id), reply = null,
            draft = draft(id), attachments = attachments(id), menu = null,
            streamBotId = if (switchingBot && ui.streamBotId != id) null else ui.streamBotId,
            workingBot = if (switchingBot && ui.workingBot != id) null else ui.workingBot,
            streamText = if (switchingBot && ui.streamBotId != id) "" else ui.streamText,
            streamThought = if (switchingBot && ui.streamBotId != id) "" else ui.streamThought,
            streamTool = if (switchingBot && ui.streamBotId != id) "" else ui.streamTool,
            streamTrace = if (switchingBot && ui.streamBotId != id) emptyList() else ui.streamTrace
        )
        scope.launch { refreshStreamOnly() }
        if (ui.pinnedMemories.isEmpty() && lastPinsVer.isBlank()) loadMemoryPins()
        fetchStickerPacks()
        acknowledge()
    }
    fun requestOpenBot(id: String) { if (id.isBlank()) return; pendingOpenBot = id; if (bot(id) != null) { pendingOpenBot = null; open(id) }; scope.launch { refresh() } }
    fun back() { predictiveBack?.let { backFrom = 1f - it }; ui = ui.copy(chatOpen = false, menu = null, activePanel = null); predictiveBack = null }
    fun cancelBack() { predictiveBack?.let { backFrom = 1f - it }; predictiveBack = null; backSettlement++ }
    fun search(value: String?) { ui = ui.copy(searching = value != null, query = value ?: "") }
    fun setDraft(value: String) {
        if (value.length > 12000) return
        synchronized(draftLock) { drafts[ui.botId] = value }
        localRevision++
        ui = ui.copy(draft = value)
        draftJob?.cancel(); draftJob = scope.launch { delay(600); persistDrafts() }
    }
    internal fun draft(id: String) = synchronized(draftLock) { drafts[id] ?: "" }
    fun draftOf(id: String): String = draft(id)
    fun persistDrafts(): Future<*> {
        val snapshot = synchronized(draftLock) { drafts.toMap() }
        val files = synchronized(draftLock) { attachmentDrafts.toMap() }
        val appCtx = activity.applicationContext
        return draftWriter.submit {
            runCatching {
                prefs = SecurePrefs.update(appCtx) { current ->
                    val stored = current.optJSONObject("drafts") ?: JSONObject()
                    snapshot.forEach { (id, value) -> stored.put(id, value) }
                    val attached = current.optJSONObject("attachmentDrafts") ?: JSONObject()
                    files.forEach { (id, value) -> attached.put(id, JSONArray(value.map { it.json() })) }
                    current.put("drafts", stored).put("attachmentDrafts", attached)
                }
            }
        }
    }
    fun secondary(view: String, action: String = "", path: String = "") {
        dismissMenu()
        when {
            view in listOf("settings", "config") -> openSettings()
            view == "vault" -> openVault(path.ifBlank { "INDEX.md" })
            view == "computer" -> openCloudDesktop()
            view == "profile" -> openBotEditor(ui.botId)
            action == "createBot" -> openBotEditor(null)
            view in listOf("local", "root") -> {
                if (action == "diagnostic") {
                    activity.startActivity(Intent(activity, NativePhoneActivity::class.java))
                } else {
                    launchFloatingVirtualDisplay()
                }
            }
        }
    }
    fun dismissMenu() { ui = ui.copy(menu = null) }
    internal fun menu(title: String, items: List<PulseMenuItem>) { ui = ui.copy(menu = PulseMenu(title, items)) }
    internal fun perform(task: suspend () -> Unit) { scope.launch { try { task() } catch (e: Exception) { if (e is CancellationException) throw e; toast(PulseErrors.safeError(e)) } } }
    fun toast(message: String) { ui = ui.copy(toast = message, toastId = ui.toastId + 1) }
    internal fun ensurePaired(): Boolean { if (ui.paired) return true; openSettings(); return false }
    internal fun hasActiveRemoteTurn(): Boolean =
        inFlightSends > 0 || remoteWorkingBot != null || ui.streamBotId != null || ui.busyBot != null

    internal fun clearStreamUi(targetBot: String? = null, clearWorking: Boolean = false) {
        val matchesStream = targetBot == null || ui.streamBotId == null || ui.streamBotId == targetBot
        val matchesWork = targetBot == null || ui.workingBot == null || ui.workingBot == targetBot
        if (matchesStream) {
            activeStreamTurnId = ""
            streamPacerJob?.cancel()
            targetStreamFullText = ""
        }
        if (clearWorking && matchesWork) {
            if (targetBot == null || remoteWorkingBot == targetBot) remoteWorkingBot = null
            if (targetBot == null || sendGuardBotId == targetBot) {
                sendGuardBotId = null
                sendGuardUntilUptime = 0L
            }
        }
        ui = ui.copy(
            busyBot = if (clearWorking && (targetBot == null || ui.busyBot == targetBot)) null else ui.busyBot,
            workingBot = if (clearWorking && matchesWork) null else ui.workingBot,
            streamBotId = if (matchesStream) null else ui.streamBotId,
            streamSpeakerId = if (matchesStream) null else ui.streamSpeakerId,
            streamSpeakerName = if (matchesStream) "" else ui.streamSpeakerName,
            streamText = if (matchesStream) "" else ui.streamText,
            streamThought = if (matchesStream) "" else ui.streamThought,
            streamStepText = if (matchesStream) "" else ui.streamStepText,
            streamTool = if (matchesStream) "" else ui.streamTool,
            streamTrace = if (matchesStream) emptyList() else ui.streamTrace,
            streamTurnSteps = if (matchesStream) emptyList() else ui.streamTurnSteps,
            streamTimeline = if (matchesStream) emptyList() else ui.streamTimeline,
            streamChoices = if (matchesStream) emptyList() else ui.streamChoices,
            streamSubAgents = if (matchesStream) emptyList() else ui.streamSubAgents,
            streamTodos = if (matchesStream) emptyList() else ui.streamTodos,
            streamStartedAt = if (matchesStream) 0L else ui.streamStartedAt
        )
    }

    fun sendQuickPrompt(prompt: String) {
        sendDirect(prompt = prompt, stickerUrl = "", bypassReplyAndEdit = true)
    }
    fun sendSticker(code: String, url: String) {
        sendDirect(prompt = code.ifBlank { "[表情]" }, stickerUrl = url, bypassReplyAndEdit = true)
    }
    fun sendDirect(
        prompt: String,
        stickerUrl: String = "",
        bypassReplyAndEdit: Boolean = true,
        targetBotId: String = ui.botId
    ) {
        val clean = prompt.trim()
        if (clean.isEmpty() && stickerUrl.isBlank()) return
        if (!bypassReplyAndEdit) {
            ui.editingMessage?.let { editing ->
                editMessageAndResend(editing, clean)
                return
            }
        }
        if (!ensurePaired()) return
        val replyMsg = if (bypassReplyAndEdit) null else ui.reply
        sendPayload(
            id = targetBotId,
            rawText = clean,
            sentAttachments = emptyList(),
            stickerUrl = stickerUrl,
            replyMsg = replyMsg,
            clearComposerDraft = false
        )
    }
    fun consumeChoiceAndSend(messageId: String, prompt: String) {
        if (prompt.isBlank()) return
        val updated = (ui.consumedChoiceIds + messageId).let { set ->
            if (set.size > 200) set.drop(set.size - 200).toSet() else set
        }
        ui = ui.copy(
            consumedChoiceIds = updated,
            streamChoices = emptyList()
        )
        sendDirect(prompt = prompt, bypassReplyAndEdit = true)
    }
    fun retryMessage(msg: PulseMessage) {
        if ((msg.optimistic || isEphemeralMsgId(msg.id)) && msg.role == "user") {
            optimisticList.removeAll { it.id == msg.id }
            ui = ui.copy(messages = combinedMessages())
            sendDirect(
                prompt = msg.text.ifBlank { if (msg.sticker.isNotBlank()) "[表情]" else "" },
                stickerUrl = msg.sticker,
                bypassReplyAndEdit = true,
                targetBotId = msg.botId
            )
        } else {
            rerollMessage(msg)
        }
    }
    fun job(kind: String) {
        dismissMenu(); if (!ensurePaired() || ui.busyBot != null) return
        val id = if (kind == "curiosity") ui.primary else ui.botId
        ui = ui.copy(busyBot = id)
        perform {
            try {
                when (kind) {
                    "curiosity" -> { open(id); request("POST", "/api/trigger-curiosity") }
                    "research" -> request("POST", "/api/research", JSONObject().put("bot_id", id))
                    "snooze", "wake" -> { request("POST", "/api/snooze", JSONObject().put("minutes", if (kind == "snooze") 60 else 0)); toast(if (kind == "snooze") "好，安静一小时。" else "已恢复主动联系。") }
                }
                refresh()
            } finally { ui = ui.copy(busyBot = null) }
        }
    }
    fun send() {
        ui.editingMessage?.let { editing ->
            editMessageAndResend(editing, ui.draft)
            return
        }
        val typedDraft = ui.draft.trim()
        val raw = typedDraft.ifEmpty { if (ui.attachments.isNotEmpty()) "请查看这些附件。" else "" }
        if (raw.isEmpty() || ui.uploading) return
        if (!ensurePaired()) return
        sendPayload(
            id = ui.botId,
            rawText = raw,
            sentAttachments = ui.attachments,
            stickerUrl = "",
            replyMsg = ui.reply,
            clearComposerDraft = true,
            restoreDraftText = typedDraft
        )
    }
    private fun sendPayload(
        id: String,
        rawText: String,
        sentAttachments: List<PulseAttachment>,
        stickerUrl: String,
        replyMsg: PulseMessage?,
        clearComposerDraft: Boolean,
        restoreDraftText: String = ""
    ) {
        val withFiles = rawText + if (sentAttachments.isEmpty()) "" else "\n\n附件（工作区相对路径，可用 read_file 或 run_bash 读取）：\n" + sentAttachments.joinToString("\n") { it.path.removePrefix("workspace/") }
        val text = replyMsg?.let { "> " + it.text.take(600).replace("\n", "\n> ") + "\n\n" + withFiles } ?: withFiles
        val pending = PulseMessage(
            "pending-${System.currentTimeMillis()}-${optimisticList.size}",
            id,
            "user",
            text,
            "",
            "",
            System.currentTimeMillis() / 1000.0,
            "",
            "[]",
            true,
            sticker = stickerUrl
        )
        optimisticList.add(pending)
        inFlightSends++
        if (clearComposerDraft) {
            draftJob?.cancel()
            synchronized(draftLock) {
                drafts[id] = ""
                attachmentDrafts[id] = attachments(id).filterNot { a -> sentAttachments.any { it.path == a.path && it.source == a.source } }
            }
            localRevision++
            persistDrafts()
        }
        stoppedGuardBotId = null
        stoppedGuardUntilUptime = 0L
        streamPacerJob?.cancel()
        targetStreamFullText = ""
        activeStreamTurnId = ""
        sendGuardBotId = id
        sendGuardUntilUptime = SystemClock.uptimeMillis() + 15_000L
        currentTurnId = "pending-${System.currentTimeMillis()}"
        ui = ui.copy(
            draft = if (clearComposerDraft && ui.botId == id) "" else ui.draft,
            attachments = if (clearComposerDraft && ui.botId == id) attachments(id) else ui.attachments,
            attachmentThumbs = if (clearComposerDraft) sentAttachments.fold(ui.attachmentThumbs) { acc, a -> acc - a.path } else ui.attachmentThumbs,
            busyBot = id,
            workingBot = id,
            workingText = "正在思考…",
            streamBotId = id,
            streamText = "",
            streamThought = "正在向云端发送指令并建立实时流…",
            streamStepText = "",
            streamTool = "",
            streamTrace = emptyList(),
            streamTurnSteps = emptyList(),
            streamTimeline = emptyList(),
            streamChoices = emptyList(),
            streamSubAgents = emptyList(),
            streamTodos = emptyList(),
            streamStartedAt = System.currentTimeMillis(),
            reply = if (replyMsg != null) null else ui.reply,
            messages = combinedMessages()
        )
        perform {
            try {
                val payload = JSONObject()
                    .put("text", text)
                    .put("bot_id", id)
                    .put("mode", "auto")
                    .put("async_ack", true)
                    .put("client_msg_id", pending.id)
                if (stickerUrl.isNotBlank()) payload.put("sticker", stickerUrl)
                val res = request("POST", "/api/chat", payload)
                val respEpoch = res.optLong("messages_epoch", 0L)
                if (respEpoch > 0L) lastMessagesEpoch = respEpoch
                val seq = res.optLong("seq", 0L)
                if (seq > lastStreamSeq) lastStreamSeq = seq
                val turnId = res.optString("turn_id", "")
                val isQueued = res.optBoolean("queued", false)
                if (turnId.isNotBlank()) {
                    if (isQueued) {
                        completedTurnIds.remove(turnId)
                        currentTurnId = turnId
                        sendGuardBotId = id
                        sendGuardUntilUptime = SystemClock.uptimeMillis() + 10_000L
                    } else {
                        markTurnCompleted(turnId)
                        sendGuardBotId = null
                        sendGuardUntilUptime = 0L
                    }
                }
                val incoming = mutableListOf<PulseMessage>()
                res.optJSONArray("recent_tail")?.objects()?.mapNotNull { runCatching { PulseMessage.from(it) }.getOrNull() }?.let { incoming.addAll(it) }
                res.optJSONObject("user_message")?.let { uObj ->
                    runCatching { PulseMessage.from(uObj) }.getOrNull()?.let { incoming.add(it) }
                }
                if (res.has("id") && res.has("text")) {
                    runCatching { PulseMessage.from(res) }.getOrNull()?.let { incoming.add(it) }
                }
                if (incoming.isNotEmpty()) {
                    mergeRemoteMessages(incoming, fullReplace = false)
                }
                optimisticList.removeAll { it.id == pending.id }
                if (isQueued) {
                    remoteWorkingBot = id
                    ui = ui.copy(
                        messages = combinedMessages(),
                        workingBot = id,
                        streamBotId = ui.streamBotId ?: id
                    )
                } else {
                    remoteWorkingBot = null
                    clearStreamUi(targetBot = id, clearWorking = true)
                    ui = ui.copy(messages = combinedMessages())
                    refresh(force = true)
                }
            } catch (e: Exception) {
                if (e is CancellationException) throw e
                runCatching { refresh(force = true) }
                val delivered = remoteMessages.takeLast(20).any {
                    it.role == "user" && it.botId == id && (
                        it.clientMsgId == pending.id ||
                            (it.created >= pending.created - 5.0 && it.text.trim() == text.trim())
                    )
                }
                if (delivered) {
                    optimisticList.removeAll { it.id == pending.id }
                    ui = ui.copy(messages = combinedMessages())
                } else {
                    optimisticList.removeAll { it.id == pending.id }
                    if (clearComposerDraft) {
                        synchronized(draftLock) {
                            if (restoreDraftText.isNotBlank() && (drafts[id].isNullOrBlank())) {
                                drafts[id] = restoreDraftText
                            }
                            if (sentAttachments.isNotEmpty()) {
                                val curAtts = attachments(id)
                                attachmentDrafts[id] = (sentAttachments + curAtts).distinctBy { it.source to it.path }
                            }
                        }
                        if (ui.botId == id) {
                            ui = ui.copy(
                                draft = draft(id),
                                attachments = attachments(id)
                            )
                        }
                        persistDrafts()
                    }
                    clearStreamUi(targetBot = id, clearWorking = true)
                    ui = ui.copy(messages = combinedMessages())
                    throw e
                }
            } finally {
                inFlightSends = (inFlightSends - 1).coerceAtLeast(0)
                if (inFlightSends == 0 && ui.busyBot == id) ui = ui.copy(busyBot = null)
            }
        }
    }
    fun stop(targetBot: String = ui.botId) {
        val phone = RootPhoneHarness.changes.value
        val hasPhoneToolInTurn = (phone.active && phone.botId == targetBot) ||
            (ui.streamBotId == targetBot && (ui.streamTool == "phone_workspace" || ui.streamTool == "phone" ||
                ui.streamTrace.any { val t = it.optString("tool"); t == "phone_workspace" || t == "phone" }))
        if (hasPhoneToolInTurn) {
            if (phone.active) {
                RootPhoneHarness.stop(activity, false)
            }
            AgentForegroundService.abortPhoneCommands(activity)
        }
        if (phone.active && phone.botId == targetBot) {
            if (ui.streamBotId != targetBot && remoteWorkingBot != targetBot) {
                return
            }
        }
        val isStreamingTarget = ui.streamBotId == null || ui.streamBotId == targetBot || ui.workingBot == targetBot
        val realStreamPart = if (isStreamingTarget) ui.streamText.takeIf { !isStreamPlaceholder(it) && it.isNotBlank() } else null
        val fallbackStepPart = if (isStreamingTarget && realStreamPart == null) {
            ui.streamStepText.trim().ifBlank {
                ui.streamTurnSteps.lastOrNull { it.optString("type") == "step_text" }?.optString("text")?.trim().orEmpty()
            }.takeIf { it.isNotBlank() && !isStreamPlaceholder(it) }?.let { "$it\n\n（已停止）" }
        } else null
        val partialText = realStreamPart ?: fallbackStepPart
        if (partialText != null && !(phone.active && phone.botId == targetBot)) {
            val frozenMsg = PulseMessage(
                "stopped-${System.currentTimeMillis()}",
                targetBot,
                "assistant",
                partialText,
                "chat",
                "small",
                System.currentTimeMillis() / 1000.0,
                "",
                JSONArray(ui.streamTrace).toString(),
                false,
                targetBot,
                bot(targetBot)?.name ?: targetBot,
                ui.streamChoices,
                thought = ui.streamThought.trim(),
                turnSteps = JSONArray(ui.streamTurnSteps).toString(),
                timeline = JSONArray(ui.streamTimeline).toString()
            )
            mergeRemoteMessages(listOf(frozenMsg), fullReplace = false)
        }
        if (currentTurnId.isNotBlank()) markTurnCompleted(currentTurnId)
        if (activeStreamTurnId.isNotBlank()) markTurnCompleted(activeStreamTurnId)
        stoppedGuardBotId = targetBot
        stoppedGuardUntilUptime = SystemClock.uptimeMillis() + 2_500L
        clearStreamUi(targetBot = targetBot, clearWorking = true)
        ui = ui.copy(messages = combinedMessages())
        val stopComputerToo = ui.computerBot == targetBot || ui.cloudDesktopLiveBotId == targetBot
        scope.launch {
            runCatching {
                request(
                    "POST",
                    "/api/stop",
                    JSONObject()
                        .put("bot_id", targetBot)
                        .put("source", SecureTransport.getNodeSource(activity))
                )
                if (stopComputerToo) {
                    runCatching { request("POST", "/api/computer/stop") }
                }
                refresh()
            }
        }
    }
    fun startEditingMessage(message: PulseMessage?) {
        val id = ui.botId
        if (message != null) {
            synchronized(draftLock) { drafts[id] = message.text }
            ui = ui.copy(editingMessage = message, draft = message.text)
        } else {
            ui = ui.copy(editingMessage = null)
        }
    }
    fun reply(message: PulseMessage?) { ui = ui.copy(reply = message) }
    fun copy(text: String) { activity.getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("DSB", text)); toast("已复制。") }
    fun confirm(message: PulseMessage, accepted: Boolean) = perform {
        request("POST", "/api/confirm", JSONObject().put("id", message.id).put("accepted", accepted)); refresh()
    }
    fun ackDemo(message: PulseMessage) = perform { request("POST", "/api/ack", JSONObject().put("ids", JSONArray().put(message.id))); refresh(); toast("演练已结束。") }
    fun close() { clearActiveInstanceIf(this); draftJob?.cancel(); settingsWriteJob?.cancel(); flushSettingsPersistence(); persistDrafts(); draftWriter.shutdown(); scope.cancel() }
}

