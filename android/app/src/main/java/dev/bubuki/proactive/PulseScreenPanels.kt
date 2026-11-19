package dev.bubuki.proactive

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.gestures.*
import androidx.compose.foundation.interaction.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.*
import androidx.compose.foundation.shape.*
import androidx.compose.foundation.text.*
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.*
import androidx.compose.ui.draw.*
import androidx.compose.ui.focus.*
import androidx.compose.ui.graphics.*
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.*
import androidx.compose.ui.platform.*
import androidx.compose.ui.semantics.*
import androidx.compose.ui.text.*
import androidx.compose.ui.text.font.*
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.style.*
import androidx.compose.ui.unit.*
import androidx.compose.ui.window.*
import dev.bubuki.proactive.markdown.MarkdownText
import kotlinx.coroutines.launch
import org.json.JSONArray
import kotlin.math.abs
import kotlin.math.roundToInt

/**
 * [Module] PulseScreenPanels
 * [Responsibility]
 * 二級業務面板與實時流卡片領域模塊：負責透明記憶庫瀏覽與 Markdown 編輯面板 (NativeVaultPanel)、
 * 定時任務/倒計時/哨兵面板 (NativeSchedulesPanel)，以及聊天室實時流式輸出與工具執行狀態氣泡 (NativeLiveStreamBubble)。
 * 雲端桌面控制面板已獨立至 [PulseScreenCloudPanel]。
 *
 * [Public API]
 * - NativeVaultPanel(model: NativePulseModel, modifier: Modifier)
 * - NativeSchedulesPanel(model: NativePulseModel, modifier: Modifier)
 * - NativeLiveStreamBubble(model: NativePulseModel, roomBot: PulseBot)
 */

@Composable
internal fun NativeLiveStreamBubble(model: NativePulseModel, roomBot: PulseBot) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val rawStream = s.streamText
    val isPlaceholder = rawStream.isBlank() ||
        rawStream == "正在思考…" ||
        rawStream == "正在思考与执行任务…" ||
        rawStream == "正在整理回复…" ||
        rawStream == "正在重新生成…" ||
        rawStream.startsWith("正在调用 ") ||
        rawStream.startsWith("正在并行执行 ") ||
        rawStream.startsWith("⏳ ") ||
        rawStream.startsWith("⏰ 正在执行")

    val lastStep = s.streamTrace.lastOrNull()
    val singleLineToolText = when {
        rawStream.startsWith("⏳ ") -> rawStream
        rawStream.startsWith("正在并行执行 ") -> rawStream
        s.streamStepText.isNotBlank() -> s.streamStepText
        s.streamTool.isNotBlank() && !s.streamTool.startsWith("已执行") && !s.streamTool.startsWith("正在思考") ->
            humanizeToolName(s.streamTool, lastStep?.optJSONObject("arguments"))
        lastStep != null -> humanizeToolName(lastStep.optString("tool"), lastStep.optJSONObject("arguments"))
        else -> s.streamTool.ifBlank { "正在思考…" }
    }
    val cleanThought = s.streamThought.removePrefix("💭").removePrefix("⚡").trim()

    Column(
        Modifier.fillMaxWidth().padding(end = 44.dp),
        verticalArrangement = Arrangement.spacedBy(5.dp)
    ) {
        if (s.streamSubAgents.isNotEmpty()) {
            Row(
                Modifier.clip(RoundedCornerShape(12.dp))
                    .background(c.surface.copy(alpha = 0.88f))
                    .padding(horizontal = 10.dp, vertical = 5.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("协同：", color = c.accent, fontSize = 11.5.sp, fontWeight = FontWeight.SemiBold)
                s.streamSubAgents.take(3).forEach { sa ->
                    val bName = sa.optString("name", sa.optString("bot_id", "助手"))
                    val bSt = sa.optString("status", "running")
                    Text(
                        if (bSt == "done") "$bName ✓" else "$bName…",
                        color = if (bSt == "done") c.green else c.muted,
                        fontSize = 11.5.sp
                    )
                }
            }
        }

        if (s.streamTodos.isNotEmpty()) {
            Column(
                Modifier.clip(RoundedCornerShape(12.dp))
                    .background(c.surface.copy(alpha = 0.88f))
                    .padding(horizontal = 10.dp, vertical = 6.dp),
                verticalArrangement = Arrangement.spacedBy(3.dp)
            ) {
                s.streamTodos.take(5).forEach { td ->
                    val st = td.optString("status", "pending")
                    val title = td.optString("title", td.optString("step", ""))
                    if (title.isNotBlank()) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(
                                when (st) {
                                    "done", "completed" -> "✓ "
                                    "in_progress", "running" -> "▸ "
                                    else -> "· "
                                },
                                color = if (st in listOf("done", "completed")) c.green else c.accent,
                                fontSize = 11.5.sp,
                                fontWeight = FontWeight.Bold
                            )
                            Text(title, color = c.muted, fontSize = 11.5.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        }
                    }
                }
            }
        }

        // 2. Framed Streaming Assistant Bubble matching settled NativeMessage (unified timeline inside)
        val hasStreamContent = s.streamTimeline.isNotEmpty() || s.streamTrace.isNotEmpty() || cleanThought.isNotBlank() || (!isPlaceholder && rawStream.isNotBlank())
        if (hasStreamContent || isPlaceholder) {
            val streamShape = RoundedCornerShape(18.dp, 18.dp, 18.dp, 4.dp)
            val streamBorder = if (s.dark) Color(0x28FFFFFF) else Color(0x1E000000)
            Column(
                Modifier.clip(streamShape)
                    .background(c.aiBubble)
                    .border(0.8.dp, streamBorder, streamShape)
                    .padding(horizontal = 14.dp, vertical = 10.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                if (hasStreamContent) {
                    UnifiedTimelineBubbleContent(
                        model = model,
                        cardKey = "live_${roomBot.id}",
                        timeline = s.streamTimeline,
                        fallbackTrace = s.streamTrace,
                        fallbackText = if (!isPlaceholder) rawStream else "",
                        thought = cleanThought,
                        isLive = true,
                        streamStartedAt = s.streamStartedAt,
                    )
                } else {
                    Text(
                        singleLineToolText,
                        color = c.muted,
                        fontSize = 13.5.sp
                    )
                }
            }
        }

        if (s.streamChoices.isNotEmpty() && !isPlaceholder && rawStream.isNotBlank()) {
            Column(
                Modifier.fillMaxWidth()
                    .heightIn(max = 220.dp)
                    .verticalScroll(rememberScrollState())
                    .padding(top = 3.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                s.streamChoices.forEach { choice ->
                    Box(
                        Modifier.fillMaxWidth()
                            .clip(RoundedCornerShape(11.dp))
                            .background(c.surface.copy(alpha = 0.92f))
                            .iosPressScale(0.97f) { model.consumeChoiceAndSend("live_stream", choice.value) }
                            .padding(horizontal = 13.dp, vertical = 9.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            "✦ ${choice.label}",
                            modifier = Modifier.fillMaxWidth().basicMarquee(iterations = Int.MAX_VALUE),
                            color = c.accent,
                            fontSize = 13.5.sp,
                            fontWeight = FontWeight.SemiBold,
                            maxLines = 1,
                            textAlign = TextAlign.Center
                        )
                    }
                }
            }
        }
    }
}

// --- 3. Native Markdown Vault Browser & Editor Panel ---
@Composable
internal fun NativeVaultPanel(model: NativePulseModel, modifier: Modifier) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val focus = LocalFocusManager.current
    Column(modifier.background(c.bg)) {
        Surface(color = c.surface.copy(alpha = 0.96f), tonalElevation = 0.dp) {
            Column {
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Row(
                        Modifier.clip(RoundedCornerShape(10.dp))
                            .iosPressScale(0.95f) { focus.clearFocus(); model.closePanel() }
                            .padding(horizontal = 6.dp, vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        PulseIcon("back", c.accent, Modifier.size(21.dp))
                        Text("返回", color = c.accent, fontSize = 16.sp)
                    }
                    Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("透明记忆库", color = c.ink, fontSize = 16.5.sp, fontWeight = FontWeight.SemiBold)
                        Text(s.selectedVaultPath, color = c.muted, fontSize = 11.sp)
                    }
                    Text(
                        if (s.vaultSaving) "保存中…" else "保存",
                        Modifier.clip(RoundedCornerShape(14.dp))
                            .background(c.accent)
                            .iosPressScale(0.95f, enabled = !s.vaultSaving) { focus.clearFocus(); model.saveVaultFile() }
                            .padding(horizontal = 14.dp, vertical = 7.dp),
                        color = Color.White,
                        fontSize = 13.5.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                }
                HorizontalDivider(color = c.line.copy(alpha = 0.65f), thickness = 0.5.dp)
            }
        }
        LazyRow(
            contentPadding = PaddingValues(horizontal = 16.dp, vertical = 10.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            items(s.vaultFiles) { path ->
                val selected = path == s.selectedVaultPath
                Text(
                    path,
                    Modifier.clip(RoundedCornerShape(10.dp))
                        .background(if (selected) c.accent else c.surface)
                        .iosPressScale(0.95f) { model.selectVaultFile(path) }
                        .padding(horizontal = 12.dp, vertical = 7.dp),
                    color = if (selected) Color.White else c.ink,
                    fontSize = 12.5.sp,
                    fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal
                )
            }
        }
        Box(
            Modifier.weight(1f)
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 8.dp)
                .clip(RoundedCornerShape(18.dp))
                .background(c.surface)
                .padding(16.dp)
        ) {
            if (s.vaultLoading) {
                Text("正在读取 Markdown…", color = c.muted, fontSize = 14.sp)
            } else {
                val vaultScroll = remember(s.selectedVaultPath) { androidx.compose.foundation.ScrollState(0) }
                BasicTextField(
                    value = s.vaultContent,
                    onValueChange = { model.updateVaultContent(it) },
                    modifier = Modifier.fillMaxSize().verticalScroll(vaultScroll),
                    textStyle = TextStyle(color = c.ink, fontSize = 14.5.sp, lineHeight = 22.sp),
                    cursorBrush = SolidColor(c.accent)
                )
            }
        }
    }
}

@Composable
internal fun NativeSchedulesPanel(model: NativePulseModel, modifier: Modifier) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val focus = LocalFocusManager.current
    val data = s.schedulesData
    val routines = remember(data) { (data?.optJSONArray("routines") ?: JSONArray()).objects() }
    val deadlines = remember(data) { (data?.optJSONArray("deadlines") ?: JSONArray()).objects() }

    Column(modifier.background(c.bg)) {
        Surface(color = c.surface.copy(alpha = 0.96f), tonalElevation = 0.dp) {
            Column {
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Row(
                        Modifier.clip(RoundedCornerShape(10.dp))
                            .iosPressScale(0.95f) { focus.clearFocus(); model.closePanel() }
                            .padding(horizontal = 6.dp, vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        PulseIcon("back", c.accent, Modifier.size(21.dp))
                        Text("返回", color = c.accent, fontSize = 16.sp)
                    }
                    Text(
                        "定时与例行巡检",
                        Modifier.weight(1f),
                        color = c.ink,
                        fontSize = 17.sp,
                        fontWeight = FontWeight.SemiBold,
                        textAlign = TextAlign.Center
                    )
                    Text(
                        if (s.schedulesLoading) "刷新中" else "刷新",
                        Modifier.clip(RoundedCornerShape(12.dp))
                            .background(c.fill)
                            .iosPressScale(0.95f) { model.openSchedules() }
                            .padding(horizontal = 12.dp, vertical = 6.dp),
                        color = c.accent,
                        fontSize = 13.sp,
                        fontWeight = FontWeight.Medium
                    )
                }
                HorizontalDivider(color = c.line.copy(alpha = 0.65f), thickness = 0.5.dp)
            }
        }

        Column(
            Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp)
        ) {
            Column(
                Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(c.surface).padding(15.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Text("快速设定闹钟或巡检", color = c.ink, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
                Text("直接在聊天里告诉主助理时间与目标，它会自动调用 schedule_task 创建准点闹钟或每日例行任务。", color = c.muted, fontSize = 12.sp)
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(
                        "每天 07:30 晨间课表提醒",
                        Modifier.weight(1f)
                            .clip(RoundedCornerShape(12.dp))
                            .background(c.fill)
                            .iosPressScale(0.96f) {
                                model.closePanel()
                                model.open(s.primary)
                                model.sendQuickPrompt("帮我设置一个每天早上 07:30 的定点例行提醒，结合我的课表提醒我当天的课程和实验安排")
                            }
                            .padding(vertical = 10.dp, horizontal = 8.dp),
                        color = c.ink,
                        fontSize = 12.sp,
                        fontWeight = FontWeight.Medium,
                        textAlign = TextAlign.Center
                    )
                    Text(
                        "晚间 21:00 查学习通作业",
                        Modifier.weight(1f)
                            .clip(RoundedCornerShape(12.dp))
                            .background(c.fill)
                            .iosPressScale(0.96f) {
                                model.closePanel()
                                model.open(s.primary)
                                model.sendQuickPrompt("帮我设置一个每天晚上 21:00 的定点例行任务，检查我的待办死线和学习通作业")
                            }
                            .padding(vertical = 10.dp, horizontal = 8.dp),
                        color = c.ink,
                        fontSize = 12.sp,
                        fontWeight = FontWeight.Medium,
                        textAlign = TextAlign.Center
                    )
                }
            }

            Column(
                Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(c.surface).padding(15.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Text("常驻例行任务 (${routines.size})", color = c.ink, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
                if (routines.isEmpty()) {
                    Text("暂无常驻例行任务。可在聊天中随时让 Bot 添加。", color = c.muted, fontSize = 12.5.sp)
                } else {
                    routines.forEach { r ->
                        val id = r.optString("id")
                        val goal = r.optString("goal")
                        val enabled = r.optBoolean("enabled", true)
                        val dailyTime = r.optString("daily_time")
                        val hours = r.optInt("interval_hours", 24)
                        val botName = model.bot(r.optString("bot_id", s.primary))?.name ?: r.optString("bot_id", "主控")
                        Column(
                            Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(c.fill).padding(12.dp),
                            verticalArrangement = Arrangement.spacedBy(6.dp)
                        ) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    if (dailyTime.isNotBlank()) "每天 $dailyTime" else "每 ${hours}h",
                                    color = if (enabled) c.accent else c.faint,
                                    fontSize = 12.sp,
                                    fontWeight = FontWeight.SemiBold,
                                    style = TextStyle(fontFeatureSettings = "tnum")
                                )
                                Text(" · $botName", color = c.muted, fontSize = 11.5.sp, modifier = Modifier.weight(1f))
                                Text(
                                    "立即执行",
                                    Modifier.padding(end = 8.dp)
                                        .clip(RoundedCornerShape(8.dp))
                                        .background(c.surface)
                                        .iosPressScale(0.95f) { model.runRoutineItem(id) }
                                        .padding(horizontal = 8.dp, vertical = 4.dp),
                                    color = c.accent,
                                    fontSize = 11.sp,
                                    fontWeight = FontWeight.SemiBold
                                )
                                Switch(checked = enabled, onCheckedChange = { model.toggleRoutineItem(id, it) })
                            }
                            Text(goal, color = c.ink, fontSize = 13.5.sp, lineHeight = 19.sp)
                        }
                    }
                }
            }

            Column(
                Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(c.surface).padding(15.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Text("零 Token 哨兵监控 (${s.watchdogs.size})", color = c.ink, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
                if (s.watchdogs.isEmpty()) {
                    Text("暂无活跃哨兵脚本。可让 Bot 调用 watchdog_task 编写低开销网页/接口巡检脚本，仅在检测到目标变化时唤醒大模型。", color = c.muted, fontSize = 12.5.sp)
                } else {
                    s.watchdogs.forEach { w ->
                        val wid = w.optString("id")
                        val wname = w.optString("name", wid)
                        val enabled = w.optBoolean("enabled", true)
                        val intervalSec = w.optInt("interval_sec", 60)
                        val lastStatus = w.optString("last_status", "待运行")
                        val runs = w.optInt("runs", 0)
                        Column(
                            Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(c.fill).padding(12.dp),
                            verticalArrangement = Arrangement.spacedBy(5.dp)
                        ) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    if (enabled) "● 运行中 · 每 ${intervalSec}s" else "○ 已停止",
                                    color = if (enabled) c.green else c.faint,
                                    fontSize = 12.sp,
                                    fontWeight = FontWeight.SemiBold,
                                    modifier = Modifier.weight(1f),
                                    style = TextStyle(fontFeatureSettings = "tnum")
                                )
                                Text("已巡检 ${runs} 次", color = c.muted, fontSize = 11.5.sp, modifier = Modifier.padding(end = 8.dp), style = TextStyle(fontFeatureSettings = "tnum"))
                                if (enabled) {
                                    Text(
                                        "停止",
                                        Modifier.clip(RoundedCornerShape(8.dp))
                                            .background(c.surface)
                                            .iosPressScale(0.95f) { model.stopWatchdog(wid) }
                                            .padding(horizontal = 9.dp, vertical = 4.dp),
                                        color = c.warn,
                                        fontSize = 11.5.sp,
                                        fontWeight = FontWeight.Medium
                                    )
                                }
                            }
                            Text(wname, color = c.ink, fontSize = 13.5.sp, fontWeight = FontWeight.Medium)
                            if (lastStatus.isNotBlank()) {
                                Text("最近状态：$lastStatus", color = c.muted, fontSize = 11.5.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
                            }
                        }
                    }
                }
            }

            Column(
                Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(c.surface).padding(15.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Text("静态前缀钉住记忆 (${s.pinnedMemories.size})", color = c.ink, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
                if (s.pinnedMemories.isEmpty()) {
                    Text("暂无钉住记忆。可在任意消息菜单点按「钉住记忆」，将其锁定在缓存静态前缀中永不遗忘。", color = c.muted, fontSize = 12.5.sp)
                } else {
                    s.pinnedMemories.forEach { p ->
                        val pid = p.optString("id")
                        val ptext = p.optString("text")
                        Row(
                            Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(c.fill).padding(12.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text(ptext, color = c.ink, fontSize = 13.sp, lineHeight = 18.sp, modifier = Modifier.weight(1f))
                            Text(
                                "移除",
                                Modifier.padding(start = 8.dp)
                                    .clip(RoundedCornerShape(8.dp))
                                    .background(c.surface)
                                    .iosPressScale(0.95f) { model.removeMemoryPin(pid) }
                                    .padding(horizontal = 9.dp, vertical = 4.dp),
                                color = c.warn,
                                fontSize = 11.5.sp,
                                fontWeight = FontWeight.Medium
                            )
                        }
                    }
                }
            }

            Column(
                Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(c.surface).padding(15.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                Text("已登记截止时间 / 死线 (${deadlines.size})", color = c.ink, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
                if (deadlines.isEmpty()) {
                    Text("暂无待办死线。", color = c.muted, fontSize = 12.5.sp)
                } else {
                    deadlines.forEach { d ->
                        val title = d.optString("title")
                        val dueAt = d.optDouble("due_at", 0.0)
                        val notified = d.optBoolean("notified", false)
                        Row(
                            Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(c.fill).padding(12.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text(title, color = c.ink, fontSize = 13.5.sp, fontWeight = FontWeight.Medium)
                                if (dueAt > 0) {
                                    Text("截止：${model.day(dueAt)} ${model.time(dueAt)}", color = c.muted, fontSize = 11.5.sp, style = TextStyle(fontFeatureSettings = "tnum"))
                                }
                            }
                            Text(
                                if (notified) "已提醒" else "待提醒",
                                color = if (notified) c.faint else c.green,
                                fontSize = 11.5.sp,
                                fontWeight = FontWeight.Medium
                            )
                        }
                    }
                }
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}
