package dev.bubuki.proactive

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.bubuki.proactive.markdown.MarkdownText
import org.json.JSONObject
import kotlin.math.roundToInt

/**
 * [Module] PulseScreenSubtaskPanel
 * [Responsibility]
 * 1:1 chat-room clone secondary screen (`NativeSubtaskPanel`) for each independent async subtask,
 * reusing `UnifiedTimelineBubbleContent` + Telegram/Apple chat bubbles + bottom live steer input
 * (`POST /api/subtasks/{task_id}/steer`). Zero fake progress bars, zero ops cards, zero top tab clutter.
 */

internal fun NativePulseModel.upsertSubtaskFromJson(obj: JSONObject) {
    val item = PulseSubtask.from(obj)
    if (item.taskId.isBlank()) return
    val merged = (ui.subtasks.filterNot { it.taskId == item.taskId } + item)
        .sortedWith(compareBy<PulseSubtask>({ if (it.status == "running") 0 else 1 }, { -it.updatedAt }))
        .take(32)
    ui = ui.copy(subtasks = merged)
}

internal fun NativePulseModel.removeSubtasksByIds(ids: Set<String>) {
    if (ids.isEmpty()) return
    val kept = ui.subtasks.filterNot { it.taskId in ids }
    val closeActive = ui.activePanel == "subtask" && ui.activeSubtaskId in ids
    ui = ui.copy(
        subtasks = kept,
        activePanel = if (closeActive) null else ui.activePanel,
        activeSubtaskId = if (closeActive) "" else ui.activeSubtaskId
    )
}

internal fun NativePulseModel.refreshSubtasks(botId: String = "") {
    perform {
        val qs = if (botId.isNotBlank()) "?bot_id=$botId&limit=30" else "?limit=30"
        val res = runCatching { request("GET", "/api/subtasks$qs") }.getOrNull() ?: return@perform
        val arr = res.optJSONArray("subtasks") ?: return@perform
        val parsed = (0 until arr.length()).mapNotNull { idx ->
            arr.optJSONObject(idx)?.let(PulseSubtask::from)?.takeIf { it.taskId.isNotBlank() }
        }.sortedWith(compareBy<PulseSubtask>({ if (it.status == "running") 0 else 1 }, { -it.updatedAt })).take(32)
        ui = ui.copy(subtasks = parsed)
    }
}

internal fun NativePulseModel.openSubtaskPanel(taskId: String) {
    val tid = taskId.trim()
    val resolvedId = tid.ifBlank {
        ui.subtasks.firstOrNull { it.parentBotId == ui.botId && it.status == "running" }?.taskId
            ?: ui.subtasks.firstOrNull { it.parentBotId == ui.botId }?.taskId
            ?: ui.subtasks.firstOrNull()?.taskId
            ?: ""
    }
    ui = ui.copy(activeSubtaskId = resolvedId, activePanel = "subtask")
    perform {
        if (resolvedId.isNotBlank()) {
            val res = runCatching { request("GET", "/api/subtasks/$resolvedId") }.getOrNull()
            res?.optJSONObject("subtask")?.let { upsertSubtaskFromJson(it) }
        }
        refreshSubtasks()
    }
}

internal fun NativePulseModel.cancelSubtask(taskId: String) {
    if (ui.subtaskActionBusy || taskId.isBlank()) return
    ui = ui.copy(subtaskActionBusy = true)
    perform {
        try {
            val res = request("POST", "/api/subtasks/$taskId/cancel")
            res.optJSONObject("subtask")?.let { upsertSubtaskFromJson(it) }
            toast(if (res.optBoolean("ok", false)) "已取消子任务" else res.optString("error", "取消失败"))
        } finally {
            ui = ui.copy(subtaskActionBusy = false)
        }
    }
}

internal fun NativePulseModel.resumeSubtask(taskId: String) {
    if (ui.subtaskActionBusy || taskId.isBlank()) return
    ui = ui.copy(subtaskActionBusy = true)
    perform {
        try {
            val res = request("POST", "/api/subtasks/$taskId/resume")
            val nextId = res.optString("task_id", taskId).ifBlank { taskId }
            res.optJSONObject("subtask")?.let { upsertSubtaskFromJson(it) }
            if (res.optBoolean("ok", false)) {
                ui = ui.copy(activeSubtaskId = nextId)
                toast("已继续执行")
            } else {
                toast(res.optString("error", "继续失败"))
            }
        } finally {
            ui = ui.copy(subtaskActionBusy = false)
        }
    }
}

internal fun NativePulseModel.steerSubtask(taskId: String, text: String) {
    val clean = text.trim()
    if (ui.subtaskActionBusy || taskId.isBlank() || clean.isBlank()) return
    ui = ui.copy(subtaskActionBusy = true)
    perform {
        try {
            val res = request("POST", "/api/subtasks/$taskId/steer", JSONObject().put("message", clean))
            res.optJSONObject("subtask")?.let { upsertSubtaskFromJson(it) }
            if (!res.optBoolean("ok", false)) {
                toast(res.optString("error", "发送失败"))
            }
        } finally {
            ui = ui.copy(subtaskActionBusy = false)
        }
    }
}

internal fun NativePulseModel.rollbackMemoryBackup(backupId: String) {
    if (ui.subtaskActionBusy) return
    ui = ui.copy(subtaskActionBusy = true)
    perform {
        try {
            val res = request("POST", "/api/memory/rollback", JSONObject().put("backup_id", backupId))
            if (res.optBoolean("ok", false)) {
                toast("已回滚至快照 ${res.optString("restored_backup", backupId)}")
            } else {
                toast("回滚失败：${res.optString("error", "未知错误").take(60)}")
            }
        } finally {
            ui = ui.copy(subtaskActionBusy = false)
        }
    }
}

internal fun NativePulseModel.triggerMemoryReorganize() {
    if (ui.reorganizingMemory) return
    ui = ui.copy(reorganizingMemory = true, memoryReorgNotice = "正在启动记忆深度整理…")
    perform {
        try {
            val res = request(
                "POST",
                "/api/memory/reorganize",
                JSONObject().put("dry_run", false).put("use_llm", true).put("bot_id", ui.botId)
            )
            val ok = res.optBoolean("ok", false)
            val tid = res.optString("task_id", "")
            res.optJSONObject("subtask")?.let { upsertSubtaskFromJson(it) }
            if (ok && tid.isNotBlank()) {
                ui = ui.copy(
                    reorganizingMemory = false,
                    memoryReorgNotice = "已启动记忆深度整理",
                    activeSubtaskId = tid,
                    activePanel = "subtask"
                )
            } else {
                val err = res.optString("error", "启动失败").take(80)
                ui = ui.copy(reorganizingMemory = false, memoryReorgNotice = "启动失败：$err")
                toast("启动失败：$err")
            }
        } catch (t: Throwable) {
            ui = ui.copy(reorganizingMemory = false, memoryReorgNotice = "启动异常：${t.message?.take(80) ?: "网络错误"}")
        }
    }
}

private fun formatSubtaskElapsed(sec: Double): String {
    val total = sec.roundToInt().coerceAtLeast(0)
    return if (total < 60) "${total}s" else "${total / 60}m${total % 60}s"
}

private fun stepToTraceJson(st: PulseSubtaskStep): JSONObject {
    val argsObj = runCatching {
        val raw = st.args.trim()
        if (raw.startsWith("{")) JSONObject(raw) else JSONObject().put("detail", raw)
    }.getOrElse { JSONObject().put("detail", st.args) }
    val resObj = JSONObject()
    if (st.result.isNotBlank()) resObj.put("summary", st.result)
    if (st.error.isNotBlank()) resObj.put("error", st.error)
    resObj.put("ok", st.ok)
    return JSONObject()
        .put("tool", st.tool.ifBlank { "subtask" })
        .put("args", argsObj)
        .put("result", resObj)
        .put("status", if (st.ok) "ok" else "error")
}

@Composable
internal fun NativeSubtaskPanel(model: NativePulseModel, modifier: Modifier) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val activeTask = remember(s.subtasks, s.activeSubtaskId) {
        s.subtasks.firstOrNull { it.taskId == s.activeSubtaskId }
            ?: s.subtasks.firstOrNull { it.parentBotId == s.botId }
            ?: s.subtasks.firstOrNull()
    }
    var steerDraft by remember(activeTask?.taskId) { mutableStateOf("") }
    var confirmRollback by remember { mutableStateOf(false) }
    val listState = rememberLazyListState()

    val parentBot = remember(activeTask?.parentBotId, s.bots) {
        activeTask?.let { model.bot(it.parentBotId) }
    }
    val subtitle = remember(activeTask) {
        if (activeTask == null) ""
        else {
            val stLabel = when (activeTask.status) {
                "running" -> if (activeTask.stageTotal > 0) {
                    val sub = if (activeTask.subTotal > 1) " (${activeTask.subDone}/${activeTask.subTotal})" else ""
                    "进行中 · 阶段 ${activeTask.stageIdx}/${activeTask.stageTotal}$sub"
                } else if (activeTask.stepsExecuted > 0) "进行中 · 第 ${activeTask.stepsExecuted} 步" else "进行中"
                "completed" -> "已完成"
                "failed" -> "已出错"
                "cancelled" -> "已取消"
                else -> activeTask.status
            }
            val elapsed = formatSubtaskElapsed(activeTask.elapsedSec)
            val botName = parentBot?.name ?: activeTask.parentBotId
            "$stLabel · $elapsed · 归属 $botName"
        }
    }

    val traceList = remember(activeTask?.recentSteps) {
        activeTask?.recentSteps?.map(::stepToTraceJson).orEmpty()
    }
    val chatMessages = remember(activeTask?.messages, activeTask?.goal, activeTask?.title) {
        val raw = activeTask?.messages.orEmpty()
        if (raw.isNotEmpty()) raw
        else listOfNotNull(activeTask?.goal?.ifBlank { activeTask.title }?.let { PulseSubtaskMessage("user", it) })
    }
    val isLive = activeTask?.status == "running"
    val isFailed = activeTask?.status == "failed"
    val canSteer = activeTask != null && activeTask.preset != "memory_reorganize"

    LaunchedEffect(activeTask?.recentSteps?.size, chatMessages.size, activeTask?.resultSummary) {
        val count = listState.layoutInfo.totalItemsCount
        if (count > 0) listState.animateScrollToItem(count - 1)
    }

    Column(modifier.background(c.bg).imePadding()) {
        // 1:1 NativeChat Top Header (Back + Task Name + Real Subtitle + Single Action)
        Column(Modifier.fillMaxWidth().background(c.surface)) {
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 6.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Row(
                    Modifier.heightIn(min = 40.dp)
                        .clip(RoundedCornerShape(12.dp))
                        .iosPressScale(0.95f) { model.closePanel() }
                        .padding(horizontal = 6.dp, vertical = 7.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    PulseIcon("back", c.accent, Modifier.size(22.dp))
                    Text("对话", color = c.accent, fontSize = 16.sp, modifier = Modifier.padding(start = 1.dp))
                }
                Row(
                    Modifier.weight(1f).padding(horizontal = 6.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    if (parentBot != null) {
                        PulseAvatar(parentBot, s, diameter = 34.dp, showStatusDot = false, ringColor = c.surface)
                        Spacer(Modifier.width(9.dp))
                    }
                    Column(Modifier.weight(1f)) {
                        Text(
                            activeTask?.title ?: "子任务",
                            color = c.ink,
                            fontSize = 16.sp,
                            fontWeight = FontWeight.SemiBold,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis
                        )
                        if (subtitle.isNotBlank()) {
                            Text(
                                subtitle,
                                color = if (isFailed) c.warn else c.muted,
                                fontSize = 12.sp,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis
                            )
                        }
                    }
                }
                if (activeTask != null) {
                    when {
                        isLive -> TextButton(
                            onClick = { model.cancelSubtask(activeTask.taskId) },
                            enabled = !s.subtaskActionBusy
                        ) { Text("取消", color = c.warn, fontSize = 14.sp, fontWeight = FontWeight.Medium) }
                        activeTask.status == "failed" || activeTask.status == "cancelled" -> TextButton(
                            onClick = { model.resumeSubtask(activeTask.taskId) },
                            enabled = !s.subtaskActionBusy
                        ) { Text("继续", color = c.accent, fontSize = 14.sp, fontWeight = FontWeight.SemiBold) }
                        activeTask.status == "completed" && activeTask.backupId.isNotBlank() -> TextButton(
                            onClick = { confirmRollback = true },
                            enabled = !s.subtaskActionBusy
                        ) { Text("回滚", color = c.muted, fontSize = 14.sp, fontWeight = FontWeight.Medium) }
                    }
                }
            }
            HorizontalDivider(color = c.line.copy(alpha = 0.6f), thickness = 0.5.dp)
        }

        if (activeTask == null) {
            Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                Text("暂无子任务记录", color = c.muted, fontSize = 14.sp)
            }
            return@Column
        }

        // 1:1 NativeChat Message Stream (User Goal / Steer Bubbles + Native Assistant Timeline Bubble)
        val userBubbleShape = RoundedCornerShape(18.dp, 18.dp, 4.dp, 18.dp)
        val aiBubbleShape = RoundedCornerShape(18.dp, 18.dp, 18.dp, 4.dp)
        val latestAssistantIdx = chatMessages.indexOfLast { it.role == "assistant" }

        LazyColumn(
            state = listState,
            modifier = Modifier.weight(1f).fillMaxWidth(),
            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            chatMessages.forEachIndexed { idx, msg ->
                if (msg.role == "user") {
                    item(key = "u_${idx}_${msg.ts}") {
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                            Box(
                                Modifier.widthIn(max = 310.dp)
                                    .clip(userBubbleShape)
                                    .background(c.userBubble)
                                    .padding(horizontal = 13.dp, vertical = 9.dp)
                            ) {
                                Text(msg.text, color = Color.White, fontSize = 15.5.sp, lineHeight = 22.sp)
                            }
                        }
                    }
                } else if (idx != latestAssistantIdx || isLive) {
                    // Historical assistant turn before a follow-up steer
                    item(key = "a_hist_${idx}_${msg.ts}") {
                        Box(
                            Modifier.fillMaxWidth(0.92f)
                                .clip(aiBubbleShape)
                                .background(c.aiBubble)
                                .border(0.5.dp, c.line.copy(alpha = 0.72f), aiBubbleShape)
                                .padding(horizontal = 14.dp, vertical = 10.dp)
                        ) {
                            MarkdownText(
                                remember(msg.text) { nativeWikiMarkdown(msg.text) },
                                TextStyle(color = c.ink, fontSize = 15.5.sp, lineHeight = 22.sp),
                                onOpenUri = model::openLink
                            )
                        }
                    }
                }
            }

            // Live / Final Assistant Bubble using EXACT same UnifiedTimelineBubbleContent as NativeChat
            item(key = "assistant_main_${activeTask.taskId}") {
                val mainBody = buildString {
                    val body = activeTask.resultSummary.ifBlank { activeTask.lastOutput }.trim()
                    if (body.isNotBlank()) append(body)
                    if (activeTask.lastError.isNotBlank() && body.isBlank()) {
                        append("执行报错：${activeTask.lastError}")
                    } else if (activeTask.lastError.isNotBlank() && isFailed) {
                        append("\n\n**报错**：${activeTask.lastError}")
                    }
                    if (activeTask.status == "completed" && activeTask.reportPath.isNotBlank()) {
                        append("\n\n完整报告：`memory_vault/${activeTask.reportPath}`")
                    }
                }
                val liveThoughtText = activeTask.liveThought.ifBlank {
                    if (isLive) activeTask.stageLabel.ifBlank { "正在执行子任务…" } else ""
                }
                Column(
                    Modifier.fillMaxWidth(0.94f)
                        .clip(aiBubbleShape)
                        .background(c.aiBubble)
                        .border(
                            0.5.dp,
                            if (isFailed) c.warn.copy(alpha = 0.55f) else c.line.copy(alpha = 0.72f),
                            aiBubbleShape
                        )
                        .padding(horizontal = 14.dp, vertical = 10.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    UnifiedTimelineBubbleContent(
                        model = model,
                        cardKey = "subtask_${activeTask.taskId}",
                        timeline = emptyList(),
                        fallbackTrace = traceList,
                        fallbackText = mainBody,
                        thought = liveThoughtText,
                        isLive = isLive,
                        isError = isFailed,
                        streamStartedAt = System.currentTimeMillis() - (activeTask.elapsedSec * 1000.0).toLong(),
                        turnId = activeTask.taskId
                    )
                }
            }
        }

        // Bottom Live Steer Composer (identical Telegram/Apple feel to NativeChat)
        if (canSteer) {
            HorizontalDivider(color = c.line.copy(alpha = 0.5f), thickness = 0.5.dp)
            Row(
                Modifier.fillMaxWidth()
                    .background(c.surface)
                    .padding(horizontal = 10.dp, vertical = 8.dp),
                verticalAlignment = Alignment.Bottom,
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                Box(
                    Modifier.weight(1f)
                        .clip(RoundedCornerShape(20.dp))
                        .background(c.elevated)
                        .border(0.5.dp, c.line.copy(alpha = 0.65f), RoundedCornerShape(20.dp))
                        .padding(horizontal = 14.dp, vertical = 10.dp)
                ) {
                    if (steerDraft.isEmpty()) {
                        Text(
                            if (isLive) "给这个子任务插话或纠偏…" else "继续补充要求让它接着查…",
                            color = c.faint,
                            fontSize = 15.sp
                        )
                    }
                    BasicTextField(
                        value = steerDraft,
                        onValueChange = { steerDraft = it },
                        textStyle = TextStyle(color = c.ink, fontSize = 15.sp, lineHeight = 20.sp),
                        cursorBrush = SolidColor(c.accent),
                        maxLines = 4,
                        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                        keyboardActions = KeyboardActions(onSend = {
                            val txt = steerDraft.trim()
                            if (txt.isNotEmpty() && !s.subtaskActionBusy) {
                                steerDraft = ""
                                model.steerSubtask(activeTask.taskId, txt)
                            }
                        }),
                        modifier = Modifier.fillMaxWidth()
                    )
                }
                val canSend = steerDraft.isNotBlank() && !s.subtaskActionBusy
                Box(
                    Modifier.size(38.dp)
                        .clip(CircleShape)
                        .background(if (canSend) c.accent else c.line.copy(alpha = 0.5f))
                        .clickable(enabled = canSend) {
                            val txt = steerDraft.trim()
                            steerDraft = ""
                            model.steerSubtask(activeTask.taskId, txt)
                        },
                    contentAlignment = Alignment.Center
                ) {
                    PulseIcon("send", Color.White, Modifier.size(18.dp))
                }
            }
        }
    }

    if (confirmRollback && activeTask != null && activeTask.backupId.isNotBlank()) {
        AlertDialog(
            onDismissRequest = { confirmRollback = false },
            containerColor = c.surface,
            title = { Text("回滚记忆整理", color = c.ink, fontWeight = FontWeight.SemiBold) },
            text = { Text("确定要还原到整理前快照 ${activeTask.backupId} 吗？系统会在回滚前自动保留一份当前安全副本。", color = c.muted, fontSize = 13.5.sp) },
            confirmButton = {
                TextButton(onClick = {
                    confirmRollback = false
                    model.rollbackMemoryBackup(activeTask.backupId)
                }) { Text("确认回滚", color = c.warn, fontWeight = FontWeight.SemiBold) }
            },
            dismissButton = {
                TextButton(onClick = { confirmRollback = false }) { Text("取消", color = c.muted) }
            }
        )
    }
}
