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


internal data class PulseBot(
    val id: String, val name: String, val description: String, val color: String, val shape: String,
    val engine: String, val pinned: Boolean, val hidden: Boolean, val pinnedAt: Double, val avatarVersion: String,
    val instructions: String = "", val model: String = "", val curiosityEnabled: Boolean = false, val notify: Boolean = true
) {
    fun json() = JSONObject().put("id", id).put("name", name).put("description", description).put("color", color).put("shape", shape)
        .put("engine", engine).put("pinned", pinned).put("hidden", hidden).put("pinned_at", pinnedAt).put("avatar_version", avatarVersion)
        .put("instructions", instructions).put("model", model).put("curiosity_enabled", curiosityEnabled).put("notify", notify)
    companion object {
        fun from(j: JSONObject, source: String = "phone"): PulseBot {
            val normSrc = source.trim().lowercase().ifBlank { "phone" }
            val nodeCfg = j.optJSONObject("node_settings")?.optJSONObject(normSrc)
            val id = j.getString("id")
            return PulseBot(
                id, j.optString("name"), j.optString("description"),
                j.optString("color", "#B568F7"), j.optString("shape", if (id == "companion") "flower" else "circle"), j.optString("engine", "small"),
                nodeCfg?.optBoolean("pinned", j.optBoolean("pinned")) ?: j.optBoolean("pinned"),
                nodeCfg?.optBoolean("hidden", j.optBoolean("hidden")) ?: j.optBoolean("hidden"),
                nodeCfg?.optDouble("pinned_at", j.optDouble("pinned_at", 0.0)) ?: j.optDouble("pinned_at", 0.0),
                j.optString("avatar_version"),
                j.optString("instructions", ""),
                nodeCfg?.optString("model", j.optString("model", "")) ?: j.optString("model", ""),
                nodeCfg?.optBoolean("curiosity_enabled", j.optBoolean("curiosity_enabled", id == "companion")) ?: j.optBoolean("curiosity_enabled", id == "companion"),
                nodeCfg?.optBoolean("notify", j.optBoolean("notify", true)) ?: j.optBoolean("notify", true)
            )
        }
    }
}

internal data class PulseAttachment(val name: String, val path: String, val source: String, val bytes: Long = 0, val sha256: String = "") {
    fun json() = JSONObject().put("name", name).put("path", path).put("source", source).put("bytes", bytes).put("sha256", sha256)
    companion object { fun from(j: JSONObject) = PulseAttachment(j.optString("name"), j.getString("path"), j.optString("source", "workspace"), j.optLong("bytes"), j.optString("sha256")) }
}

internal data class PulsePendingUpload(
    val id: String,
    val name: String,
    val bytes: Long,
    val isImage: Boolean,
    val progress: Float = 0f,
    val error: String = "",
    val uri: android.net.Uri? = null,
    val thumb: ImageBitmap? = null
)

internal data class PulseChoice(val label: String, val value: String) {
    fun json(): JSONObject = JSONObject().put("label", label).put("value", value)
    companion object {
        fun from(j: JSONObject) = PulseChoice(
            j.optString("label", j.optString("value", "")).trim(),
            j.optString("value", j.optString("label", "")).trim()
        )
        fun parseArray(rawChoices: JSONArray?): List<PulseChoice> {
            if (rawChoices == null) return emptyList()
            return (0 until rawChoices.length()).mapNotNull { idx ->
                val item = rawChoices.optJSONObject(idx)
                if (item != null) from(item).takeIf { it.label.isNotBlank() }
                else rawChoices.optString(idx).trim().takeIf { it.isNotBlank() }?.let { PulseChoice(it, it) }
            }
        }
    }
}

internal data class PulseMessageAttachment(
    val path: String,
    val name: String,
    val size: Long = 0L,
    val ext: String = "",
    val mime: String = ""
) {
    companion object {
        fun parseArray(arr: JSONArray?): List<PulseMessageAttachment> {
            if (arr == null || arr.length() == 0) return emptyList()
            val out = mutableListOf<PulseMessageAttachment>()
            for (i in 0 until arr.length()) {
                val obj = arr.optJSONObject(i) ?: continue
                val p = obj.optString("path", "").trim()
                if (p.isBlank()) continue
                val rawName = obj.optString("name", "").trim().ifBlank { p.substringAfterLast('/') }
                val ext = obj.optString("ext", "").trim().ifBlank { rawName.substringAfterLast('.', "").uppercase(Locale.ROOT) }
                out.add(
                    PulseMessageAttachment(
                        path = p,
                        name = rawName,
                        size = obj.optLong("size", 0L),
                        ext = ext,
                        mime = obj.optString("mime", "").trim()
                    )
                )
            }
            return out
        }
    }
}

internal data class PulseMessageVariant(
    val id: String = "",
    val text: String = "",
    val thought: String = "",
    val trace: String = "[]",
    val turnSteps: String = "[]",
    val timeline: String = "[]",
    val sticker: String = "",
    val attachments: List<PulseMessageAttachment> = emptyList(),
    val choices: List<PulseChoice> = emptyList(),
    val apiCalls: Int = 0,
    val ttftMs: Int = 0,
    val tps: Double = 0.0,
    val cacheHitPct: Int = 0,
    val totalMs: Int = 0,
) {
    companion object {
        fun parseArray(arr: JSONArray?): List<PulseMessageVariant> {
            if (arr == null || arr.length() == 0) return emptyList()
            val out = mutableListOf<PulseMessageVariant>()
            for (i in 0 until arr.length()) {
                val obj = arr.optJSONObject(i) ?: continue
                val m = obj.optJSONObject("metrics")
                val rawTps = obj.optDouble("tps", m?.optDouble("tps", 0.0) ?: 0.0)
                out.add(
                    PulseMessageVariant(
                        id = obj.optString("id", ""),
                        text = obj.optString("text", ""),
                        thought = obj.optString("thought", ""),
                        trace = (obj.optJSONArray("trace") ?: JSONArray()).toString(),
                        turnSteps = (obj.optJSONArray("turn_steps") ?: JSONArray()).toString(),
                        timeline = (obj.optJSONArray("timeline") ?: JSONArray()).toString(),
                        sticker = obj.optString("sticker", ""),
                        attachments = PulseMessageAttachment.parseArray(obj.optJSONArray("attachments")),
                        choices = PulseChoice.parseArray(obj.optJSONArray("choices")),
                        apiCalls = obj.optInt("api_calls", m?.optInt("api_calls", 0) ?: 0),
                        ttftMs = obj.optInt("ttft_ms", m?.optInt("ttft_ms", 0) ?: 0),
                        tps = if (rawTps.isNaN()) 0.0 else rawTps,
                        cacheHitPct = obj.optInt("cache_hit_pct", m?.optInt("cache_hit_pct", 0) ?: 0),
                        totalMs = m?.optInt("total_ms", 0) ?: 0,
                    )
                )
            }
            return out
        }
    }
}

internal data class PulseMessage(
    val id: String, val botId: String, val role: String, val text: String, val kind: String, val engine: String,
    val created: Double, val decision: String, val trace: String, val optimistic: Boolean = false,
    val fromBotId: String = "", val fromBotName: String = "",
    val choices: List<PulseChoice> = emptyList(),
    val isSubTurn: Boolean = false,
    val thought: String = "",
    val sticker: String = "",
    val apiCalls: Int = 0,
    val ttftMs: Int = 0,
    val tps: Double = 0.0,
    val promptTokens: Int = 0,
    val cachedTokens: Int = 0,
    val cacheHitPct: Int = 0,
    val completionTokens: Int = 0,
    val totalMs: Int = 0,
    val toBotId: String = "",
    val toBotName: String = "",
    val direction: String = "",
    val seq: Long = 0L,
    val orderSeq: Long = 0L,
    val isBgResult: Boolean = false,
    val nonTerminal: Boolean = false,
    val turnId: String = "",
    val clientMsgId: String = "",
    val turnSteps: String = "[]",
    val timeline: String = "[]",
    val attachments: List<PulseMessageAttachment> = emptyList(),
    val variants: List<PulseMessageVariant> = emptyList(),
    val activeVariantIdx: Int = 0
) {
    val isTerminalAssistantReply: Boolean
        get() = role == "assistant" && !isSubTurn && !isBgResult && !nonTerminal && kind != "bot_message"

    companion object {
        fun from(j: JSONObject): PulseMessage {
            val parsedChoices = PulseChoice.parseArray(j.optJSONArray("choices"))
            val parsedAtts = PulseMessageAttachment.parseArray(j.optJSONArray("attachments"))
            val parsedVariants = PulseMessageVariant.parseArray(j.optJSONArray("variants"))
            val activeVarIdx = if (j.has("active_variant_idx") && !j.isNull("active_variant_idx")) {
                j.optInt("active_variant_idx", (parsedVariants.size - 1).coerceAtLeast(0))
            } else {
                (parsedVariants.size - 1).coerceAtLeast(0)
            }
            val m = j.optJSONObject("metrics")
            val apiCalls = j.optInt("api_calls", m?.optInt("api_calls", 0) ?: 0)
            val seqVal = j.optLong("seq", 0L)
            return PulseMessage(
                j.getString("id"), j.optString("bot_id", "companion"), j.optString("role"),
                j.optString("text"), j.optString("kind"), j.optString("engine"), j.optDouble("created_at"),
                j.optString("decision"), (j.optJSONArray("trace") ?: JSONArray()).toString(), false,
                j.optString("from_bot_id", ""), j.optString("from_bot_name", ""),
                parsedChoices,
                j.optBoolean("is_sub_turn", false) || j.optString("kind") == "bot_message",
                j.optString("thought", ""),
                j.optString("sticker", ""),
                apiCalls,
                j.optInt("ttft_ms", m?.optInt("ttft_ms", 0) ?: 0),
                j.optDouble("tps", m?.optDouble("tps", 0.0) ?: 0.0).let { if (it.isNaN()) 0.0 else it },
                m?.optInt("prompt_tokens", 0) ?: 0,
                m?.optInt("cached_tokens", 0) ?: 0,
                j.optInt("cache_hit_pct", m?.optInt("cache_hit_pct", 0) ?: 0),
                m?.optInt("completion_tokens", 0) ?: 0,
                m?.optInt("total_ms", 0) ?: 0,
                j.optString("to_bot_id", ""),
                j.optString("to_bot_name", ""),
                j.optString("direction", ""),
                seqVal,
                j.optLong("order_seq", seqVal),
                j.optBoolean("is_bg_result", false),
                j.optBoolean("non_terminal", false),
                j.optString("turn_id", ""),
                j.optString("client_msg_id", ""),
                (j.optJSONArray("turn_steps") ?: JSONArray()).toString(),
                (j.optJSONArray("timeline") ?: JSONArray()).toString(),
                parsedAtts,
                parsedVariants,
                activeVarIdx
            )
        }
    }
}

internal data class PulseMenuItem(val title: String, val description: String = "", val icon: String = "next", val run: () -> Unit)
internal data class PulseMenu(val title: String, val items: List<PulseMenuItem>)

internal data class NativePulseState(
    val bots: List<PulseBot> = emptyList(), val messages: List<PulseMessage> = emptyList(), val unread: Set<String> = emptySet(),
    val online: Boolean = false, val paired: Boolean = true, val latency: Long = 0, val primary: String = "companion",
    val botId: String = "companion", val chatOpen: Boolean = false, val busyBot: String? = null,
    val workingBot: String? = null, val computerBot: String? = null, val workingText: String = "", val mode: String = "auto", val draft: String = "",
    val reply: PulseMessage? = null, val attachments: List<PulseAttachment> = emptyList(), val pendingUploads: List<PulsePendingUpload> = emptyList(), val attachmentThumbs: Map<String, ImageBitmap> = emptyMap(), val uploading: Boolean = false,
    val searching: Boolean = false, val query: String = "", val snoozed: Boolean = false,
    val smallReady: Boolean = false, val bigReady: Boolean = false, val smallModel: String = "", val bigModel: String = "",
    val defaultModel: String = "",
    val dark: Boolean = true, val motion: Boolean = true, val haptics: Boolean = true, val powerSave: Boolean = false, val routineCount: Int = 0,
    val avatars: Map<String, ImageBitmap> = emptyMap(), val menu: PulseMenu? = null, val toast: String = "", val toastId: Long = 0,
    val activePanel: String? = null,
    val serverUrl: String = SecureTransport.DEFAULT_SERVER,
    val deviceTokenInput: String = "",
    val apiBaseUrl: String = "https://api.openai.com/v1",
    val apiKeyInput: String = "",
    val hasApiKey: Boolean = false,
    val fallbackEnabled: Boolean = false,
    val fallbackBaseUrl: String = "",
    val fallbackApiKeyInput: String = "",
    val fallbackModel: String = "",
    val hasFallbackApiKey: Boolean = false,
    val availableModels: List<String> = emptyList(),
    val fetchingModels: Boolean = false,
    val savingSettings: Boolean = false,
    val curiosityEnabledGlobal: Boolean = true,
    val curiosityMinutes: String = "90",
    val autoCompactTokens: String = "100000",
    val contextTokensByBot: Map<String, Int> = emptyMap(),
    val contextCachedByBot: Map<String, Int> = emptyMap(),
    val contextMsgsByBot: Map<String, Int> = emptyMap(),
    val contextTtftByBot: Map<String, Int> = emptyMap(),
    val contextTpsByBot: Map<String, Double> = emptyMap(),
    val contextCompTokensByBot: Map<String, Int> = emptyMap(),
    val compactingContext: Boolean = false,
    val backgroundService: Boolean = true,
    val appearance: String = "dark",
    val latestReleaseVersion: String = "",
    val latestReleaseNotes: String = "",
    val updateAvailable: Boolean = false, val updateChecked: Boolean = false, val expectedApkSize: Long = 0L,
    val checkingUpdate: Boolean = false,
    val downloadingApk: Boolean = false,
    val downloadProgress: Int = 0,
    val editingBotId: String? = null,
    val editBotName: String = "",
    val editBotDesc: String = "",
    val editBotModel: String = "",
    val editBotInstructions: String = "",
    val editBotColor: String = "#B568F7",
    val editBotShape: String = "flower",
    val editBotCuriosity: Boolean = false,
    val editBotNotify: Boolean = true,
    val savingBot: Boolean = false,
    val vaultFiles: List<String> = emptyList(),
    val selectedVaultPath: String = "INDEX.md",
    val vaultContent: String = "",
    val vaultLoading: Boolean = false,
    val vaultSaving: Boolean = false,
    val streamBotId: String? = null,
    val streamSpeakerId: String? = null,
    val streamSpeakerName: String = "",
    val streamText: String = "",
    val streamThought: String = "",
    val streamStepText: String = "",
    val streamTool: String = "",
    val streamTrace: List<JSONObject> = emptyList(),
    val streamTurnSteps: List<JSONObject> = emptyList(),
    val streamTimeline: List<JSONObject> = emptyList(),
    val streamChoices: List<PulseChoice> = emptyList(),
    val consumedChoiceIds: Set<String> = emptySet(),
    val streamStartedAt: Long = 0L,
    val activeTypewriterMsgId: String? = null,
    val activeTypewriterChars: Int = Int.MAX_VALUE,
    val cloudDesktopFrame: ImageBitmap? = null,
    val cloudDesktopHash: String = "",
    val cloudDesktopUrl: String = "",
    val cloudDesktopTitle: String = "",
    val cloudDesktopLoading: Boolean = false,
    val cloudDesktopTapNorm: Pair<Float, Float>? = null,
    val cloudDesktopLiveBotId: String? = null,
    val cloudDesktopLiveOp: String = "",
    val cloudDesktopPipVisible: Boolean = false,
    val cloudDesktopUserControl: Boolean = false, val cloudDesktopActiveWindow: String = "", val cloudDesktopTaskRunning: Boolean = false, val cloudDesktopQuota: String = "",
    val schedulesData: JSONObject? = null,
    val schedulesLoading: Boolean = false,
    val streamSubAgents: List<JSONObject> = emptyList(),
    val streamTodos: List<JSONObject> = emptyList(),
    val pinnedMemories: List<JSONObject> = emptyList(),
    val watchdogs: List<JSONObject> = emptyList(),
    val stickerPacks: List<JSONObject> = emptyList(),
    val editingMessage: PulseMessage? = null,
    val lastSwitchDir: Int = 1,
    val onboardingStep: Int = 1,
    val dshPeers: List<JSONObject> = emptyList(),
    val testingConnection: Boolean = false,
    val connectionTestOk: Boolean? = null,
    val connectionTestMsg: String = "",
    val syncLoadedOnce: Boolean = false,
    val onboardingCompleted: Boolean = false,
    val cloudDesktopConfigured: Boolean = false,
    val quickGateDismissed: Boolean = false,
    val promptPreview: JSONObject? = null,
    val promptPreviewLoading: Boolean = false,
    val healthDialogOpen: Boolean = false
)


internal fun JSONArray.objects(): List<JSONObject> = (0 until length()).map { getJSONObject(it) }

