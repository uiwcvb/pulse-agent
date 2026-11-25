package dev.bubuki.proactive

import android.os.Build
import org.json.JSONObject

internal const val DESTROYED_DISPLAY_JSON: String =
    "{\"created\":false,\"display_active\":false,\"allocated\":false,\"released\":true,\"destroyed\":true,\"explicit_destroy\":true,\"displayId\":-1,\"display_id\":-1}"

internal data class PhoneApp(val name: String, val component: String)

internal data class RootPhoneState(
    val root: Boolean = false,
    val privileged: Boolean = false,
    val privilegeMode: String = "none",
    val uid: Int = -1,
    val sdk: Int = Build.VERSION.SDK_INT,
    val display: String = "{}",
    val image: String = "",
    val phase: String = "idle",
    val botId: String? = null,
    val goal: String = "",
    val result: String = "",
    val error: String = "",
    val trace: String = "[]",
    val messages: List<PulseMessage> = emptyList(),
    val apps: List<PhoneApp> = emptyList(),
    val model: String = "",
    val modelReady: Boolean = false,
    val imageInfo: String = "{}",
    val notifications: Boolean = false,
    val overlay: Boolean = false,
    val listener: Boolean = false,
    val background: Boolean = false,
    val lastTouchX: Int = -1,
    val lastTouchY: Int = -1,
    val lastTouchTime: Long = 0L,
    val lastTreeSummary: String = ""
) {
    val active: Boolean get() = phase in listOf("running", "waiting_user")
    val displayCreated: Boolean get() = runCatching { JSONObject(display).optBoolean("created") }.getOrDefault(false)
    val displayId: Int get() = runCatching { JSONObject(display).let { it.optInt("displayId", it.optInt("display_id", -1)) } }.getOrDefault(-1)
    val currentPackage: String?
        get() = PulseAccessibilityService.getCachedSnapshot(displayId)?.activePackage?.takeIf { it.isNotBlank() }
            ?: runCatching { JSONObject(display).optString("package", "").takeIf { it.isNotBlank() } }.getOrNull()
}
