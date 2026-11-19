package dev.bubuki.proactive

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutLinearInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.LinearOutSlowInEasing
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import dev.bubuki.proactive.markdown.MarkdownText
import org.json.JSONObject

/**
 * [Module] PulseScreenToolTrace
 * [Responsibility] Full-width unified turn process accordion (`NativeTurnProcessCard`) for both
 *                  live streaming turns and settled assistant messages, animating purely along
 *                  the vertical axis without horizontal width snapping.
 * [Public API]
 * - NativeTurnProcessCard(...)
 * - NativeToolTraceGroup(...)
 */

private fun extractProcessStepNotes(
    trace: List<JSONObject>,
    turnSteps: List<JSONObject>,
    liveStepText: String
): List<String> {
    val notes = ArrayList<String>()
    fun pushNote(raw: String) {
        val clean = raw.trim()
        if (clean.isNotBlank() && notes.lastOrNull() != clean) {
            notes.add(clean)
        }
    }
    for (item in turnSteps) {
        val t = item.optString("type")
        if (t == "step_text") {
            pushNote(item.optString("text"))
        } else if (item.optString("role") == "assistant") {
            pushNote(item.optString("content"))
        }
    }
    for (tr in trace) {
        pushNote(tr.optString("step_note"))
    }
    if (liveStepText.isNotBlank()) {
        pushNote(liveStepText)
    }
    return notes
}

@Composable
internal fun NativeTurnProcessCard(
    model: NativePulseModel,
    cardKey: String,
    trace: List<JSONObject>,
    thought: String,
    turnSteps: List<JSONObject> = emptyList(),
    liveStepText: String = "",
    liveToolLabel: String = "",
    isLive: Boolean = false,
    streamStartedAt: Long = 0L,
    apiCalls: Int = 1
) {
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    val cleanThought = remember(thought, isLive) {
        val raw = thought.removePrefix("💭").removePrefix("⚡").trim()
        when {
            raw.startsWith("正在生成 ") || raw.startsWith("正在准备调用 ") -> ""
            !isLive && raw == "正在向云端发送指令并建立实时流…" -> ""
            else -> raw
        }
    }
    val stepNotes = remember(trace, turnSteps, liveStepText) {
        extractProcessStepNotes(trace, turnSteps, liveStepText)
    }
    val hasBody = cleanThought.isNotBlank() || trace.isNotEmpty() || stepNotes.isNotEmpty()

    var expanded by rememberSaveable(cardKey) { mutableStateOf(isLive) }
    val totalMs = remember(trace) { trace.sumOf { it.optLong("duration_ms", 0L) } }
    val chevRot by animateFloatAsState(
        targetValue = if (expanded && hasBody) 180f else 0f,
        animationSpec = spring(dampingRatio = 0.85f, stiffness = 440f),
        label = "proc_chev"
    )

    val lifecycleStarted = rememberLifecycleStarted()
    var nowMs by remember(streamStartedAt) { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(isLive, streamStartedAt, lifecycleStarted) {
        if (!isLive || streamStartedAt <= 0L || !lifecycleStarted) return@LaunchedEffect
        nowMs = System.currentTimeMillis()
        while (true) {
            kotlinx.coroutines.delay(1000L)
            nowMs = System.currentTimeMillis()
        }
    }

    val headerTitle = remember(isLive, liveStepText, liveToolLabel, trace, cleanThought, stepNotes, apiCalls) {
        if (isLive) {
            liveStepText.trim().ifBlank {
                liveToolLabel.trim().ifBlank {
                    stepNotes.lastOrNull() ?: if (trace.isNotEmpty()) {
                        humanizeToolName(trace.last().optString("tool"), trace.last().optJSONObject("arguments"))
                    } else {
                        "正在思考…"
                    }
                }
            }
        } else {
            val parts = ArrayList<String>(3)
            if (cleanThought.isNotBlank()) {
                parts.add(if (apiCalls > 1) "已思考 (${apiCalls}轮)" else "已思考")
            }
            if (trace.isNotEmpty()) {
                val firstTool = humanizeToolName(trace.first().optString("tool"), trace.first().optJSONObject("arguments"))
                parts.add(if (trace.size == 1) firstTool else "执行 ${trace.size} 步 · $firstTool")
            } else if (stepNotes.isNotEmpty()) {
                parts.add("${stepNotes.size} 条过程说明")
            }
            if (parts.isEmpty()) "处理过程" else parts.joinToString(" · ")
        }
    }

    val metaLabel = if (isLive && streamStartedAt > 0L) {
        val elapsedSec = ((nowMs - streamStartedAt) / 1000L).coerceAtLeast(0L)
        val elStr = "%d:%02d".format(elapsedSec / 60, elapsedSec % 60)
        if (trace.isNotEmpty()) "${trace.size}步 · $elStr" else elStr
    } else if (!isLive && totalMs > 0L) {
        if (totalMs >= 1000L) "%.1fs".format(totalMs / 1000.0) else "${totalMs}ms"
    } else {
        ""
    }

    val thoughtScroll = rememberScrollState()
    LaunchedEffect(isLive, cleanThought.length / 24) {
        if (isLive && expanded && hasBody) {
            runCatching { thoughtScroll.scrollTo(thoughtScroll.maxValue) }
        }
    }

    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(13.dp))
            .background(c.surface.copy(alpha = 0.92f))
            .border(0.6.dp, c.line.copy(alpha = 0.72f), RoundedCornerShape(13.dp))
            .clickable(enabled = hasBody) {
                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                expanded = !expanded
            }
            .padding(horizontal = 12.dp, vertical = 8.dp)
    ) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Box(
                Modifier.size(6.5.dp)
                    .clip(CircleShape)
                    .background(if (isLive) c.accent else c.accent.copy(alpha = 0.75f))
            )
            Text(
                headerTitle,
                Modifier.padding(start = 8.dp).weight(1f),
                color = if (isLive) c.ink else c.muted,
                fontSize = 12.5.sp,
                fontWeight = FontWeight.Medium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
            if (metaLabel.isNotBlank()) {
                Text(
                    metaLabel,
                    Modifier.padding(start = 8.dp),
                    color = c.faint,
                    fontSize = 11.5.sp,
                    style = TextStyle(fontFeatureSettings = "tnum")
                )
            }
            if (hasBody) {
                Text(
                    "▾",
                    Modifier.padding(start = 5.dp).graphicsLayer { rotationZ = chevRot },
                    color = c.faint,
                    fontSize = 12.sp
                )
            }
        }

        AnimatedVisibility(
            visible = expanded && hasBody,
            enter = expandVertically(spring(dampingRatio = 0.88f, stiffness = 420f)) + fadeIn(tween(150)),
            exit = shrinkVertically(spring(dampingRatio = 0.92f, stiffness = 460f)) + fadeOut(tween(120))
        ) {
            Column(
                Modifier.fillMaxWidth()
                    .padding(top = 8.dp)
                    .then(if (isLive) Modifier.heightIn(max = 220.dp).verticalScroll(thoughtScroll) else Modifier),
                verticalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                if (cleanThought.isNotBlank()) {
                    Row(Modifier.fillMaxWidth()) {
                        Box(
                            Modifier.width(2.5.dp)
                                .heightIn(min = 18.dp)
                                .clip(RoundedCornerShape(2.dp))
                                .background(c.accent.copy(alpha = 0.55f))
                        )
                        Text(
                            cleanThought,
                            Modifier.padding(start = 8.dp).weight(1f),
                            color = c.muted,
                            fontSize = 12.5.sp,
                            lineHeight = 18.sp
                        )
                    }
                }
                if (stepNotes.isNotEmpty()) {
                    stepNotes.forEach { note ->
                        Text(
                            "💬 $note",
                            Modifier.fillMaxWidth().padding(start = 2.dp),
                            color = c.ink.copy(alpha = 0.82f),
                            fontSize = 12.sp,
                            lineHeight = 17.sp,
                            maxLines = 4,
                            overflow = TextOverflow.Ellipsis
                        )
                    }
                }
                if (trace.isNotEmpty()) {
                    Column(
                        Modifier.fillMaxWidth().padding(start = 2.dp),
                        verticalArrangement = Arrangement.spacedBy(4.5.dp)
                    ) {
                        val displayTrace = if (isLive) trace.takeLast(10) else trace
                        displayTrace.forEach { step ->
                            val toolName = step.optString("tool")
                            val argsObj = step.optJSONObject("arguments")
                            val dur = step.optLong("duration_ms", 0L)
                            val resObj = step.optJSONObject("result")
                            val statusStr = step.optString("status", "")
                            val isRunning = statusStr == "running"
                            val hasErr = statusStr == "error" ||
                                resObj?.has("error") == true ||
                                resObj?.optBoolean("ok", true) == false
                            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    if (isRunning) "▸" else if (hasErr) "✕" else "✓",
                                    color = if (isRunning) c.accent else if (hasErr) c.warn else c.green,
                                    fontSize = 11.sp,
                                    fontWeight = FontWeight.Bold
                                )
                                Text(
                                    humanizeToolName(toolName, argsObj),
                                    Modifier.padding(start = 6.dp).weight(1f),
                                    color = if (hasErr) c.warn else c.ink.copy(alpha = 0.86f),
                                    fontSize = 12.sp,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis
                                )
                                if (toolName == "cloud_desktop" || toolName == "cloud") {
                                    Text(
                                        "查看电脑 ›",
                                        Modifier.padding(start = 6.dp)
                                            .clip(RoundedCornerShape(6.dp))
                                            .background(c.accent.copy(alpha = 0.14f))
                                            .clickable { model.openCloudDesktop() }
                                            .padding(horizontal = 7.dp, vertical = 2.dp),
                                        color = c.accent,
                                        fontSize = 11.sp,
                                        fontWeight = FontWeight.Medium
                                    )
                                }
                                if (dur > 0) {
                                    Text(
                                        "${dur}ms",
                                        Modifier.padding(start = 6.dp),
                                        color = c.faint,
                                        fontSize = 11.sp,
                                        style = TextStyle(fontFeatureSettings = "tnum")
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
internal fun NativeToolTraceGroup(model: NativePulseModel, messageId: String, trace: List<JSONObject>) {
    NativeTurnProcessCard(
        model = model,
        cardKey = messageId,
        trace = trace,
        thought = "",
        isLive = false
    )
}

private data class AppleProcessStep(
    val tool: String,
    val args: JSONObject?,
    val result: JSONObject?,
    val status: String,
    val durationMs: Long,
    val contextNote: String = "",
)

@Composable
internal fun UnifiedTimelineBubbleContent(
    model: NativePulseModel,
    cardKey: String,
    timeline: List<JSONObject>,
    fallbackTrace: List<JSONObject>,
    fallbackText: String,
    thought: String,
    isLive: Boolean,
    isError: Boolean = false,
    streamStartedAt: Long = 0L,
    typewriterCharBudget: Int = Int.MAX_VALUE,
    inlineStripRegex: Regex? = null,
    inlineStripSet: List<String> = emptyList(),
) {
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    val reduceMotion = LocalReduceMotion.current

    val cleanThought = remember(thought, isLive) {
        val raw = thought.removePrefix("💭").removePrefix("⚡").trim()
        when {
            raw.startsWith("正在生成 ") || raw.startsWith("正在准备调用 ") -> ""
            !isLive && raw == "正在向云端发送指令并建立实时流…" -> ""
            else -> raw
        }
    }

    val (pureProse, processSteps) = remember(timeline, fallbackTrace, fallbackText, inlineStripRegex, inlineStripSet) {
        val proseSegs = ArrayList<String>()
        val steps = ArrayList<AppleProcessStep>()
        var lastShortNote = ""
        if (timeline.isNotEmpty()) {
            for (item in timeline) {
                when (item.optString("type")) {
                    "text" -> {
                        val rawSeg = item.optString("text", "")
                        val cleanSeg = if (inlineStripRegex == null || inlineStripSet.isEmpty()) rawSeg.trim() else {
                            rawSeg.replace(inlineStripRegex) { m ->
                                if (m.groupValues[1].trim() in inlineStripSet) "" else m.value
                            }.replace(Regex("""\n{3,}"""), "\n\n").trim()
                        }
                        if (cleanSeg.isNotBlank()) {
                            proseSegs.add(cleanSeg)
                            val tailLine = cleanSeg.lines().lastOrNull { it.isNotBlank() }?.trim().orEmpty()
                            lastShortNote = if (tailLine.length > 32) tailLine.take(31) + "…" else tailLine
                        }
                    }
                    "tool" -> {
                        steps.add(
                            AppleProcessStep(
                                tool = item.optString("tool", "tool"),
                                args = item.optJSONObject("arguments"),
                                result = item.optJSONObject("result"),
                                status = item.optString("status", "done"),
                                durationMs = item.optLong("duration_ms", 0L),
                                contextNote = lastShortNote,
                            )
                        )
                    }
                }
            }
        } else {
            for (tr in fallbackTrace) {
                steps.add(
                    AppleProcessStep(
                        tool = tr.optString("tool", "tool"),
                        args = tr.optJSONObject("arguments"),
                        result = tr.optJSONObject("result"),
                        status = tr.optString("status", "done"),
                        durationMs = tr.optLong("duration_ms", 0L),
                        contextNote = tr.optString("step_note", ""),
                    )
                )
            }
        }
        val body = if (proseSegs.isNotEmpty()) proseSegs.joinToString("\n\n") else fallbackText.trim()
        body to steps
    }

    var elapsedSec by remember(cardKey) { mutableLongStateOf(0L) }
    LaunchedEffect(isLive, streamStartedAt) {
        if (isLive && streamStartedAt > 0L) {
            while (true) {
                elapsedSec = ((System.currentTimeMillis() - streamStartedAt) / 1000L).coerceAtLeast(0L)
                kotlinx.coroutines.delay(500L)
            }
        }
    }

    val beamProgress = remember(cardKey) { Animatable(0f) }
    LaunchedEffect(isLive, reduceMotion) {
        if (isLive && !reduceMotion) {
            while (true) {
                beamProgress.snapTo(0f)
                beamProgress.animateTo(1f, tween(durationMillis = 1250, easing = LinearEasing))
            }
        } else {
            beamProgress.snapTo(0f)
        }
    }

    val hasProcess = cleanThought.isNotBlank() || processSteps.isNotEmpty()
    var expanded by rememberSaveable("proc_$cardKey") { mutableStateOf(false) }
    val chevronRot by animateFloatAsState(
        targetValue = if (expanded) 90f else 0f,
        animationSpec = tween(durationMillis = 200, easing = LinearEasing),
        label = "proc_chev"
    )
    val totalDurMs = remember(processSteps) { processSteps.sumOf { it.durationMs } }
    val hasAnyErr = remember(processSteps) {
        processSteps.any { it.status == "error" || it.result?.has("error") == true || it.result?.optBoolean("ok", true) == false }
    }
    val runningStep = remember(processSteps, isLive) {
        if (!isLive) null else processSteps.lastOrNull { it.status == "running" } ?: processSteps.lastOrNull()
    }
    val eyebrowLabel = remember(isLive, elapsedSec, cleanThought, processSteps, runningStep, totalDurMs) {
        if (isLive) {
            when {
                runningStep != null -> {
                    val act = humanizeToolName(runningStep.tool, runningStep.args)
                    val cntTag = if (processSteps.size > 1) " · 第 ${processSteps.size} 步" else ""
                    "$act$cntTag"
                }
                cleanThought.isNotBlank() -> if (elapsedSec > 0) "正在思考 · ${elapsedSec}s" else "正在思考…"
                else -> if (elapsedSec > 0) "正在处理 · ${elapsedSec}s" else "正在思考…"
            }
        } else {
            val durS = when {
                totalDurMs >= 1000L -> String.format(java.util.Locale.US, " · %.1fs", totalDurMs / 1000.0)
                totalDurMs > 0L -> " · ${totalDurMs}ms"
                else -> ""
            }
            when {
                cleanThought.isNotBlank() && processSteps.isNotEmpty() -> "已思考并执行 ${processSteps.size} 步$durS"
                processSteps.isNotEmpty() -> "已完成 ${processSteps.size} 步$durS"
                else -> "思考过程"
            }
        }
    }

    Column(
        Modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        if (hasProcess) {
            Column(Modifier.fillMaxWidth()) {
                Row(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(8.dp))
                        .clickable(
                            interactionSource = remember { MutableInteractionSource() },
                            indication = null
                        ) {
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            expanded = !expanded
                        }
                        .padding(vertical = 3.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    val dotColor = when {
                        hasAnyErr -> c.warn
                        isLive -> c.accent
                        else -> c.muted.copy(alpha = 0.55f)
                    }
                    Box(
                        Modifier.size(6.dp)
                            .clip(CircleShape)
                            .background(dotColor)
                    )
                    AnimatedContent(
                        targetState = eyebrowLabel,
                        modifier = Modifier.weight(1f),
                        transitionSpec = {
                            if (reduceMotion) {
                                fadeIn(tween(100)) togetherWith fadeOut(tween(80))
                            } else {
                                (fadeIn(tween(180, easing = LinearEasing)) + slideInVertically(tween(180, easing = LinearEasing)) { it / 4 }) togetherWith
                                    (fadeOut(tween(120, easing = LinearEasing)) + slideOutVertically(tween(120, easing = LinearEasing)) { -it / 4 })
                            }
                        },
                        label = "eyebrow_txt"
                    ) { lbl ->
                        Text(
                            lbl,
                            color = if (isLive) c.ink.copy(alpha = 0.78f) else c.muted.copy(alpha = 0.85f),
                            fontSize = 12.5.sp,
                            fontWeight = FontWeight.Medium,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                            style = TextStyle(fontFeatureSettings = "tnum")
                        )
                    }
                    Text(
                        "›",
                        Modifier.graphicsLayer { rotationZ = chevronRot },
                        color = c.faint,
                        fontSize = 14.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                }

                // Hairline divider with smooth linear beam animation while live
                Box(Modifier.fillMaxWidth().padding(top = 4.dp).height(1.5.dp)) {
                    val baseLineColor = c.line.copy(alpha = if (isLive) 0.45f else 0.35f)
                    val accentCol = c.accent
                    Canvas(Modifier.fillMaxWidth().height(1.5.dp)) {
                        drawLine(
                            color = baseLineColor,
                            start = Offset(0f, size.height / 2f),
                            end = Offset(size.width, size.height / 2f),
                            strokeWidth = 1f
                        )
                        if (isLive && !reduceMotion) {
                            val p = beamProgress.value
                            val segW = size.width * 0.34f
                            val startX = (size.width + segW) * p - segW
                            drawLine(
                                brush = Brush.horizontalGradient(
                                    colors = listOf(Color.Transparent, accentCol.copy(alpha = 0.88f), Color.Transparent),
                                    startX = startX,
                                    endX = startX + segW
                                ),
                                start = Offset(startX.coerceAtLeast(0f), size.height / 2f),
                                end = Offset((startX + segW).coerceAtMost(size.width), size.height / 2f),
                                strokeWidth = 3f
                            )
                        }
                    }
                }

                AnimatedVisibility(
                    visible = expanded,
                    enter = expandVertically(tween(240, easing = LinearOutSlowInEasing)) + fadeIn(tween(180, easing = LinearEasing)),
                    exit = shrinkVertically(tween(180, easing = FastOutLinearInEasing)) + fadeOut(tween(120, easing = LinearEasing))
                ) {
                    Column(
                        Modifier.fillMaxWidth()
                            .padding(top = 8.dp, bottom = 2.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        if (cleanThought.isNotBlank()) {
                            Row(Modifier.fillMaxWidth()) {
                                Box(
                                    Modifier.width(2.dp)
                                        .height(14.dp)
                                        .padding(top = 2.dp)
                                        .clip(CircleShape)
                                        .background(c.line)
                                )
                                Text(
                                    cleanThought,
                                    Modifier.padding(start = 8.dp),
                                    color = c.muted,
                                    fontSize = 12.sp,
                                    lineHeight = 17.sp
                                )
                            }
                        }
                        if (processSteps.isNotEmpty()) {
                            Column(
                                Modifier.fillMaxWidth().padding(start = 2.dp),
                                verticalArrangement = Arrangement.spacedBy(6.dp)
                            ) {
                                var prevCtx = ""
                                processSteps.forEachIndexed { idx, step ->
                                    if (step.contextNote.isNotBlank() && step.contextNote != prevCtx) {
                                        prevCtx = step.contextNote
                                        Text(
                                            "“${step.contextNote}”",
                                            color = c.faint,
                                            fontSize = 11.sp,
                                            fontStyle = FontStyle.Italic,
                                            maxLines = 1,
                                            overflow = TextOverflow.Ellipsis,
                                            modifier = Modifier.padding(start = 12.dp, top = 2.dp)
                                        )
                                    }
                                    var stepOpen by rememberSaveable("st_${cardKey}_$idx") { mutableStateOf(false) }
                                    val stErr = step.status == "error" || step.result?.has("error") == true || step.result?.optBoolean("ok", true) == false
                                    val stRun = step.status == "running"
                                    val durLbl = when {
                                        stRun -> "进行中"
                                        step.durationMs >= 1000L -> String.format(java.util.Locale.US, "%.1fs", step.durationMs / 1000.0)
                                        step.durationMs > 0L -> "${step.durationMs}ms"
                                        else -> ""
                                    }
                                    Column(Modifier.fillMaxWidth()) {
                                        Row(
                                            Modifier.fillMaxWidth()
                                                .clip(RoundedCornerShape(6.dp))
                                                .clickable { stepOpen = !stepOpen }
                                                .padding(vertical = 2.dp),
                                            verticalAlignment = Alignment.CenterVertically,
                                            horizontalArrangement = Arrangement.spacedBy(7.dp)
                                        ) {
                                            Box(
                                                Modifier.size(5.5.dp)
                                                    .clip(CircleShape)
                                                    .background(
                                                        when {
                                                            stRun -> c.accent
                                                            stErr -> c.warn
                                                            else -> c.green
                                                        }
                                                    )
                                            )
                                            Text(
                                                humanizeToolName(step.tool, step.args),
                                                Modifier.weight(1f),
                                                color = if (stErr) c.warn else c.ink.copy(alpha = 0.86f),
                                                fontSize = 12.sp,
                                                maxLines = 1,
                                                overflow = TextOverflow.Ellipsis
                                            )
                                            if (step.tool == "cloud_desktop" || step.tool == "cloud") {
                                                Text(
                                                    "查看电脑 ›",
                                                    Modifier.clickable { model.openCloudDesktop() }.padding(horizontal = 4.dp),
                                                    color = c.accent,
                                                    fontSize = 11.sp,
                                                    fontWeight = FontWeight.Medium
                                                )
                                            }
                                            if (durLbl.isNotEmpty()) {
                                                Text(
                                                    durLbl,
                                                    color = if (stRun) c.accent else c.faint,
                                                    fontSize = 11.sp,
                                                    style = TextStyle(fontFeatureSettings = "tnum")
                                                )
                                            }
                                        }
                                        AnimatedVisibility(
                                            visible = stepOpen,
                                            enter = expandVertically(tween(180, easing = LinearEasing)) + fadeIn(tween(140)),
                                            exit = shrinkVertically(tween(140, easing = LinearEasing)) + fadeOut(tween(100))
                                        ) {
                                            val argsStr = step.args?.toString()?.take(360).orEmpty()
                                            val resStr = step.result?.toString()?.take(480).orEmpty()
                                            Column(
                                                Modifier.fillMaxWidth().padding(start = 13.dp, top = 3.dp, bottom = 2.dp),
                                                verticalArrangement = Arrangement.spacedBy(2.dp)
                                            ) {
                                                if (argsStr.isNotBlank() && argsStr != "{}") {
                                                    Text("参数：$argsStr", color = c.faint, fontSize = 10.5.sp, lineHeight = 14.sp)
                                                }
                                                if (resStr.isNotBlank() && resStr != "{}") {
                                                    Text(
                                                        "返回：$resStr",
                                                        color = if (stErr) c.warn else c.faint,
                                                        fontSize = 10.5.sp,
                                                        lineHeight = 14.sp,
                                                        maxLines = 6,
                                                        overflow = TextOverflow.Ellipsis
                                                    )
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        if (pureProse.isNotBlank() && typewriterCharBudget > 0) {
            val shownText = if (typewriterCharBudget < pureProse.length) {
                val cut = typewriterCharBudget.coerceIn(1, pureProse.length)
                val safeCut = if (cut < pureProse.length && pureProse[cut - 1].isHighSurrogate()) cut + 1 else cut
                pureProse.substring(0, safeCut.coerceAtMost(pureProse.length))
            } else pureProse
            MarkdownText(
                remember(shownText) { nativeWikiMarkdown(shownText) },
                TextStyle(color = if (isError) c.warn else c.ink, fontSize = 16.sp, lineHeight = 23.sp),
                streaming = isLive || (typewriterCharBudget < Int.MAX_VALUE),
                onOpenUri = model::openLink
            )
        } else if (isLive && !hasProcess) {
            Text("正在思考…", color = c.muted, fontSize = 13.5.sp)
        }
    }
}
