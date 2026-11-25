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
 * [Module] NativePulseScreen
 * [Responsibility]
 * 原生主界面容器與首頁工作台領域模塊：負責頂層頁面路由切換（首頁 Bot 列表、聊天室視口、二級面板、底部彈窗與畫中畫浮層）、
 * 首頁狀態總覽與搜索欄 (NativeHome)，以及聊天室滾動視口與手勢切頁容器 (NativeChatRoomViewport)。
 *
 * [Public API]
 * - NativePulseScreen(model: NativePulseModel)
 * - NativeHome(model: NativePulseModel, modifier: Modifier)
 * - NativeChatRoomViewport(model: NativePulseModel, bot: PulseBot)
 */

@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
@Composable
internal fun NativePulseScreen(model: NativePulseModel) {
    val state = model.ui
    val context = LocalContext.current
    val powerSaveActive = rememberPowerSaveActive(state.powerSave)
    val effectiveReduce = !state.motion || animationsOff(context) || powerSaveActive
    val colors = rememberAnimatedPulsePalette(state.dark, effectiveReduce)
    val progress = remember { Animatable(if (state.chatOpen) 1f else 0f) }
    val panelProgress = remember { Animatable(if (state.activePanel != null) 1f else 0f) }
    var retainedPanel by remember { mutableStateOf(state.activePanel) }
    if (state.activePanel != null) {
        retainedPanel = state.activePanel
    }
    var panelDragBack by remember { mutableStateOf<Float?>(null) }
    var panelFrom by remember { mutableStateOf<Float?>(null) }
    val focus = LocalFocusManager.current
    val haptics = LocalHapticFeedback.current
    val modalScope = rememberCoroutineScope()
    var gestureEnd by remember { mutableStateOf<Float?>(null) }
    var composerHeight by remember { mutableIntStateOf(0) }
    val snackbar = remember { SnackbarHostState() }

    LaunchedEffect(state.toastId) {
        if (state.toast.isNotBlank()) snackbar.showSnackbar(state.toast)
    }

    // 1st-level -> Chat iOS Spring Push/Pop transition
    LaunchedEffect(state.chatOpen, effectiveReduce, model.backSettlement) {
        model.navigationRunning = true
        try {
            (model.backFrom ?: gestureEnd)?.let {
                progress.snapTo(it)
                gestureEnd = null
                model.backFrom = null
            }
            val target = if (state.chatOpen) 1f else 0f
            if (effectiveReduce) progress.snapTo(target)
            else progress.animateTo(
                target,
                spring(dampingRatio = 0.88f, stiffness = 390f)
            )
        } finally {
            model.navigationRunning = false
        }
    }

    // 2nd-level Panel (`bot_edit`, `settings`, `vault`, `cloud_desktop`, `schedules`) iOS Spring Push/Pop transition
    LaunchedEffect(state.activePanel, effectiveReduce) {
        panelFrom?.let {
            panelProgress.snapTo(it)
            panelFrom = null
        }
        val target = if (state.activePanel != null) 1f else 0f
        if (effectiveReduce) {
            panelProgress.snapTo(target)
        } else {
            panelProgress.animateTo(
                target,
                spring(dampingRatio = 0.86f, stiffness = 380f)
            )
        }
        if (state.activePanel == null) {
            retainedPanel = null
        }
    }

    PulseTheme(dark = state.dark, reduceMotion = effectiveReduce) {
        val sysHaptics = LocalHapticFeedback.current
        val effectiveHaptics = if (state.haptics) sysHaptics else QuietHaptics
        val mdColors = remember(colors.fill, colors.line, colors.ink, colors.accent, colors.codeBg, colors.codeHeader, state.dark) {
            PulseColors(colors.fill, colors.line, colors.ink, colors.accent, colors.codeBg, colors.codeHeader, state.dark)
        }
        CompositionLocalProvider(
            LocalPulsePalette provides colors,
            LocalPulseColors provides mdColors,
            LocalHapticFeedback provides effectiveHaptics,
            LocalPowerSaveMode provides powerSaveActive
        ) {
            val phoneState by RootPhoneHarness.changes.collectAsState()
            var dualPaneLayout by remember { mutableStateOf(false) }
            Box(
                Modifier.fillMaxSize()
                    .background(colors.bg)
                    .windowInsetsPadding(WindowInsets.systemBars.union(WindowInsets.ime)),
                contentAlignment = Alignment.TopCenter
            ) {
                BoxWithConstraints(
                    Modifier.fillMaxSize()
                        .clipToBounds()
                ) {
                    val isTabletDualPane = maxWidth >= 600.dp
                    dualPaneLayout = isTabletDualPane
                    val shownPanel = state.activePanel ?: retainedPanel

                    if (isTabletDualPane) {
                        val pFraction = panelDragBack?.let { 1f - it } ?: panelProgress.value
                        val activeBot = model.bot() ?: state.bots.firstOrNull()
                        Row(Modifier.fillMaxSize().background(colors.bg)) {
                            NativeHome(
                                model,
                                Modifier.width(330.dp).fillMaxHeight(),
                                phoneState,
                                selectedBotId = activeBot?.id
                            )
                            Box(Modifier.width(0.5.dp).fillMaxHeight().background(colors.line))
                            BoxWithConstraints(
                                Modifier.weight(1f).fillMaxHeight().background(colors.bg).clipToBounds()
                            ) {
                                val paneWidth = constraints.maxWidth.toFloat()
                                if (activeBot != null) {
                                    val roomBaseMod = Modifier.fillMaxSize().graphicsLayer {
                                        if (pFraction > 0.001f && pFraction < 0.999f) {
                                            translationX = -paneWidth * 0.14f * pFraction
                                        }
                                    }
                                    Box(
                                        roomBaseMod.then(
                                            if (state.activePanel != null) Modifier.clearAndSetSemantics { } else Modifier
                                        )
                                    ) {
                                        NativeChat(
                                            model,
                                            activeBot,
                                            Modifier.fillMaxSize()
                                        ) { composerHeight = it }
                                        if (pFraction > 0.01f) {
                                            Box(
                                                Modifier.fillMaxSize()
                                                    .background(Color.Black.copy(alpha = 0.24f * pFraction))
                                            )
                                        }
                                    }
                                }
                                if (shownPanel != null && pFraction > 0.001f) {
                                    Box(
                                        Modifier.fillMaxSize()
                                            .graphicsLayer {
                                                if (pFraction < 0.999f) {
                                                    translationX = paneWidth * (1f - pFraction)
                                                    shadowElevation = 22f * pFraction
                                                }
                                            }
                                            .background(colors.bg)
                                    ) {
                                        when (shownPanel) {
                                            "settings" -> NativeSettingsPanel(model, Modifier.fillMaxSize())
                                            "bot_edit" -> NativeBotEditorPanel(model, Modifier.fillMaxSize())
                                            "vault" -> NativeVaultPanel(model, Modifier.fillMaxSize())
                                            "cloud_desktop" -> NativeCloudDesktopPanel(model, Modifier.fillMaxSize())
                                            "schedules" -> NativeSchedulesPanel(model, Modifier.fillMaxSize())
                                            "subtask" -> NativeSubtaskPanel(model, Modifier.fillMaxSize())
                                        }
                                    }
                                }
                            }
                        }
                        InAppFloatingVirtualDisplay()
                    } else {
                        BoxWithConstraints(
                            Modifier.fillMaxHeight()
                                .widthIn(max = 700.dp)
                                .fillMaxWidth()
                                .align(Alignment.TopCenter)
                                .clipToBounds()
                                .pointerInput(state.chatOpen, state.menu != null, state.activePanel != null) {
                                    if ((!state.chatOpen && state.activePanel == null) || state.menu != null) return@pointerInput
                                    awaitEachGesture {
                                        val down = awaitFirstDown(requireUnconsumed = false)
                                        if (down.position.x > 28.dp.toPx()) return@awaitEachGesture
                                        var started = false
                                        var dx = 0f
                                        var rejected = false
                                        do {
                                            val event = awaitPointerEvent()
                                            val change = event.changes.find { it.id == down.id } ?: break
                                            dx = (change.position.x - down.position.x).coerceAtLeast(0f)
                                            val dy = abs(change.position.y - down.position.y)
                                            if (!started && (dx > viewConfiguration.touchSlop || dy > viewConfiguration.touchSlop)) {
                                                if (dy >= dx) {
                                                    rejected = true
                                                    break
                                                }
                                                started = true
                                                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                                            }
                                            if (started) {
                                                change.consume()
                                                val frac = (dx / size.width).coerceIn(0f, 1f)
                                                if (state.activePanel != null) {
                                                    panelDragBack = frac
                                                } else {
                                                    model.predictiveBack = frac
                                                }
                                            }
                                            if (!change.pressed) break
                                        } while (true)
                                        if (started && !rejected) {
                                            if (state.activePanel != null) {
                                                val dragged = panelDragBack ?: 0f
                                                panelDragBack = null
                                                if (dx > size.width * 0.26f) {
                                                    focus.clearFocus()
                                                    panelFrom = 1f - dragged
                                                    model.closePanel()
                                                } else {
                                                    modalScope.launch {
                                                        panelProgress.snapTo(1f - dragged)
                                                        if (effectiveReduce) panelProgress.snapTo(1f)
                                                        else panelProgress.animateTo(1f, spring(dampingRatio = 0.85f, stiffness = 420f))
                                                    }
                                                }
                                            } else {
                                                gestureEnd = 1f - (model.predictiveBack ?: 0f)
                                                model.predictiveBack = null
                                                if (dx > size.width * 0.26f) {
                                                    focus.clearFocus()
                                                    model.back()
                                                } else {
                                                    model.backSettlement++
                                                }
                                            }
                                        }
                                    }
                                }
                        ) {
                            val width = constraints.maxWidth.toFloat()
                            val height = constraints.maxHeight.toFloat().coerceAtLeast(1f)
                            val chatFraction = model.predictiveBack?.let { 1f - it } ?: progress.value
                            val pFraction = panelDragBack?.let { 1f - it } ?: panelProgress.value

                            if (pFraction < 0.999f) {
                                val outerCorner = (26f * pFraction).dp
                                val outerLayerMod = if (pFraction > 0.001f) {
                                    Modifier.fillMaxSize()
                                        .graphicsLayer {
                                            val sc = 1f - 0.06f * pFraction
                                            scaleX = sc
                                            scaleY = sc
                                            translationY = -height * 0.015f * pFraction
                                        }
                                        .clip(RoundedCornerShape(outerCorner))
                                } else {
                                    Modifier.fillMaxSize()
                                }
                                Box(outerLayerMod) {
                                    if (chatFraction < 0.999f) {
                                        val homeLayerMod = if (chatFraction > 0.001f) {
                                            Modifier.fillMaxSize().graphicsLayer {
                                                val hSc = 1f - 0.16f * chatFraction
                                                scaleX = hSc
                                                scaleY = hSc
                                                translationY = -height * 0.18f * chatFraction
                                                alpha = (1f - 1.15f * chatFraction).coerceIn(0f, 1f)
                                            }
                                        } else {
                                            Modifier.fillMaxSize()
                                        }
                                        NativeHome(
                                            model,
                                            homeLayerMod.then(if (state.chatOpen || state.activePanel != null) Modifier.clearAndSetSemantics { } else Modifier),
                                            phoneState
                                        )
                                    }
                                    val bot = model.bot()
                                    if (bot != null && (chatFraction > 0.001f || state.chatOpen)) {
                                        val chatCorner = (30f * (1f - chatFraction)).dp
                                        val chatLayerMod = if (chatFraction < 0.999f) {
                                            Modifier.fillMaxSize()
                                                .graphicsLayer {
                                                    val cSc = 0.92f + 0.08f * chatFraction
                                                    scaleX = cSc
                                                    scaleY = cSc
                                                    translationY = height * 0.06f * (1f - chatFraction)
                                                    alpha = (chatFraction * 1.45f).coerceIn(0f, 1f)
                                                }
                                                .clip(RoundedCornerShape(chatCorner))
                                        } else {
                                            Modifier.fillMaxSize()
                                        }
                                        NativeChat(
                                            model,
                                            bot,
                                            chatLayerMod.then(if (!state.chatOpen || state.activePanel != null) Modifier.clearAndSetSemantics { } else Modifier)
                                        ) { composerHeight = it }
                                    }
                                    if (pFraction > 0.01f) {
                                        Box(
                                            Modifier.fillMaxSize()
                                                .background(Color.Black.copy(alpha = 0.30f * pFraction))
                                        )
                                    }
                                }
                            }

                            if (shownPanel != null && pFraction > 0.001f) {
                                val panelCorner = (28f * (1f - pFraction)).dp
                                val panelLayerMod = if (pFraction < 0.999f) {
                                    Modifier.fillMaxSize()
                                        .graphicsLayer {
                                            val pSc = 0.96f + 0.04f * pFraction
                                            scaleX = pSc
                                            scaleY = pSc
                                            translationX = width * 0.85f * (1f - pFraction)
                                            translationY = height * 0.04f * (1f - pFraction)
                                            shadowElevation = 26f * pFraction
                                        }
                                        .clip(RoundedCornerShape(panelCorner))
                                } else {
                                    Modifier.fillMaxSize()
                                }
                                Box(panelLayerMod) {
                                    when (shownPanel) {
                                        "settings" -> NativeSettingsPanel(model, Modifier.fillMaxSize())
                                        "bot_edit" -> NativeBotEditorPanel(model, Modifier.fillMaxSize())
                                        "vault" -> NativeVaultPanel(model, Modifier.fillMaxSize())
                                        "cloud_desktop" -> NativeCloudDesktopPanel(model, Modifier.fillMaxSize())
                                        "schedules" -> NativeSchedulesPanel(model, Modifier.fillMaxSize())
                                        "subtask" -> NativeSubtaskPanel(model, Modifier.fillMaxSize())
                                    }
                                }
                            }

                            InAppFloatingVirtualDisplay()
                        }
                    }
                }
                WhaleChanApiGate(model)
                if (state.healthDialogOpen) {
                    ConnectionHealthDialog(model) { model.closeHealthDialog() }
                }
                val toastBottom = if ((state.chatOpen || dualPaneLayout) && state.activePanel == null && composerHeight > 0) {
                    with(LocalDensity.current) { composerHeight.toDp() } + 12.dp
                } else 20.dp
                SnackbarHost(snackbar, Modifier.align(Alignment.BottomCenter).padding(start = 20.dp, end = 20.dp, bottom = toastBottom))
            }
            state.menu?.let { menu ->
                if (effectiveReduce) {
                    Popup(alignment = Alignment.BottomCenter, onDismissRequest = model::dismissMenu, properties = PopupProperties(focusable = true, clippingEnabled = false)) {
                        Box(
                            Modifier.fillMaxSize()
                                .background(Color.Black.copy(alpha = .32f))
                                .clickable(
                                    interactionSource = remember { MutableInteractionSource() },
                                    indication = null,
                                    onClick = model::dismissMenu
                                ),
                            contentAlignment = Alignment.BottomCenter
                        ) {
                            Surface(
                                Modifier.fillMaxWidth()
                                    .heightIn(max = (LocalConfiguration.current.screenHeightDp * .85f).dp)
                                    .clickable(
                                        interactionSource = remember { MutableInteractionSource() },
                                        indication = null,
                                        onClick = {}
                                    )
                                    .windowInsetsPadding(WindowInsets.navigationBars.union(WindowInsets.ime)),
                                shape = RoundedCornerShape(topStart = 28.dp, topEnd = 28.dp), color = colors.surface
                            ) {
                                NativeMenuContent(menu, false, model::dismissMenu) { item -> model.dismissMenu(); item.run() }
                            }
                        }
                    }
                } else {
                    val modalState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
                    var closing by remember(menu) { mutableStateOf(false) }
                    val closeThen: (() -> Unit) -> Unit = { next ->
                        if (!closing) {
                            closing = true
                            modalScope.launch {
                                if (!effectiveReduce) modalState.hide()
                                model.dismissMenu()
                                next()
                            }
                        }
                    }
                    ModalBottomSheet(
                        onDismissRequest = model::dismissMenu,
                        sheetState = modalState,
                        dragHandle = null,
                        containerColor = colors.surface,
                        contentColor = colors.ink,
                        shape = RoundedCornerShape(topStart = 28.dp, topEnd = 28.dp),
                        tonalElevation = 0.dp
                    ) {
                        NativeMenuContent(menu, closing, { closeThen {} }) { item -> closeThen(item.run) }
                    }
                }
            }
        }
    }
}

@Composable
internal fun NativeChatRoomViewport(
    model: NativePulseModel,
    bot: PulseBot,
    onScrolledChanged: (Boolean) -> Unit = {}
) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val messages = remember(s.messages, bot.id) { s.messages.filter { it.botId == bot.id && it.kind != "compact_summary" } }
    val list = rememberLazyListState()
    val chatScope = rememberCoroutineScope()
    var initialScrollDoneForBot by remember(bot.id) { mutableStateOf(false) }
    var lastMsgCount by remember(bot.id) { mutableIntStateOf(0) }
    var unseenNewCount by remember(bot.id) { mutableIntStateOf(0) }
    val chatScrolled by remember {
        derivedStateOf { list.firstVisibleItemIndex > 0 || list.firstVisibleItemScrollOffset > 8 }
    }
    LaunchedEffect(chatScrolled, bot.id) {
        onScrolledChanged(chatScrolled && (messages.isNotEmpty() || s.streamBotId == bot.id || s.workingBot == bot.id))
    }

    val lastMsg = messages.lastOrNull()
    val duplicateWithTail = lastMsg != null && lastMsg.role == "assistant" && s.streamText.isNotBlank() &&
        lastMsg.text.trim() == s.streamText.trim() && s.workingBot != bot.id && s.streamBotId != bot.id && s.streamTool.isBlank() && s.streamTrace.isEmpty()
    val isLiveHere = !duplicateWithTail && (s.streamBotId == bot.id || s.workingBot == bot.id || s.busyBot == bot.id || (s.streamBotId == bot.id && s.streamTrace.isNotEmpty()))

    val nearTail by remember {
        derivedStateOf {
            val info = list.layoutInfo
            val total = info.totalItemsCount
            if (total <= 1) true
            else {
                val lastVisible = info.visibleItemsInfo.lastOrNull()?.index ?: 0
                lastVisible >= total - 4
            }
        }
    }

    LaunchedEffect(nearTail) {
        if (nearTail) unseenNewCount = 0
    }

    val lastChoiceCount = messages.lastOrNull()?.choices?.size ?: 0
    LaunchedEffect(messages.size, messages.lastOrNull()?.id, bot.id, isLiveHere, lastChoiceCount, s.streamChoices.size, s.consumedChoiceIds.size) {
        val anchorIdx = (messages.size + (if (isLiveHere) 1 else 0)).coerceAtLeast(0)
        val addedCount = (messages.size - lastMsgCount).coerceAtLeast(0)
        val userSentNew = messages.lastOrNull()?.role == "user" && addedCount > 0
        if (!initialScrollDoneForBot) {
            if (messages.isNotEmpty() || isLiveHere) {
                runCatching { list.scrollToItem(anchorIdx) }
                kotlinx.coroutines.yield()
                runCatching { list.scrollToItem(anchorIdx) }
                initialScrollDoneForBot = true
                unseenNewCount = 0
            }
        } else if (userSentNew || (nearTail && !list.isScrollInProgress)) {
            runCatching { list.scrollToItem(anchorIdx) }
            unseenNewCount = 0
        } else if (addedCount > 0 && !nearTail) {
            unseenNewCount = (unseenNewCount + addedCount).coerceAtMost(99)
        }
        lastMsgCount = messages.size
    }

    LaunchedEffect(s.streamText.length / 80, s.streamTimeline.size, s.streamTrace.size) {
        if (isLiveHere && nearTail && !list.isScrollInProgress) {
            val info = list.layoutInfo
            val lastVis = info.visibleItemsInfo.lastOrNull()
            val alreadyAtBottom = lastVis != null &&
                lastVis.index >= info.totalItemsCount - 1 &&
                (lastVis.offset + lastVis.size) <= info.viewportEndOffset + 12
            if (!alreadyAtBottom) {
                val anchorIdx = (messages.size + 1).coerceAtLeast(0)
                runCatching { list.scrollToItem(anchorIdx) }
            }
        }
    }

    Box(Modifier.fillMaxSize()) {
        if (messages.isEmpty() && !isLiveHere) {
            // Telegram Bot Welcome Card
            Column(
                Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 24.dp, vertical = 28.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center
            ) {
                Column(
                    Modifier.widthIn(max = 560.dp)
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(22.dp))
                        .background(c.surface)
                        .padding(horizontal = 20.dp, vertical = 24.dp),
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    PulseAvatar(bot, s, diameter = 72.dp, showStatusDot = true, ringColor = c.surface)
                    Text(
                        bot.name,
                        Modifier.padding(top = 14.dp),
                        color = c.ink,
                        fontWeight = FontWeight.Bold,
                        fontSize = 21.sp,
                        letterSpacing = (-0.4).sp
                    )
                    Text(
                        bot.description,
                        Modifier.padding(top = 6.dp, bottom = 20.dp),
                        color = c.muted,
                        fontSize = 14.5.sp,
                        textAlign = TextAlign.Center,
                        lineHeight = 20.sp
                    )
                    val starters = remember(bot.id, bot.name, bot.description) {
                        val hint = "${bot.id} ${bot.name} ${bot.description}".lowercase()
                        when {
                            hint.contains("research") || hint.contains("探") || hint.contains("调研") -> listOf(
                                "系统调研某个技术方案与开源竞品" to "请帮我深度调研下面这个技术方向，对比核心方案的优缺点与选型建议：",
                                "提炼长文档或网页里的核心观点与数据" to "请帮我精读下面这段内容，提炼出核心结论、关键数据与潜在问题：",
                                "对比两个设计架构或算法的取舍边界" to "请对比下面两种方案在性能、复杂度与长期维护上的核心差异："
                            )
                            hint.contains("review") || hint.contains("审") || hint.contains("校") -> listOf(
                                "严格审查近期代码改动的边界与并发隐患" to "请对当前实现做一次代码审查，重点排查并发竞争、空值和边界情况。",
                                "检查系统架构里最容易踩坑的地方" to "请审视这份设计方案，指出最危险的脆弱环节并给出简化建议。",
                                "定位一次莫名其妙的测试或运行报错" to "请结合上下文帮我按证据链定位下面这个异常的真实根因："
                            )
                            hint.contains("companion") || hint.contains("云汐") || hint.contains("whale") -> listOf(
                                "润色一段表达或整理散落的想法成文稿" to "请帮我把下面这些零碎想法润色成自然、有条理、不拖沓的文字：",
                                "规划今天接下来轻松可落地的待办节奏" to "结合我现在的进度，帮我把今天剩下的事情排一个轻重缓急的节奏。",
                                "看看跨端记忆金库里最近记下的长期偏好" to "/pins"
                            )
                            else -> listOf(
                                "查看中枢健康、在线节点与上下文占用" to "/status",
                                "排查一段报错或问题并给出可直接验证的方案" to "请分析下面这个问题或日志报错，定位真正原因并给出最小修复方案：",
                                "在手机虚拟屏或云电脑工作区协同操作" to "请在工作区里查看当前屏幕状态，并继续帮我推进手头这项任务："
                            )
                        }
                    }
                    Column(
                        Modifier.fillMaxWidth()
                            .clip(RoundedCornerShape(14.dp))
                            .background(c.fill.copy(alpha = 0.65f))
                    ) {
                        starters.forEachIndexed { idx, (label, prompt) ->
                            if (idx > 0) {
                                HorizontalDivider(Modifier.padding(start = 14.dp), color = c.line.copy(alpha = 0.6f), thickness = 0.5.dp)
                            }
                            Row(
                                Modifier.fillMaxWidth()
                                    .iosPressScale(0.98f) { model.sendQuickPrompt(prompt) }
                                    .padding(horizontal = 14.dp, vertical = 13.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Text(label, Modifier.weight(1f), color = c.ink, fontSize = 14.5.sp)
                                PulseIcon("next", c.faint, Modifier.size(14.dp))
                            }
                        }
                    }
                }
            }
        } else {
            LazyColumn(
                Modifier.fillMaxSize().semantics { contentDescription = "对话消息" },
                state = list,
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 12.dp)
            ) {
                items(
                    count = messages.size,
                    key = { idx -> messages[idx].id },
                    contentType = { idx -> "${messages[idx].role}_${messages[idx].kind}" }
                ) { idx ->
                    val message = messages[idx]
                    val prev = if (idx > 0) messages[idx - 1] else null
                    val next = if (idx < messages.lastIndex) messages[idx + 1] else null

                    val prevCreated = prev?.created ?: 0.0
                    val showTimeHeader = idx == 0 || (message.created - prevCreated) >= 300.0

                    val sameSenderAsPrev = prev != null &&
                        prev.role == message.role &&
                        prev.fromBotId == message.fromBotId &&
                        prev.kind == message.kind &&
                        (message.created - prev.created) < 180.0 &&
                        !showTimeHeader

                    val sameSenderAsNext = next != null &&
                        next.role == message.role &&
                        next.fromBotId == message.fromBotId &&
                        next.kind == message.kind &&
                        (next.created - message.created) < 180.0

                    val topGap = when {
                        idx == 0 -> 2.dp
                        sameSenderAsPrev -> 3.dp
                        else -> 11.dp
                    }

                    val chatReduceMotion = rememberReducedMotion()
                    Column(
                        Modifier.fillMaxWidth()
                            .then(
                                if (chatReduceMotion) Modifier
                                else Modifier.animateItem(
                                    fadeInSpec = tween(150),
                                    fadeOutSpec = tween(100),
                                    placementSpec = iosPlacementSpring(false)
                                )
                            )
                            .padding(top = topGap)
                    ) {
                        // Telegram Floating Pill Date / Time Header
                        if (showTimeHeader && message.created > 0) {
                            Box(
                                Modifier.fillMaxWidth().padding(top = 4.dp, bottom = 10.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Box(
                                    Modifier.clip(CircleShape)
                                        .background(c.surface.copy(alpha = 0.82f))
                                        .padding(horizontal = 11.dp, vertical = 3.5.dp)
                                ) {
                                    Text(
                                        model.timeSeparator(message.created),
                                        color = c.muted,
                                        fontSize = 11.5.sp,
                                        fontWeight = FontWeight.Medium,
                                        style = TextStyle(fontFeatureSettings = "tnum")
                                    )
                                }
                            }
                        }
                        Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.TopCenter) {
                            Box(Modifier.widthIn(max = 760.dp).fillMaxWidth()) {
                                NativeMessage(
                                    model = model,
                                    bot = bot,
                                    message = message,
                                    groupedWithPrev = sameSenderAsPrev,
                                    groupedWithNext = sameSenderAsNext,
                                    isLatestMessage = (idx == messages.lastIndex && !isLiveHere)
                                )
                            }
                        }
                    }
                }
                if (isLiveHere) {
                    item(key = "live_stream", contentType = "live_stream") {
                        Box(Modifier.fillMaxWidth().padding(top = 10.dp), contentAlignment = Alignment.TopCenter) {
                            Box(Modifier.widthIn(max = 760.dp).fillMaxWidth()) {
                                NativeLiveStreamBubble(model, bot)
                            }
                        }
                    }
                }
                item(key = "bottom_anchor", contentType = "anchor") {
                    Spacer(Modifier.height(2.dp).fillMaxWidth())
                }
            }

            // Telegram Circular Scroll-to-Bottom Button (Animated Scale + Fade, 48dp touch target + Unread badge)
            val fabReduce = rememberReducedMotion()
            AnimatedVisibility(
                visible = !nearTail && (messages.size > 3 || unseenNewCount > 0),
                modifier = Modifier.align(Alignment.BottomEnd).padding(end = 14.dp, bottom = 14.dp),
                enter = if (fabReduce) fadeIn(tween(110)) else scaleIn(iosInteractiveSpring(), initialScale = 0.92f) + fadeIn(tween(150)),
                exit = if (fabReduce) fadeOut(tween(90)) else scaleOut(iosGentleSpring(), targetScale = 0.92f) + fadeOut(tween(110))
            ) {
                Box(contentAlignment = Alignment.TopCenter) {
                    Box(
                        Modifier.padding(top = if (unseenNewCount > 0) 8.dp else 0.dp)
                            .size(48.dp)
                            .clip(CircleShape)
                            .background(c.surface.copy(alpha = 0.96f))
                            .border(0.6.dp, c.line.copy(alpha = 0.75f), CircleShape)
                            .iosPressScale(0.92f) {
                                unseenNewCount = 0
                                chatScope.launch {
                                    val anchorIdx = (messages.size + (if (isLiveHere) 1 else 0)).coerceAtLeast(0)
                                    runCatching { list.animateScrollToItem(anchorIdx) }
                                }
                            }
                            .semantics { contentDescription = "回到底部" },
                        contentAlignment = Alignment.Center
                    ) {
                        PulseIcon("chevron_down", c.accent, Modifier.size(22.dp))
                    }
                    if (unseenNewCount > 0) {
                        Box(
                            Modifier.defaultMinSize(minWidth = 20.dp, minHeight = 18.dp)
                                .clip(CircleShape)
                                .background(c.accent)
                                .padding(horizontal = 6.dp, vertical = 1.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                unseenNewCount.toString(),
                                color = Color.White,
                                fontSize = 10.5.sp,
                                fontWeight = FontWeight.Bold,
                                style = TextStyle(fontFeatureSettings = "tnum")
                            )
                        }
                    }
                }
            }
        }
    }
}
