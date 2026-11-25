package dev.bubuki.proactive

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutLinearInEasing
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.LinearOutSlowInEasing
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.snap
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
import androidx.compose.foundation.layout.IntrinsicSize
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
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
 * [Responsibility] Full-width unified turn process accordion (`UnifiedTimelineBubbleContent`) for both
 *                  live streaming turns and settled assistant messages, gating tickers and beam animations
 *                  on lifecycle and battery-saver mode.
 * [Public API]
 * - UnifiedTimelineBubbleContent(...)
 */

private val RE_TRACE_MULTI_NL = Regex("""\n{3,}""")

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
    turnId: String = "",
    inlineStripRegex: Regex? = null,
    inlineStripSet: List<String> = emptyList(),
) {
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    val reduceMotion = LocalReduceMotion.current
    val powerSave = LocalPowerSaveMode.current
    val effectiveReduce = reduceMotion || powerSave
    val lifecycleStarted = rememberLifecycleStarted()

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
                        val rawSeg = stripResidualControlTags(item.optString("text", ""))
                        val cleanSeg = if (inlineStripRegex == null || inlineStripSet.isEmpty()) rawSeg else {
                            rawSeg.replace(inlineStripRegex) { m ->
                                if (m.groupValues[1].trim() in inlineStripSet) "" else m.value
                            }.replace(RE_TRACE_MULTI_NL, "\n\n").trim()
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
        val body = if (proseSegs.isNotEmpty()) proseSegs.joinToString("\n\n") else stripResidualControlTags(fallbackText)
        body to steps
    }

    var elapsedSec by remember(cardKey) { mutableLongStateOf(0L) }
    LaunchedEffect(isLive, streamStartedAt, lifecycleStarted, powerSave) {
        if (isLive && streamStartedAt > 0L && lifecycleStarted) {
            val tickMs = if (powerSave) 1000L else 500L
            while (true) {
                elapsedSec = ((System.currentTimeMillis() - streamStartedAt) / 1000L).coerceAtLeast(0L)
                kotlinx.coroutines.delay(tickMs)
            }
        }
    }

    val beamProgress = remember(cardKey) { Animatable(0f) }
    LaunchedEffect(isLive, reduceMotion, powerSave, lifecycleStarted) {
        if (isLive && !reduceMotion && !powerSave && lifecycleStarted) {
            while (true) {
                beamProgress.snapTo(0f)
                beamProgress.animateTo(1f, tween(durationMillis = 1250, easing = LinearEasing))
            }
        } else {
            beamProgress.snapTo(0f)
        }
    }

    val hasProcess = cleanThought.isNotBlank() || processSteps.isNotEmpty()
    val stateKey = remember(cardKey, turnId) { if (turnId.isNotBlank()) "turn_$turnId" else "card_$cardKey" }
    var expanded by rememberSaveable("proc_$stateKey") {
        mutableStateOf(
            if (turnId.isNotBlank()) (model.liveProcessExpandedByBot[turnId] ?: false) else false
        )
    }
    LaunchedEffect(turnId) {
        if (turnId.isNotBlank()) {
            val saved = model.liveProcessExpandedByBot[turnId]
            if (saved != null && saved != expanded) expanded = saved
        }
    }
    val chevronRot by animateFloatAsState(
        targetValue = if (expanded) 90f else 0f,
        animationSpec = if (effectiveReduce) snap() else iosGentleSpring(),
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
                        .heightIn(min = 40.dp)
                        .clip(RoundedCornerShape(8.dp))
                        .clickable(
                            interactionSource = remember { MutableInteractionSource() },
                            indication = null
                        ) {
                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                            expanded = !expanded
                            if (turnId.isNotBlank()) {
                                if (model.liveProcessExpandedByBot.size > 100) {
                                    model.liveProcessExpandedByBot.clear()
                                }
                                model.liveProcessExpandedByBot[turnId] = expanded
                            }
                        }
                        .padding(vertical = 8.dp),
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
                    Text(
                        eyebrowLabel,
                        modifier = Modifier.weight(1f),
                        color = if (isLive) c.ink.copy(alpha = 0.78f) else c.muted.copy(alpha = 0.85f),
                        fontSize = 12.5.sp,
                        fontWeight = FontWeight.Medium,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        style = TextStyle(fontFeatureSettings = "tnum")
                    )
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
                        if (isLive && !reduceMotion && !powerSave && lifecycleStarted) {
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
                    enter = if (effectiveReduce) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(160)),
                    exit = if (effectiveReduce) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(110))
                ) {
                    Column(
                        Modifier.fillMaxWidth()
                            .padding(top = 6.dp, bottom = 2.dp),
                        verticalArrangement = Arrangement.spacedBy(5.dp)
                    ) {
                        if (cleanThought.isNotBlank()) {
                            var thoughtOpen by rememberSaveable("th_$stateKey") { mutableStateOf(false) }
                            val thChev by animateFloatAsState(
                                targetValue = if (thoughtOpen) 90f else 0f,
                                animationSpec = if (effectiveReduce) snap() else iosGentleSpring(),
                                label = "th_chev"
                            )
                            Column(
                                Modifier.fillMaxWidth()
                                    .clip(RoundedCornerShape(12.dp))
                                    .background(c.fill.copy(alpha = 0.42f))
                                    .padding(horizontal = 10.dp, vertical = 4.dp)
                            ) {
                                Row(
                                    Modifier.fillMaxWidth()
                                        .heightIn(min = 34.dp)
                                        .clickable(
                                            interactionSource = remember { MutableInteractionSource() },
                                            indication = null
                                        ) {
                                            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                            thoughtOpen = !thoughtOpen
                                        },
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(7.dp)
                                ) {
                                    Box(Modifier.size(5.5.dp).clip(CircleShape).background(c.muted.copy(alpha = 0.7f)))
                                    Text(
                                        "思考过程（${cleanThought.length} 字）",
                                        Modifier.weight(1f),
                                        color = c.ink.copy(alpha = 0.82f),
                                        fontSize = 12.sp,
                                        fontWeight = FontWeight.Medium,
                                        maxLines = 1,
                                        overflow = TextOverflow.Ellipsis
                                    )
                                    Text(
                                        "›",
                                        Modifier.graphicsLayer { rotationZ = thChev },
                                        color = c.faint,
                                        fontSize = 14.sp,
                                        fontWeight = FontWeight.SemiBold
                                    )
                                }
                                AnimatedVisibility(
                                    visible = thoughtOpen,
                                    enter = if (effectiveReduce) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(140)),
                                    exit = if (effectiveReduce) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(100))
                                ) {
                                    Text(
                                        cleanThought,
                                        Modifier.padding(top = 4.dp, bottom = 6.dp),
                                        color = c.muted,
                                        fontSize = 12.sp,
                                        lineHeight = 18.sp
                                    )
                                }
                            }
                        }
                        if (processSteps.isNotEmpty()) {
                            Column(
                                Modifier.fillMaxWidth().padding(start = 2.dp),
                                verticalArrangement = Arrangement.spacedBy(5.dp)
                            ) {
                                processSteps.forEachIndexed { idx, step ->
                                    var stepOpen by rememberSaveable("st_${cardKey}_$idx") { mutableStateOf(false) }
                                    val stErr = step.status == "error" || step.result?.has("error") == true || step.result?.optBoolean("ok", true) == false
                                    val stRun = step.status == "running"
                                    val durLbl = when {
                                        stRun -> "进行中"
                                        step.durationMs >= 1000L -> String.format(java.util.Locale.US, "%.1fs", step.durationMs / 1000.0)
                                        step.durationMs > 0L -> "${step.durationMs}ms"
                                        else -> ""
                                    }
                                    val recallItems = remember(step.tool, step.result) {
                                        val r = step.result ?: return@remember emptyList<String>()
                                        val arr = r.optJSONArray("facts") ?: r.optJSONArray("results") ?: r.optJSONArray("hits") ?: r.optJSONArray("items")
                                        if (arr == null || arr.length() == 0) emptyList()
                                        else (0 until arr.length().coerceAtMost(8)).mapNotNull { i ->
                                            val o = arr.optJSONObject(i)
                                            if (o != null) {
                                                val head = o.optString("entity").ifBlank { o.optString("title") }.ifBlank { o.optString("path") }
                                                val attr = o.optString("attr")
                                                val body = o.optString("value").ifBlank { o.optString("body") }.ifBlank { o.optString("text") }.ifBlank { o.optString("summary") }
                                                val left = if (attr.isNotBlank()) "$head · $attr" else head
                                                listOf(left, body.take(120)).filter { it.isNotBlank() }.joinToString("：")
                                            } else arr.optString(i).take(120)
                                        }.filter { it.isNotBlank() }
                                    }
                                    val stepLabel = remember(step.tool, step.args, recallItems) {
                                        val base = humanizeToolName(step.tool, step.args)
                                        if (recallItems.isNotEmpty()) "$base · 召回 ${recallItems.size} 条" else base
                                    }
                                    Column(
                                        Modifier.fillMaxWidth()
                                            .clip(RoundedCornerShape(12.dp))
                                            .background(c.fill.copy(alpha = 0.42f))
                                            .padding(horizontal = 10.dp, vertical = 2.dp)
                                    ) {
                                        Row(
                                            Modifier.fillMaxWidth().heightIn(min = 36.dp),
                                            verticalAlignment = Alignment.CenterVertically,
                                            horizontalArrangement = Arrangement.spacedBy(6.dp)
                                        ) {
                                            Row(
                                                Modifier.weight(1f)
                                                    .heightIn(min = 36.dp)
                                                    .clickable(
                                                        interactionSource = remember { MutableInteractionSource() },
                                                        indication = null
                                                    ) {
                                                        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                                        stepOpen = !stepOpen
                                                    }
                                                    .padding(vertical = 4.dp),
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
                                                    stepLabel,
                                                    Modifier.weight(1f),
                                                    color = if (stErr) c.warn else c.ink.copy(alpha = 0.86f),
                                                    fontSize = 12.sp,
                                                    fontWeight = FontWeight.Medium,
                                                    maxLines = 1,
                                                    overflow = TextOverflow.Ellipsis
                                                )
                                                if (durLbl.isNotEmpty()) {
                                                    Text(
                                                        durLbl,
                                                        color = if (stRun) c.accent else c.faint,
                                                        fontSize = 11.sp,
                                                        style = TextStyle(fontFeatureSettings = "tnum")
                                                    )
                                                }
                                            }
                                            if (step.tool == "cloud_desktop" || step.tool == "cloud" || step.tool == "cloud_computer") {
                                                Box(
                                                    Modifier.heightIn(min = 30.dp)
                                                        .clip(RoundedCornerShape(10.dp))
                                                        .background(c.accent.copy(alpha = 0.12f))
                                                        .iosPressScale(0.95f) { model.openCloudDesktop() }
                                                        .padding(horizontal = 9.dp, vertical = 4.dp),
                                                    contentAlignment = Alignment.Center
                                                ) {
                                                    Text("云电脑 ›", color = c.accent, fontSize = 11.5.sp, fontWeight = FontWeight.SemiBold)
                                                }
                                            }
                                            if (step.tool == "subtask" || step.tool == "run_subtask") {
                                                val subTaskId = step.result?.optString("task_id", "").orEmpty()
                                                    .ifBlank { step.args?.optString("task_id", "").orEmpty() }
                                                val liveSub = model.ui.subtasks.firstOrNull { it.taskId == subTaskId && subTaskId.isNotBlank() }
                                                val subTitle = liveSub?.title
                                                    ?: step.result?.optString("title", "").orEmpty()
                                                    .ifBlank { step.args?.optString("title", "").orEmpty() }
                                                    .ifBlank { "子任务" }
                                                val subStatusTag = when (liveSub?.status) {
                                                    "running" -> if (liveSub.stageTotal > 0) "${liveSub.stageIdx}/${liveSub.stageTotal}" else "进行中"
                                                    "completed" -> "已完成"
                                                    "failed" -> "出错"
                                                    "cancelled" -> "已取消"
                                                    else -> "查看"
                                                }
                                                Box(
                                                    Modifier.heightIn(min = 30.dp)
                                                        .clip(RoundedCornerShape(10.dp))
                                                        .background(
                                                            if (liveSub?.status == "failed") c.warn.copy(alpha = 0.14f)
                                                            else c.accent.copy(alpha = 0.12f)
                                                        )
                                                        .iosPressScale(0.95f) { model.openSubtaskPanel(subTaskId) }
                                                        .padding(horizontal = 9.dp, vertical = 4.dp),
                                                    contentAlignment = Alignment.Center
                                                ) {
                                                    Text(
                                                        "${subTitle.take(10)} · $subStatusTag ›",
                                                        color = if (liveSub?.status == "failed") c.warn else c.accent,
                                                        fontSize = 11.5.sp,
                                                        fontWeight = FontWeight.SemiBold,
                                                        maxLines = 1,
                                                        overflow = TextOverflow.Ellipsis
                                                    )
                                                }
                                            }
                                        }
                                        AnimatedVisibility(
                                            visible = stepOpen,
                                            enter = if (effectiveReduce) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(140)),
                                            exit = if (effectiveReduce) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(100))
                                        ) {
                                            val argsStr = step.args?.toString()?.take(1800).orEmpty()
                                            val resStr = step.result?.toString()?.take(2400).orEmpty()
                                            Column(
                                                Modifier.fillMaxWidth().padding(start = 12.dp, top = 2.dp, bottom = 6.dp),
                                                verticalArrangement = Arrangement.spacedBy(4.dp)
                                            ) {
                                                if (recallItems.isNotEmpty()) {
                                                    recallItems.forEachIndexed { rIdx, rLine ->
                                                        Text(
                                                            "${rIdx + 1}. $rLine",
                                                            color = c.muted,
                                                            fontSize = 11.5.sp,
                                                            lineHeight = 16.sp
                                                        )
                                                    }
                                                }
                                                if (argsStr.isNotBlank() && argsStr != "{}") {
                                                    Text("输入：$argsStr", color = c.faint, fontSize = 11.sp, lineHeight = 15.sp)
                                                }
                                                if (recallItems.isEmpty() && resStr.isNotBlank() && resStr != "{}") {
                                                    Text(
                                                        "输出：$resStr",
                                                        color = if (stErr) c.warn else c.faint,
                                                        fontSize = 11.sp,
                                                        lineHeight = 15.sp,
                                                        maxLines = 20,
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

        if (pureProse.isNotBlank()) {
            MarkdownText(
                remember(pureProse) { nativeWikiMarkdown(pureProse) },
                TextStyle(color = if (isError) c.warn else c.ink, fontSize = 16.sp, lineHeight = 23.sp),
                streaming = isLive,
                onOpenUri = model::openLink
            )
        } else if (isLive && !hasProcess) {
            Text("正在思考…", color = c.muted, fontSize = 13.5.sp)
        }
    }
}
