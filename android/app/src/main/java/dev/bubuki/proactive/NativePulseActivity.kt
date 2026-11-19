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
 * 多媒體與雲端桌面/權限交互委派至 [PulseActivityMedia]。
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
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
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
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        intent.getStringExtra("openBot")?.let(model::requestOpenBot)
        if (intent.getStringExtra("entryRoute") == "settings") model.openSettings()
        model.handleIncomingShareIntent(intent)
    }
    @Deprecated("Legacy detail-page result bridge")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 740 && resultCode == RESULT_OK) data?.getStringExtra("createdBot")?.let(model::requestOpenBot)
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
            while (isActive && (resumed || model.hasInFlightWork())) {
                if (resumed && tick % 5 == 0) {
                    AgentForegroundService.ensureHealthyConnection(applicationContext)
                }
                val wsOnline = AgentForegroundService.connected
                val fast = model.hasInFlightWork() || (resumed && model.ui.activePanel == "cloud_desktop")
                if (resumed && model.ui.activePanel == "cloud_desktop") {
                    if (!wsOnline) {
                        model.refreshStreamOnly()
                    }
                    delay(if (wsOnline) 3000L else 1100L)
                } else if (fast) {
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
                    if (!wsOnline) {
                        if (tick % 4 == 0) {
                            model.refresh()
                        } else {
                            model.refreshStreamOnly()
                        }
                        tick++
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
    override fun onPause() { resumed = false; if (!model.hasInFlightWork()) poll?.cancel(); model.pause(); model.persistDrafts(); FloatingVirtualDisplay.promoteToSystemOverlayIfPermitted(applicationContext); super.onPause() }
    override fun onStop() { resumed = false; if (!model.hasInFlightWork()) poll?.cancel(); model.pause(); FloatingVirtualDisplay.promoteToSystemOverlayIfPermitted(applicationContext); super.onStop() }
    override fun onSaveInstanceState(outState: Bundle) { if (model.ui.chatOpen) outState.putString("chat_bot", model.ui.botId); model.savePicker(outState); super.onSaveInstanceState(outState) }
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
    var backSettlement by mutableStateOf(0)
    var navigationRunning = false
    internal var prefs = JSONObject()
    internal val drafts = mutableMapOf<String, String>()
    internal val attachmentDrafts = mutableMapOf<String, List<PulseAttachment>>()
    internal var localRevision = 0
    internal var pendingOpenBot: String? = restoredBot
    internal var pickerBot: String? = null
    internal var pickerSource: String? = null
    internal val draftLock = Any()
    internal val draftWriter = Executors.newSingleThreadExecutor { task -> Thread(task, "native-drafts").apply { priority = Thread.MIN_PRIORITY } }
    internal var draftJob: Job? = null
    internal var revision = 0
    internal var active = false
    internal val refreshLock = Mutex()
    internal val streamLock = Mutex()
    internal var lastStreamSeq: Long = 0L
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
    internal var sendGuardUntilUptime: Long = 0L
    internal var sendGuardBotId: String? = null
    internal var stoppedGuardBotId: String? = null
    internal var stoppedGuardUntilUptime: Long = 0L
    internal var typewriterJob: Job? = null
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
    internal val stickerCache = android.util.LruCache<String, ImageBitmap>(48)
    internal val clock = SimpleDateFormat("HH:mm", Locale.CHINA).apply { timeZone = TimeZone.getTimeZone("Asia/Shanghai") }
    internal val calendar = SimpleDateFormat("M月d日", Locale.CHINA).apply { timeZone = TimeZone.getTimeZone("Asia/Shanghai") }
    companion object {
        @Volatile private var activeInstance: NativePulseModel? = null
        @JvmStatic internal fun setActiveInstance(inst: NativePulseModel?) {
            activeInstance = inst
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
        return draftWriter.submit {
            synchronized(SecurePrefs::class.java) {
                val current = SecurePrefs.read(activity); val stored = current.optJSONObject("drafts") ?: JSONObject()
                snapshot.forEach { (id, value) -> stored.put(id, value) }
                val attached = current.optJSONObject("attachmentDrafts") ?: JSONObject()
                files.forEach { (id, value) -> attached.put(id, JSONArray(value.map { it.json() })) }
                current.put("drafts", stored).put("attachmentDrafts", attached); SecurePrefs.save(activity, current)
            }
        }
    }
    fun secondary(view: String, action: String = "", path: String = "") {
        dismissMenu()
        when {
            view in listOf("settings", "config") -> { openSettings(); return }
            view == "vault" -> { openVault(path.ifBlank { "INDEX.md" }); return }
            view == "computer" -> { openCloudDesktop(); return }
            view == "profile" -> { openBotEditor(ui.botId); return }
            action == "createBot" -> { openBotEditor(null); return }
            view in listOf("local", "root") -> {
                if (action == "diagnostic") {
                    activity.startActivity(Intent(activity, NativePhoneActivity::class.java))
                } else {
                    launchFloatingVirtualDisplay()
                }
                return
            }
        }
        val selected = ui.botId; val selectedMode = ui.mode; val saved = persistDrafts()
        perform {
            withContext(Dispatchers.IO) { saved.get() }
            activity.startActivityForResult(Intent(activity, MainActivity::class.java).putExtra("nativeParent", true)
                .putExtra("entryRoute", view).putExtra("entryBot", selected).putExtra("entryAction", action).putExtra("entryMode", selectedMode).putExtra("entryPath", path), 740)
        }
    }
    fun dismissMenu() { ui = ui.copy(menu = null) }
    internal fun menu(title: String, items: List<PulseMenuItem>) { ui = ui.copy(menu = PulseMenu(title, items)) }
    internal fun perform(task: suspend () -> Unit) { scope.launch { try { task() } catch (e: Exception) { if (e is CancellationException) throw e; toast(MainActivity.safeError(e)) } } }
    fun toast(message: String) { ui = ui.copy(toast = message, toastId = ui.toastId + 1) }
    internal fun ensurePaired(): Boolean { if (ui.paired) return true; openSettings(); return false }
    fun sendQuickPrompt(prompt: String) {
        if (prompt.isBlank()) return
        setDraft(prompt)
        send()
    }
    fun consumeChoiceAndSend(messageId: String, prompt: String) {
        if (prompt.isBlank()) return
        ui = ui.copy(
            consumedChoiceIds = ui.consumedChoiceIds + messageId,
            streamChoices = emptyList()
        )
        sendQuickPrompt(prompt)
    }
    fun retryMessage(msg: PulseMessage) {
        rerollMessage(msg)
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
        val raw = ui.draft.trim().ifEmpty { if (ui.attachments.isNotEmpty()) "请查看这些附件。" else "" }
        if (raw.isEmpty() || ui.uploading) return
        if (!ensurePaired()) return
        val id = ui.botId
        val sentAttachments = ui.attachments
        val withFiles = raw + if (sentAttachments.isEmpty()) "" else "\n\n附件（工作区相对路径，可用 read_file 或 run_bash 读取）：\n" + sentAttachments.joinToString("\n") { it.path.removePrefix("workspace/") }
        val text = ui.reply?.let { "> " + it.text.take(600).replace("\n", "\n> ") + "\n\n" + withFiles } ?: withFiles
        val pending = PulseMessage("pending-${System.currentTimeMillis()}-${optimisticList.size}", id, "user", text, "", "", System.currentTimeMillis() / 1000.0, "", "[]", true)
        optimisticList.add(pending)
        inFlightSends++
        draftJob?.cancel()
        synchronized(draftLock) {
            drafts[id] = ""
            attachmentDrafts[id] = attachments(id).filterNot { a -> sentAttachments.any { it.path == a.path && it.source == a.source } }
        }
        localRevision++
        persistDrafts()
        stoppedGuardBotId = null
        stoppedGuardUntilUptime = 0L
        activeStreamTurnId = ""
        sendGuardBotId = id
        sendGuardUntilUptime = SystemClock.uptimeMillis() + 15_000L
        currentTurnId = "pending-${System.currentTimeMillis()}"
        ui = ui.copy(
            draft = if (ui.botId == id) "" else ui.draft,
            attachments = if (ui.botId == id) attachments(id) else ui.attachments,
            attachmentThumbs = sentAttachments.fold(ui.attachmentThumbs) { acc, a -> acc - a.path },
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
            streamChoices = emptyList(),
            streamSubAgents = emptyList(),
            streamTodos = emptyList(),
            streamStartedAt = System.currentTimeMillis(),
            reply = null,
            messages = combinedMessages()
        )
        perform {
            try {
                val res = request("POST", "/api/chat", JSONObject().put("text", text).put("bot_id", id).put("mode", "auto").put("async_ack", true).put("client_msg_id", pending.id))
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
                    ui = ui.copy(
                        messages = combinedMessages(),
                        workingBot = if (ui.workingBot == id) null else ui.workingBot,
                        streamBotId = if (ui.streamBotId == id) null else ui.streamBotId,
                        streamText = if (ui.streamBotId == id) "" else ui.streamText,
                        streamTool = if (ui.streamBotId == id) "" else ui.streamTool,
                        streamTrace = if (ui.streamBotId == id) emptyList() else ui.streamTrace
                    )
                    refresh(force = true)
                }
            } catch (e: Exception) {
                runCatching { refresh(force = true) }
                val delivered = remoteMessages.takeLast(16).any { it.role == "user" && it.botId == id && it.text.trim() == text.trim() } ||
                    ui.workingBot == id || ui.streamBotId == id
                if (delivered) {
                    optimisticList.removeAll { it.id == pending.id }
                    ui = ui.copy(messages = combinedMessages())
                } else {
                    ui = ui.copy(messages = combinedMessages())
                    throw e
                }
            } finally {
                inFlightSends = (inFlightSends - 1).coerceAtLeast(0)
                if (inFlightSends == 0) ui = ui.copy(busyBot = null)
            }
        }
    }
    fun stop(targetBot: String = ui.botId) {
        val phone = RootPhoneHarness.changes.value
        val hasPhoneToolInTurn = (phone.active && phone.botId == targetBot) ||
            (ui.streamBotId == targetBot && (ui.streamTool == "phone_workspace" ||
                ui.streamTrace.any { it.optString("tool") == "phone_workspace" }))
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
                turnSteps = JSONArray(ui.streamTurnSteps).toString()
            )
            mergeRemoteMessages(listOf(frozenMsg), fullReplace = false)
        }
        if (currentTurnId.isNotBlank()) markTurnCompleted(currentTurnId)
        if (activeStreamTurnId.isNotBlank()) markTurnCompleted(activeStreamTurnId)
        if (isStreamingTarget) {
            activeStreamTurnId = ""
            streamPacerJob?.cancel()
            targetStreamFullText = ""
        }
        stoppedGuardBotId = targetBot
        stoppedGuardUntilUptime = SystemClock.uptimeMillis() + 2_500L
        if (remoteWorkingBot == targetBot) remoteWorkingBot = null
        if (sendGuardBotId == targetBot) {
            sendGuardBotId = null
            sendGuardUntilUptime = 0L
        }
        ui = ui.copy(
            messages = combinedMessages(),
            busyBot = if (ui.busyBot == targetBot) null else ui.busyBot,
            workingBot = if (ui.workingBot == targetBot) null else ui.workingBot,
            streamBotId = if (isStreamingTarget) null else ui.streamBotId,
            streamText = if (isStreamingTarget) "" else ui.streamText,
            streamThought = if (isStreamingTarget) "" else ui.streamThought,
            streamStepText = if (isStreamingTarget) "" else ui.streamStepText,
            streamTool = if (isStreamingTarget) "" else ui.streamTool,
            streamTrace = if (isStreamingTarget) emptyList() else ui.streamTrace,
            streamTurnSteps = if (isStreamingTarget) emptyList() else ui.streamTurnSteps,
            streamChoices = if (isStreamingTarget) emptyList() else ui.streamChoices,
            streamSubAgents = if (isStreamingTarget) emptyList() else ui.streamSubAgents,
            streamTodos = if (isStreamingTarget) emptyList() else ui.streamTodos,
            streamStartedAt = if (isStreamingTarget) 0L else ui.streamStartedAt
        )
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
    fun editMessageAndResend(message: PulseMessage, newText: String) {
        val clean = newText.trim()
        if (clean.isEmpty()) return
        if (!ensurePaired()) return
        val id = message.botId
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
            } finally {
                if (inFlightSends == 0) ui = ui.copy(busyBot = null)
            }
        }
    }
    fun rerollMessage(message: PulseMessage) {
        if (!ensurePaired()) return
        val id = message.botId
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
            streamTool = "",
            streamTrace = emptyList(),
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
            } finally {
                if (inFlightSends == 0) ui = ui.copy(busyBot = null)
            }
        }
    }
    fun switchMessageVariant(message: PulseMessage, variantIdx: Int) {
        if (!ensurePaired()) return
        val vars = message.variants
        if (variantIdx !in vars.indices) return
        val sel = vars[variantIdx]
        val idxInRemote = remoteMessages.indexOfFirst { it.id == message.id }
        if (idxInRemote >= 0) {
            remoteMessages = remoteMessages.toMutableList().also { list ->
                list[idxInRemote] = list[idxInRemote].copy(
                    text = sel.text,
                    thought = sel.thought,
                    trace = sel.trace,
                    turnSteps = sel.turnSteps,
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
        }
    }
    fun reply(message: PulseMessage?) { ui = ui.copy(reply = message) }
    fun copy(text: String) { activity.getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("DSB", text)); toast("已复制。") }
    fun confirm(message: PulseMessage, accepted: Boolean) = perform {
        request("POST", "/api/confirm", JSONObject().put("id", message.id).put("accepted", accepted)); refresh()
    }
    fun ackDemo(message: PulseMessage) = perform { request("POST", "/api/ack", JSONObject().put("ids", JSONArray().put(message.id))); refresh(); toast("演练已结束。") }
    fun close() { setActiveInstance(null); draftJob?.cancel(); persistDrafts(); draftWriter.shutdown(); scope.cancel() }
}

