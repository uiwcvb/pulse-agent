package dev.bubuki.proactive

import android.content.Intent
import kotlinx.coroutines.flow.update
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/**
 * [Module] RootPhoneStore
 * [Responsibility] Phone workspace message formatting, in-memory history deduplication,
 *                  encrypted `phoneHistory` persistence (migrated off main `SecurePrefs`),
 *                  and update broadcasts.
 */

internal fun pulseMessageToJson(message: PulseMessage): JSONObject = JSONObject()
    .put("id", message.id)
    .put("bot_id", message.botId)
    .put("role", message.role)
    .put("text", message.text)
    .put("kind", message.kind)
    .put("engine", message.engine)
    .put("created_at", message.created)
    .put("decision", message.decision)
    .put("trace", runCatching { JSONArray(message.trace) }.getOrElse { JSONArray() })

internal fun RootPhoneHarness.message(
    bot: String,
    role: String,
    text: String,
    kind: String,
    trace: String = "[]"
): PulseMessage = PulseMessage(
    "phone-" + UUID.randomUUID(),
    bot,
    role,
    text,
    kind,
    "root-phone",
    System.currentTimeMillis() / 1000.0,
    "",
    trace
)

internal fun RootPhoneHarness.append(message: PulseMessage) {
    state.update { cur ->
        val cleanTxt = message.text.trim()
        val lastSameBot = cur.messages.lastOrNull { it.botId == message.botId && it.role == message.role }
        if (lastSameBot != null && lastSameBot.text.trim() == cleanTxt && kotlin.math.abs(message.created - lastSameBot.created) < 30.0) {
            cur
        } else {
            cur.copy(messages = (cur.messages + message).takeLast(600))
        }
    }
    broadcast()
}

internal fun RootPhoneHarness.persist() {
    val base = context ?: return
    val snapshot = state.value
    val packet = JSONObject()
        .put("phoneHistory", JSONArray(snapshot.messages.map(::pulseMessageToJson)))
        .put(
            "phoneTask",
            JSONObject()
                .put("phase", snapshot.phase)
                .put("botId", snapshot.botId)
                .put("goal", snapshot.goal)
        )
    SecurePrefs.savePhoneHistory(base, packet)
}

internal fun RootPhoneHarness.broadcast() {
    context?.let { it.sendBroadcast(Intent(it.packageName + ".UPDATE").setPackage(it.packageName)) }
}
