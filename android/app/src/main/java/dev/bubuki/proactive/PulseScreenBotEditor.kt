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


@Composable
internal fun NativeBotEditorPanel(model: NativePulseModel, modifier: Modifier) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val focus = LocalFocusManager.current
    val reduceMotion = rememberReducedMotion()
    val isNew = s.editingBotId == null
    val targetBotId = s.editingBotId ?: s.botId
    val targetBot = model.bot(targetBotId)
    val (curCtxTok, threshCtxTok, ctxMsgCnt) = model.contextStatsFor(targetBotId)
    val (cachedCtxTok, cacheHitPct) = model.contextCacheFor(targetBotId)
    val (perfTtft, perfTps, perfCompTok) = model.contextPerfFor(targetBotId)
    val rawCtxRatio = (curCtxTok.toFloat() / threshCtxTok.coerceAtLeast(1)).coerceIn(0f, 1f)
    val animatedCtxRatio by animateFloatAsState(
        targetValue = rawCtxRatio.coerceAtLeast(0.03f),
        animationSpec = if (reduceMotion) snap() else spring(dampingRatio = 0.80f, stiffness = 320f),
        label = "ctx_bar")
    val ctxPct = (rawCtxRatio * 100).toInt()
    val rawCacheRatio = if (curCtxTok > 0) (cachedCtxTok.toFloat() / curCtxTok.toFloat()).coerceIn(0f, 1f) else 0f
    val animatedCacheRatio by animateFloatAsState(
        targetValue = if (cachedCtxTok > 0) rawCacheRatio.coerceAtLeast(0.04f) else 0f,
        animationSpec = if (reduceMotion) snap() else spring(dampingRatio = 0.80f, stiffness = 320f),
        label = "cache_bar")
    Column(modifier.background(c.bg)) {
        // Telegram Contact Info Top Bar
        Surface(color = c.surface.copy(alpha = 0.96f), tonalElevation = 0.dp) {
            Column {
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Row(
                        Modifier.heightIn(min = 40.dp).clip(RoundedCornerShape(10.dp))
                            .iosPressScale(0.95f) { focus.clearFocus(); model.closePanel() }
                            .padding(horizontal = 8.dp, vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        PulseIcon("back", c.accent, Modifier.size(21.dp))
                        Text("返回", color = c.accent, fontSize = 16.sp)
                    }
                    Text(
                        if (isNew) "新建助手" else "个人主页",
                        Modifier.weight(1f),
                        color = c.ink,
                        fontSize = 17.5.sp,
                        fontWeight = FontWeight.SemiBold,
                        textAlign = TextAlign.Center)
                    Box(
                        Modifier.heightIn(min = 40.dp).clip(RoundedCornerShape(14.dp))
                            .background(c.accent)
                            .iosPressScale(0.95f, enabled = !s.savingBot) { focus.clearFocus(); model.saveBotEditor() }
                            .padding(horizontal = 15.dp, vertical = 7.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            if (s.savingBot) "保存中…" else "保存",
                            color = Color.White,
                            fontSize = 13.5.sp,
                            fontWeight = FontWeight.SemiBold)
                    }
                }
                HorizontalDivider(color = c.line.copy(alpha = 0.65f), thickness = 0.5.dp)
            }
        }
        Column(
            Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 14.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp)
        ) {
            if (!isNew && targetBot != null) {
                val previewBot = remember(targetBot, s.editBotName, s.editBotShape, s.editBotColor) {
                    targetBot.copy(
                        name = s.editBotName.ifBlank { targetBot.name },
                        shape = s.editBotShape.ifBlank { targetBot.shape },
                        color = s.editBotColor.ifBlank { targetBot.color })
                }
                // 0. Telegram Contact Info Centered Hero Card + 3 Action Tiles
                Column(
                    Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(c.surface).padding(vertical = 20.dp, horizontal = 16.dp),
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    PulseAvatar(previewBot, s, diameter = 78.dp, showStatusDot = true, ringColor = c.surface)
                    Text(
                        previewBot.name,
                        Modifier.padding(top = 12.dp),
                        color = c.ink,
                        fontSize = 22.sp,
                        fontWeight = FontWeight.Bold,
                        letterSpacing = (-0.4).sp)
                    Text(
                        targetBot.model.ifBlank { s.defaultModel.ifBlank { "跟随全局默认模型" } } +
                            (if (s.primary == targetBot.id) " · 主控助手" else ""),
                        Modifier.padding(top = 3.dp),
                        color = c.accent,
                        fontSize = 13.sp,
                        fontWeight = FontWeight.Medium)
                    Text(
                        targetBot.description,
                        Modifier.padding(top = 6.dp, bottom = 16.dp),
                        color = c.muted,
                        fontSize = 13.5.sp,
                        textAlign = TextAlign.Center,
                        lineHeight = 19.sp)
                    // Telegram 3-Tile Quick Action Row
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        TelegramProfileActionTile("chat", "发消息", Modifier.weight(1f)) {
                            model.closePanel()
                            model.open(targetBot.id)
                        }
                        TelegramProfileActionTile("spark", "压缩上下文", Modifier.weight(1f)) {
                            model.compactBotContext(targetBot.id)
                        }
                        TelegramProfileActionTile(
                            "pin",
                            if (targetBot.pinned) "取消置顶" else "置顶对话",
                            Modifier.weight(1f)
                        ) {
                            model.togglePinBot(targetBot.id)
                        }
                    }
                }
                // 1. Per-Bot Context & Prefix Cache Hit Card (Animated Spring Progress Bars)
                Column(
                    Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(c.surface).padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "专属上下文与前缀缓存",
                            Modifier.weight(1f),
                            color = c.ink,
                            fontSize = 15.5.sp,
                            fontWeight = FontWeight.SemiBold)
                        Text(
                            "$ctxMsgCnt 条消息",
                            color = c.muted,
                            fontSize = 12.sp,
                            style = TextStyle(fontFeatureSettings = "tnum"))
                    }
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "上下文占用：${model.formatTokenCount(curCtxTok)} / ${model.formatTokenCount(threshCtxTok)}",
                            Modifier.weight(1f),
                            color = if (rawCtxRatio >= 0.80f) c.warn else c.ink,
                            fontSize = 13.sp,
                            fontWeight = FontWeight.Medium,
                            style = TextStyle(fontFeatureSettings = "tnum"))
                        Text(
                            "$ctxPct%",
                            color = if (rawCtxRatio >= 0.80f) c.warn else c.accent,
                            fontSize = 13.sp,
                            fontWeight = FontWeight.Bold,
                            style = TextStyle(fontFeatureSettings = "tnum"))
                    }
                    Box(Modifier.fillMaxWidth().height(8.dp).clip(RoundedCornerShape(4.dp)).background(c.fill)) {
                        Box(
                            Modifier.fillMaxWidth(animatedCtxRatio).fillMaxHeight()
                                .clip(RoundedCornerShape(4.dp))
                                .background(
                                    when {
                                        rawCtxRatio >= 0.85f -> c.warn
                                        rawCtxRatio >= 0.65f -> c.amber
                                        else -> c.accent
                                    })
                        )
                    }
                    Row(Modifier.fillMaxWidth().padding(top = 2.dp), verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            "前缀缓存命中：${model.formatTokenCount(cachedCtxTok)} Tokens",
                            Modifier.weight(1f),
                            color = if (cachedCtxTok > 0) c.green else c.muted,
                            fontSize = 13.sp,
                            fontWeight = FontWeight.Medium,
                            style = TextStyle(fontFeatureSettings = "tnum"))
                        Text(
                            "命中率 $cacheHitPct%",
                            color = if (cachedCtxTok > 0) c.green else c.faint,
                            fontSize = 13.sp,
                            fontWeight = FontWeight.Bold,
                            style = TextStyle(fontFeatureSettings = "tnum"))
                    }
                    Box(Modifier.fillMaxWidth().height(8.dp).clip(RoundedCornerShape(4.dp)).background(c.fill)) {
                        if (cachedCtxTok > 0) {
                            Box(
                                Modifier.fillMaxWidth(animatedCacheRatio).fillMaxHeight()
                                    .clip(RoundedCornerShape(4.dp))
                                    .background(c.green))
                        }
                    }
                    Row(
                        Modifier.fillMaxWidth().padding(top = 4.dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Column(
                            Modifier.weight(1f).clip(RoundedCornerShape(10.dp)).background(c.fill.copy(alpha = 0.72f)).padding(horizontal = 10.dp, vertical = 7.dp)
                        ) {
                            Text("首字延迟 (TTFT)", color = c.muted, fontSize = 11.sp)
                            Text(
                                if (perfTtft > 0) "${perfTtft} ms" else "待测",
                                color = if (perfTtft in 1..1500) c.green else c.ink,
                                fontSize = 13.5.sp,
                                fontWeight = FontWeight.Bold,
                                style = TextStyle(fontFeatureSettings = "tnum"))
                        }
                        Column(
                            Modifier.weight(1f).clip(RoundedCornerShape(10.dp)).background(c.fill.copy(alpha = 0.72f)).padding(horizontal = 10.dp, vertical = 7.dp)
                        ) {
                            Text("生成速度 (TPS)", color = c.muted, fontSize = 11.sp)
                            Text(
                                if (perfTps > 0.0) String.format(java.util.Locale.US, "%.1f tok/s", perfTps) else "待测",
                                color = if (perfTps >= 20.0) c.accent else c.ink,
                                fontSize = 13.5.sp,
                                fontWeight = FontWeight.Bold,
                                style = TextStyle(fontFeatureSettings = "tnum"))
                        }
                        Column(
                            Modifier.weight(1f).clip(RoundedCornerShape(10.dp)).background(c.fill.copy(alpha = 0.72f)).padding(horizontal = 10.dp, vertical = 7.dp)
                        ) {
                            Text("最近输出", color = c.muted, fontSize = 11.sp)
                            Text(
                                if (perfCompTok > 0) "${model.formatTokenCount(perfCompTok)} tok" else "—",
                                color = c.ink,
                                fontSize = 13.5.sp,
                                fontWeight = FontWeight.Bold,
                                style = TextStyle(fontFeatureSettings = "tnum"))
                        }
                    }
                    Row(
                        Modifier.fillMaxWidth().padding(top = 2.dp),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text("上下文窗口上限（滑块或点按实时生效）：", color = c.muted, fontSize = 12.sp)
                        Text(
                            "${model.formatTokenCount(threshCtxTok)} Tokens",
                            color = c.accent,
                            fontSize = 12.5.sp,
                            fontWeight = FontWeight.Bold,
                            style = TextStyle(fontFeatureSettings = "tnum")
                        )
                    }
                    Slider(
                        value = threshCtxTok.coerceIn(16000, 200000).toFloat(),
                        onValueChange = { v ->
                            val stepped = ((v / 4000f).roundToInt() * 4000).coerceIn(16000, 200000)
                            model.updateSettingsField(autoCompactTokens = stepped.toString())
                        },
                        onValueChangeFinished = {
                            val finalTok = (s.autoCompactTokens.toIntOrNull() ?: 200000).coerceIn(8000, 1000000)
                            model.applyContextWindowTokens(finalTok, targetBotId)
                        },
                        valueRange = 16000f..200000f,
                        colors = SliderDefaults.colors(
                            thumbColor = c.accent,
                            activeTrackColor = c.accent,
                            inactiveTrackColor = c.fill
                        )
                    )
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                        listOf(16000 to "16K", 32000 to "32K", 64000 to "64K", 100000 to "100K", 120000 to "120K", 200000 to "200K").forEach { (tokInt, label) ->
                            val selected = threshCtxTok == tokInt
                            Text(
                                label,
                                Modifier.weight(1f)
                                    .clip(RoundedCornerShape(9.dp))
                                    .background(if (selected) c.accent else c.fill)
                                    .iosPressScale(0.95f) {
                                        model.applyContextWindowTokens(tokInt, targetBotId)
                                    }
                                    .padding(vertical = 8.dp),
                                color = if (selected) Color.White else c.ink,
                                fontSize = 11.5.sp,
                                fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
                                textAlign = TextAlign.Center,
                                style = TextStyle(fontFeatureSettings = "tnum"))
                        }
                    }
                    Box(
                        Modifier.fillMaxWidth()
                            .padding(top = 2.dp)
                            .clip(RoundedCornerShape(12.dp))
                            .background(c.accent.copy(alpha = 0.14f))
                            .iosPressScale(0.97f, enabled = !s.compactingContext) {
                                focus.clearFocus()
                                model.compactBotContext(targetBotId)
                            }
                            .padding(vertical = 11.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            if (s.compactingContext) "正在浓缩归档对话…" else "立即压缩 ${targetBot.name} 的上下文",
                            color = c.accent,
                            fontSize = 13.5.sp,
                            fontWeight = FontWeight.SemiBold)
                    }
                }
                // 2. Pluggable Sticker Pack Card with Live Thumbnail Strip
                if (s.stickerPacks.isNotEmpty()) {
                    Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(c.surface).padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(10.dp)
                    ) {
                        Text("可选扩展与表情包插件", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
                        s.stickerPacks.forEach { pack ->
                            val pid = pack.optString("id", "fubuki")
                            val pname = pack.optString("name", pid)
                            val penabled = pack.optBoolean("enabled", true)
                            val pcnt = pack.optInt("count", 0)
                            val stickersArr = pack.optJSONArray("stickers") ?: JSONArray()
                            val stickers = remember(stickersArr) { (0 until stickersArr.length()).map { stickersArr.getJSONObject(it) } }
                            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                                Column(Modifier.weight(1f)) {
                                    Text(pname, color = c.ink, fontSize = 14.5.sp, fontWeight = FontWeight.Medium)
                                    Text("共 $pcnt 枚贴纸 · 开启后聊天可收发表情包", color = c.muted, fontSize = 11.5.sp, style = TextStyle(fontFeatureSettings = "tnum"))
                                }
                                Switch(checked = penabled, onCheckedChange = { model.toggleStickerPack(pid, it) })
                            }
                            if (penabled && stickers.isNotEmpty()) {
                                LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(top = 4.dp)) {
                                    items(stickers.take(12)) { st ->
                                        val url = st.optString("url", "/stickers/$pid/${st.optString("file")}")
                                        Box(
                                            Modifier.clip(RoundedCornerShape(10.dp))
                                                .background(c.fill.copy(alpha = 0.65f))
                                                .padding(5.dp)
                                        ) {
                                            PulseStickerThumb(model, url, 48.dp)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            // 3. Bot Configuration & System Prompt Section (Minimal, ZERO Preset Templates)
            Column(
                Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(c.surface).padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Text("全端共用人设（手机 / 平板 / 电脑同步）", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
                Text("头像造型、主题色、名称、简介与系统指令在所有客户端保持一致", color = c.muted, fontSize = 11.5.sp)
                Text("主题色（色彩特征）", color = c.muted, fontSize = 12.5.sp, fontWeight = FontWeight.Medium)
                BotColorSwatchRow(
                    selectedColorHex = s.editBotColor,
                    onSelectColor = { model.updateBotEditField(color = it) })
                Text("几何特征（头像造型）", color = c.muted, fontSize = 12.5.sp, fontWeight = FontWeight.Medium)
                BotShapeSelectorGrid(
                    selectedShape = s.editBotShape,
                    previewColorHex = s.editBotColor,
                    onSelectShape = { model.updateBotEditField(shape = it) })
                PulseInputField("名称（全端共用）", s.editBotName, "输入助手名称") { model.updateBotEditField(name = it) }
                PulseInputField("简介（全端共用）", s.editBotDesc, "显示在对话列表中") { model.updateBotEditField(desc = it) }
                PulseInputField(
                    "系统指令 / 角色设定（全端共用 System Prompt）",
                    s.editBotInstructions,
                    "告诉这位 Bot 它的职责、说话风格与协作要求…",
                    singleLine = false
                ) {
                    model.updateBotEditField(instructions = it)
                }
                HorizontalDivider(color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
                Text("当前客户端独立设置（仅本设备生效）", color = c.ink, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
                Text("不同设备（如手机打开、平板关闭）可各自独立开关好奇心、通知与专属模型", color = c.muted, fontSize = 11.5.sp)
                PulseInputField(
                    "本端专属模型（留空则使用全局默认：${s.defaultModel.ifBlank { "未设置" }}）",
                    s.editBotModel,
                    "留空跟随全局默认，或填入/点选指定模型"
                ) { model.updateBotEditField(modelName = it) }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(
                        if (s.fetchingModels) "正在拉取 /models…" else "刷新 /models 列表",
                        Modifier.clip(RoundedCornerShape(10.dp))
                            .background(c.fill)
                            .iosPressScale(0.95f, enabled = !s.fetchingModels) { model.fetchModels(true) }
                            .padding(horizontal = 12.dp, vertical = 8.dp),
                        color = c.accent,
                        fontSize = 12.sp,
                        fontWeight = FontWeight.Medium)
                    if (s.editBotModel.isNotBlank()) {
                        Text(
                            "清空（恢复跟随默认）",
                            Modifier.clip(RoundedCornerShape(10.dp))
                                .background(c.fill)
                                .iosPressScale(0.95f) { model.updateBotEditField(modelName = "") }
                                .padding(horizontal = 12.dp, vertical = 8.dp),
                            color = c.muted,
                            fontSize = 12.sp)
                    }
                }
                val botModelEntries = remember(s.modelEntries, s.availableModels, s.channels) {
                    s.modelEntries.ifEmpty { PulseModelEntry.synthesize(s.availableModels, s.channels) }
                }
                if (botModelEntries.isNotEmpty()) {
                    ChannelAwareModelPicker(
                        entries = botModelEntries,
                        channels = s.channels,
                        selectedValue = s.editBotModel,
                        accentColor = c.accent,
                        showSearchBox = botModelEntries.size > 12,
                        onSelect = { model.updateBotEditField(modelName = it) }
                    )
                }
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("在本设备上开启好奇心心跳", color = c.ink, fontSize = 14.5.sp)
                        Text("仅控制当前客户端（如手机开、平板关互不影响）", color = c.muted, fontSize = 11.5.sp)
                    }
                    Switch(checked = s.editBotCuriosity, onCheckedChange = { model.updateBotEditField(curiosity = it) })
                }
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("在本设备上允许主动通知", color = c.ink, fontSize = 14.5.sp)
                        Text("仅控制当前客户端是否弹出这位 Bot 的主动提醒", color = c.muted, fontSize = 11.5.sp)
                    }
                    Switch(checked = s.editBotNotify, onCheckedChange = { model.updateBotEditField(notify = it) })
                }
            }
            PulseAction(if (s.savingBot) "保存中…" else if (isNew) "创建此 Bot" else "保存修改") {
                focus.clearFocus()
                model.saveBotEditor()
            }
            val editingId = s.editingBotId
            if (editingId != null) {
                var showPromptPreview by rememberSaveable(editingId) { mutableStateOf(false) }
                Column(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(16.dp))
                        .background(c.surface)
                        .padding(14.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    Row(
                        Modifier.fillMaxWidth()
                            .clip(RoundedCornerShape(10.dp))
                            .iosPressScale(0.98f) {
                                showPromptPreview = !showPromptPreview
                                if (showPromptPreview && s.promptPreview == null && !s.promptPreviewLoading) {
                                    model.fetchPromptPreview(s.editingBotId)
                                }
                            }
                            .padding(vertical = 2.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(Modifier.weight(1f)) {
                            Text("提示词装配与记忆插槽预览", color = c.ink, fontSize = 14.5.sp, fontWeight = FontWeight.SemiBold)
                            val pv = s.promptPreview
                            val metaLine = if (pv != null) {
                                val chars = pv.optInt("total_chars", 0)
                                val est = pv.optInt("estimated_tokens", 0)
                                val pt = pv.optInt("last_prompt_tokens", 0)
                                val ct = pv.optInt("last_cached_tokens", 0)
                                if (pt > 0) "总计 ${chars}字 (~${est} tok) · 实测 ${pt} / 缓存 ${ct}"
                                else "总计 ${chars}字 · 估算 ~${est} tok"
                            } else {
                                "查看真实发送给模型的 5 段 System Prompt"
                            }
                            Text(metaLine, color = c.muted, fontSize = 11.5.sp, style = TextStyle(fontFeatureSettings = "tnum"))
                        }
                        Text(if (showPromptPreview) "收起" else "展开", color = c.accent, fontSize = 12.5.sp, fontWeight = FontWeight.Medium)
                    }
                    AnimatedVisibility(
                        visible = showPromptPreview,
                        enter = if (reduceMotion) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(150)),
                        exit = if (reduceMotion) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(110))
                    ) {
                        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                                Text(
                                    if (s.promptPreviewLoading) "正在拉取装配详情…" else "节点：${s.promptPreview?.optString("node_label", "手机端") ?: "手机端"}",
                                    color = c.muted,
                                    fontSize = 11.5.sp
                                )
                                Text(
                                    if (s.promptPreviewLoading) "刷新中" else "刷新预览",
                                    Modifier.clip(RoundedCornerShape(8.dp))
                                        .background(c.fill)
                                        .iosPressScale(0.95f, enabled = !s.promptPreviewLoading) { model.fetchPromptPreview(s.editingBotId) }
                                        .padding(horizontal = 10.dp, vertical = 5.dp),
                                    color = c.accent,
                                    fontSize = 11.5.sp,
                                    fontWeight = FontWeight.Medium
                                )
                            }
                            val segs = s.promptPreview?.optJSONObject("segments")
                            if (segs != null) {
                                listOf(
                                    "1. Harness 底座指令" to segs.optString("system_base", ""),
                                    "2. 置顶记忆块 (Static Pinned)" to segs.optString("pinned_block", ""),
                                    "3. 记忆快照 (Vault Context)" to segs.optString("vault_context", ""),
                                    "4. 角色设定与节点身份" to segs.optString("persona_and_node", ""),
                                    "5. 响应校准尾段 (Calibration)" to segs.optString("calibration_tail", "")
                                ).forEach { (title, content) ->
                                    var expandedSeg by remember { mutableStateOf(title.startsWith("4.") || title.startsWith("5.")) }
                                    Column(
                                        Modifier.fillMaxWidth()
                                            .clip(RoundedCornerShape(10.dp))
                                            .background(c.fill.copy(alpha = 0.65f))
                                            .padding(10.dp),
                                        verticalArrangement = Arrangement.spacedBy(4.dp)
                                    ) {
                                        Row(
                                            Modifier.fillMaxWidth().heightIn(min = 36.dp).clickable { expandedSeg = !expandedSeg },
                                            verticalAlignment = Alignment.CenterVertically
                                        ) {
                                            Text(title, Modifier.weight(1f), color = c.ink, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                                            Text("${content.length}字 · ${if (expandedSeg) "▲" else "▼"}", color = c.muted, fontSize = 11.sp, style = TextStyle(fontFeatureSettings = "tnum"))
                                        }
                                        AnimatedVisibility(
                                            visible = expandedSeg,
                                            enter = if (reduceMotion) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(140)),
                                            exit = if (reduceMotion) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(110))
                                        ) {
                                            SelectionContainer {
                                                Text(
                                                    content.ifBlank { "（空）" },
                                                    color = c.muted,
                                                    fontSize = 11.5.sp,
                                                    lineHeight = 16.sp
                                                )
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                if (s.primary != s.editingBotId) {
                    PulseAction("将此 Bot 设为主助理", filled = false) { model.setPrimaryBot(s.editingBotId) }
                }
                if (s.bots.size > 1 && s.primary != s.editingBotId) {
                    var confirmDelete by remember { mutableStateOf(false) }
                    if (confirmDelete) {
                        PulseAction("确认删除「${s.editBotName.ifBlank { s.editingBotId }}」（不可恢复）", filled = false) {
                            confirmDelete = false
                            model.deleteBot(s.editingBotId)
                        }
                    } else {
                        PulseAction("删除此 Bot", filled = false) { confirmDelete = true }
                    }
                }
            }
            Spacer(Modifier.height(28.dp))
        }
    }
}
