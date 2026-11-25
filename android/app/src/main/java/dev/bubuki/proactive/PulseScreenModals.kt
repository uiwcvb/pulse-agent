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
 * [Module] PulseScreenModals
 * [Responsibility]
 * 模態設置與 Bot 檔案編輯器領域模塊：負責全局設置面板、首次配對引導向導、API 與備用模型配置、OTA 檢查更新 (NativeSettingsPanel)，
 * 以及 Telegram 風格 Bot 詳情頁、人設指令編輯、上下文緩存統計與記憶釘住管理 (NativeBotEditorPanel)。
 *
 * [Public API]
 * - NativeSettingsPanel(model: NativePulseModel, modifier: Modifier)
 * - NativeBotEditorPanel(model: NativePulseModel, modifier: Modifier)
 */

// --- 1. Native Settings Panel (Clean 2-Level Sub-Interface, Zero Emojis) ---
@Composable
internal fun NativeSettingsPanel(model: NativePulseModel, modifier: Modifier) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val focus = LocalFocusManager.current
    val phoneState by RootPhoneHarness.changes.collectAsState()
    var subPage by rememberSaveable { mutableStateOf<String?>(null) }
    androidx.activity.compose.BackHandler(enabled = subPage != null) {
        focus.clearFocus()
        subPage = null
    }
    var modelFilter by remember { mutableStateOf("") }
    var workerModelFilter by remember { mutableStateOf("") }
    val pageTitle = when (subPage) {
        "api" -> "模型与接口"
        "phone" -> "手机虚拟屏"
        "memory" -> "记忆与缓存"
        "rhythm" -> "主动任务与通知"
        "appearance" -> "外观与省电"
        "update" -> "软件更新"
        else -> "设置"
    }
    val settingsReduce = rememberReducedMotion()
    val settingsScroll = rememberScrollState()
    LaunchedEffect(subPage) { settingsScroll.scrollTo(0) }
    val showSaveBtn = subPage in setOf("api", "memory", "rhythm", "appearance") || s.savingSettings
    val settingsDivAlpha by animateFloatAsState(
        targetValue = if (settingsScroll.value > 6) 0.65f else 0f,
        animationSpec = if (settingsReduce) snap() else tween(160),
        label = "settings_hdr_div"
    )
    Column(modifier.background(c.bg)) {
        Surface(color = c.surface.copy(alpha = 0.96f), tonalElevation = 0.dp) {
            Column {
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Row(
                        Modifier.heightIn(min = 40.dp).clip(RoundedCornerShape(10.dp))
                            .iosPressScale(0.95f) {
                                focus.clearFocus()
                                if (subPage != null) subPage = null else model.closePanel()
                            }
                            .padding(horizontal = 8.dp, vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        PulseIcon("back", c.accent, Modifier.size(21.dp))
                        Text(if (subPage != null) "设置" else "返回", color = c.accent, fontSize = 16.sp)
                    }
                    AnimatedContent(
                        targetState = pageTitle,
                        modifier = Modifier.weight(1f),
                        transitionSpec = {
                            if (settingsReduce) fadeIn(tween(110)) togetherWith fadeOut(tween(90))
                            else fadeIn(tween(150)) togetherWith fadeOut(tween(100))
                        },
                        label = "settings_title"
                    ) { titleText ->
                        Text(
                            titleText,
                            Modifier.fillMaxWidth(),
                            color = c.ink,
                            fontSize = 17.5.sp,
                            fontWeight = FontWeight.SemiBold,
                            textAlign = TextAlign.Center
                        )
                    }
                    Box(Modifier.widthIn(min = 58.dp), contentAlignment = Alignment.CenterEnd) {
                        androidx.compose.animation.AnimatedVisibility(
                            visible = showSaveBtn,
                            enter = fadeIn(tween(150)),
                            exit = fadeOut(tween(110))
                        ) {
                            Box(
                                Modifier.heightIn(min = 40.dp).clip(RoundedCornerShape(14.dp))
                                    .background(c.accent)
                                    .iosPressScale(0.95f, enabled = !s.savingSettings) { focus.clearFocus(); model.saveSettings() }
                                    .padding(horizontal = 14.dp, vertical = 7.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text(
                                    if (s.savingSettings) "同步中…" else "保存",
                                    color = Color.White,
                                    fontSize = 13.5.sp,
                                    fontWeight = FontWeight.SemiBold)
                            }
                        }
                    }
                }
                HorizontalDivider(color = c.line.copy(alpha = settingsDivAlpha), thickness = 0.5.dp)
            }
        }
        AnimatedContent(
            targetState = subPage,
            modifier = Modifier.fillMaxSize(),
            transitionSpec = { appSubPageTransitionSpec(goingDeeper = targetState != null, reduceMotion = settingsReduce) },
            label = "settings_subpage"
        ) { page ->
        Column(
            Modifier.fillMaxSize().verticalScroll(settingsScroll).padding(horizontal = 16.dp, vertical = 14.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            when (page) {
                null -> {
                    // Level 1: Minimal Grouped Category Rows
                    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text("连接与模型", color = c.muted, fontSize = 12.sp, fontWeight = FontWeight.Medium, modifier = Modifier.padding(start = 6.dp))
                        Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface)) {
                            SettingsCategoryRow(
                                icon = "cloud",
                                title = "模型与接口",
                                subtitle = "服务器、主模型、辅助模型与备用切换",
                                value = s.defaultModel.ifBlank { if (s.hasApiKey) "已配置" else "待配置" }
                            ) { subPage = "api" }
                        }
                    }
                    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text("运行与记忆", color = c.muted, fontSize = 12.sp, fontWeight = FontWeight.Medium, modifier = Modifier.padding(start = 6.dp))
                        Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface)) {
                            SettingsCategoryRow(
                                icon = "vault",
                                title = "记忆与缓存",
                                subtitle = "上下文上限、缓存保活、向量模型与记忆整理",
                                value = if (s.cacheKeepaliveEnabled) "${s.idleCompactMinutes}m" else "标准"
                            ) { subPage = "memory" }
                            HorizontalDivider(Modifier.padding(start = 54.dp), color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
                            SettingsCategoryRow(
                                icon = "screen",
                                title = "手机虚拟屏",
                                subtitle = "Root / Shizuku / 无线 ADB 权限",
                                value = if (phoneState.privileged || phoneState.root) "已就绪" else "待授权"
                            ) { subPage = "phone" }
                            HorizontalDivider(Modifier.padding(start = 54.dp), color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
                            SettingsCategoryRow(
                                icon = "bell",
                                title = "主动任务与通知",
                                subtitle = "巡检间隔、后台保活与分级通知",
                                value = if (s.curiosityEnabledGlobal) "${s.curiosityMinutes}m" else "已关闭"
                            ) { subPage = "rhythm" }
                        }
                    }
                    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text("系统", color = c.muted, fontSize = 12.sp, fontWeight = FontWeight.Medium, modifier = Modifier.padding(start = 6.dp))
                        Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface)) {
                            SettingsCategoryRow(
                                icon = "moon",
                                title = "外观与省电",
                                subtitle = "主题、动效与省电",
                                value = if (s.powerSave) "省电" else if (s.appearance == "light") "浅色" else "深色"
                            ) { subPage = "appearance" }
                            HorizontalDivider(Modifier.padding(start = 54.dp), color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
                            SettingsCategoryRow(
                                icon = "spark",
                                title = "软件更新",
                                subtitle = "v${BuildConfig.VERSION_NAME} (Build ${BuildConfig.VERSION_CODE})",
                                value = when { s.checkingUpdate -> "检查中"; !s.updateChecked -> "检查更新"; s.updateAvailable -> "有更新"; else -> "最新" }
                            ) { subPage = "update" }
                        }
                    }
                }
                "api" -> {
                    Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(10.dp)
                    ) {
                        Text("1. 服务器连接", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
                        PulseInputField(
                            label = "Server URL",
                            value = s.serverUrl,
                            placeholder = "https://your-server-ip/proactive-agent",
                            onChange = { model.updateSettingsField(serverUrl = it) })
                        PulseInputField(
                            label = "设备配对码 (Device Token)",
                            value = s.deviceTokenInput,
                            placeholder = "粘贴你的 device-token",
                            onChange = { model.updateSettingsField(deviceTokenInput = it) })
                        if (s.connectionTestMsg.isNotBlank()) {
                            Text(
                                s.connectionTestMsg,
                                color = when (s.connectionTestOk) {
                                    true -> c.green
                                    false -> Color(0xFFFF453A)
                                    null -> c.accent
                                },
                                fontSize = 12.sp
                            )
                        }
                        Box(
                            Modifier.fillMaxWidth()
                                .clip(RoundedCornerShape(12.dp))
                                .background(c.accent)
                                .iosPressScale(0.97f, enabled = !s.testingConnection) {
                                    focus.clearFocus()
                                    model.connectServer(s.serverUrl, s.deviceTokenInput)
                                }
                                .padding(vertical = 11.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                if (s.testingConnection) "正在连接中枢服务器…" else "保存并连接服务器",
                                color = Color.White,
                                fontSize = 13.5.sp,
                                fontWeight = FontWeight.SemiBold)
                        }
                    }
                    Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Text("2. 主模型接口 (OpenAI 兼容)", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
                        PulseInputField(
                            label = "API Base URL（不含 /chat/completions）",
                            value = s.apiBaseUrl,
                            placeholder = "https://api.openai.com/v1",
                            onChange = { model.updateSettingsField(apiBaseUrl = it) })
                        PulseInputField(
                            label = if (s.hasApiKey && s.apiKeyInput.isBlank()) "API Key（云端已保存，留空保持不变）" else "API Key",
                            value = s.apiKeyInput,
                            placeholder = if (s.hasApiKey) "sk-...（已保存，如需更换请重新输入）" else "sk-...",
                            onChange = { model.updateSettingsField(apiKeyInput = it) })
                        Box(
                            Modifier.fillMaxWidth()
                                .clip(RoundedCornerShape(12.dp))
                                .background(c.accent)
                                .iosPressScale(0.97f, enabled = !s.fetchingModels) { focus.clearFocus(); model.fetchModels(false) }
                                .padding(vertical = 11.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                if (s.fetchingModels) "正在请求 /models…" else "获取可用模型列表 (/models)",
                                color = Color.White,
                                fontSize = 13.5.sp,
                                fontWeight = FontWeight.SemiBold)
                        }
                        PulseInputField(
                            label = "全局默认模型",
                            value = s.defaultModel,
                            placeholder = "填入或从下方选择模型名称",
                            onChange = { model.updateSettingsField(defaultModel = it) })
                        val resolvedEntries = remember(s.modelEntries, s.availableModels, s.channels) {
                            s.modelEntries.ifEmpty { PulseModelEntry.synthesize(s.availableModels, s.channels) }
                        }
                        if (resolvedEntries.isNotEmpty()) {
                            ChannelAwareModelPicker(
                                entries = resolvedEntries,
                                channels = s.channels,
                                selectedValue = s.defaultModel,
                                accentColor = c.accent,
                                searchQuery = modelFilter,
                                showSearchBox = true,
                                onSearchChange = { modelFilter = it },
                                onSelect = { model.updateSettingsField(defaultModel = it) }
                            )
                        }
                    }
                    ApiChannelsSection(model)
                    Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text("3. 辅助模型（子任务与整理）", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
                                Text(
                                    "用于子任务与记忆整理，留空则使用主模型",
                                    color = c.muted,
                                    fontSize = 12.sp
                                )
                            }
                        }
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Box(
                                Modifier.weight(1f)
                                    .clip(RoundedCornerShape(10.dp))
                                    .background(c.accent)
                                    .iosPressScale(0.96f, enabled = !s.fetchingModels) { focus.clearFocus(); model.fetchModels(false) }
                                    .padding(vertical = 9.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text(
                                    if (s.fetchingModels) "获取中…" else "获取模型列表",
                                    color = Color.White,
                                    fontSize = 12.5.sp,
                                    fontWeight = FontWeight.SemiBold
                                )
                            }
                            if (s.workerModel.isNotBlank()) {
                                Box(
                                    Modifier.clip(RoundedCornerShape(10.dp))
                                        .background(c.fill)
                                        .iosPressScale(0.96f) { model.updateSettingsField(workerModel = "") }
                                        .padding(horizontal = 12.dp, vertical = 9.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    Text("跟随主模型", color = c.muted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
                                }
                            }
                        }
                        PulseInputField(
                            label = "辅助模型",
                            value = s.workerModel,
                            placeholder = "留空则跟随主模型",
                            onChange = { model.updateSettingsField(workerModel = it) })
                        val workerEntries = remember(s.modelEntries, s.availableModels, s.channels) {
                            s.modelEntries.ifEmpty { PulseModelEntry.synthesize(s.availableModels, s.channels) }
                        }
                        if (workerEntries.isNotEmpty()) {
                            ChannelAwareModelPicker(
                                entries = workerEntries,
                                channels = s.channels,
                                selectedValue = s.workerModel,
                                accentColor = c.accent,
                                searchQuery = workerModelFilter,
                                showSearchBox = true,
                                onSearchChange = { workerModelFilter = it },
                                onSelect = { model.updateSettingsField(workerModel = it) }
                            )
                        }
                    }
                    Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text("启用备用模型自动切换", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
                                Text(
                                    if (s.fallbackEnabled) "当主模型返回 500/502/429/超时时，自动切换至备用模型"
                                    else "已关闭（仅使用主模型，不自动切换备用接口）",
                                    color = c.muted,
                                    fontSize = 12.sp
                                )
                            }
                            Switch(
                                checked = s.fallbackEnabled,
                                onCheckedChange = { model.updateSettingsField(fallbackEnabled = it) }
                            )
                        }
                        val fbReduce = rememberReducedMotion()
                        AnimatedVisibility(
                            visible = s.fallbackEnabled,
                            enter = if (fbReduce) fadeIn(tween(110)) else expandVertically(iosFluidSpring()) + fadeIn(tween(150)),
                            exit = if (fbReduce) fadeOut(tween(90)) else shrinkVertically(iosGentleSpring()) + fadeOut(tween(110))
                        ) {
                            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                            PulseInputField(
                                label = "统一备用模型名称",
                                value = s.fallbackModel,
                                placeholder = "填入备用模型名称",
                                onChange = { model.updateSettingsField(fallbackModel = it) })
                            val fbEntries = remember(s.modelEntries, s.availableModels, s.channels) {
                                s.modelEntries.ifEmpty { PulseModelEntry.synthesize(s.availableModels, s.channels) }
                            }
                            if (fbEntries.isNotEmpty()) {
                                Text("点选设为统一备用模型：", color = c.muted, fontSize = 11.5.sp)
                                ChannelAwareModelPicker(
                                    entries = fbEntries,
                                    channels = s.channels,
                                    selectedValue = s.fallbackModel,
                                    accentColor = c.green,
                                    searchQuery = modelFilter,
                                    showSearchBox = false,
                                    onSelect = { model.updateSettingsField(fallbackModel = it) }
                                )
                            }
                            PulseInputField(
                                label = "备用 API Base URL（留空则共用上方主接口）",
                                value = s.fallbackBaseUrl,
                                placeholder = "留空则共用主接口 Base URL",
                                onChange = { model.updateSettingsField(fallbackBaseUrl = it) })
                            PulseInputField(
                                label = if (s.hasFallbackApiKey && s.fallbackApiKeyInput.isBlank()) "备用 API Key（云端已保存，留空保持不变）" else "备用 API Key（留空则共用主 Key）",
                                value = s.fallbackApiKeyInput,
                                placeholder = "sk-...（留空则共用主 API Key）",
                                onChange = { model.updateSettingsField(fallbackApiKeyInput = it) })
                            }
                        }
                    }
                }
                "phone" -> {
                    Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text("手机独立虚拟屏与权限状态", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
                                val ready = phoneState.privileged || phoneState.root
                                Text(
                                    if (ready) {
                                        when (phoneState.privilegeMode) {
                                            "shizuku" -> "Shizuku 独立虚拟屏已就绪 (uid ${phoneState.uid})"
                                            "wireless_adb" -> "无线 ADB 独立虚拟屏已就绪 (uid ${phoneState.uid})"
                                            else -> "Root 后台独立虚拟屏已就绪 (uid 0)"
                                        }
                                    } else "支持免 Root Shizuku / 无线 ADB (uid 2000) 或 Root 启用后台独立虚拟屏",
                                    color = if (ready) c.green else c.muted,
                                    fontSize = 12.5.sp,
                                    fontWeight = FontWeight.Medium)
                            }
                            val readyTag = phoneState.privileged || phoneState.root
                            Text(
                                if (readyTag) "已连接" else "待连接",
                                color = if (readyTag) c.green else c.amber,
                                fontSize = 12.sp,
                                fontWeight = FontWeight.SemiBold)
                        }
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            val btnReady = phoneState.privileged || phoneState.root
                            Box(
                                Modifier.weight(1f)
                                    .clip(RoundedCornerShape(10.dp))
                                    .background(if (btnReady) c.fill else c.accent)
                                    .iosPressScale(0.96f) { model.connectRootVirtualDisplay() }
                                    .padding(vertical = 10.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text(
                                    when {
                                        !btnReady -> "连接 Root / Shizuku / ADB"
                                        phoneState.privilegeMode == "shizuku" -> "Shizuku 已就绪"
                                        phoneState.privilegeMode == "wireless_adb" -> "无线 ADB 已就绪"
                                        else -> "Root 已授权"
                                    },
                                    color = if (btnReady) c.green else Color.White,
                                    fontSize = 12.5.sp,
                                    fontWeight = FontWeight.SemiBold)
                            }
                            Box(
                                Modifier.weight(1f)
                                    .clip(RoundedCornerShape(10.dp))
                                    .background(c.fill)
                                    .iosPressScale(0.96f) { model.launchFloatingVirtualDisplay() }
                                    .padding(vertical = 10.dp),
                                contentAlignment = Alignment.Center
                            ) {
                                Text(
                                    if (phoneState.displayCreated) "展开虚拟屏小窗" else "启动虚拟屏工作区",
                                    color = c.ink,
                                    fontSize = 12.5.sp,
                                    fontWeight = FontWeight.Medium)
                            }
                        }
                    }
                    ShizukuSettingsCard(model)
                    WirelessAdbSettingsCard(model)
                }
                "memory" -> {
                    Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Text("上下文与缓存", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
                        val curSettingCompact = (s.autoCompactTokens.toIntOrNull() ?: 200000).coerceIn(8000, 1000000)
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                            Text("上下文窗口上限 / 自动压缩阈值", color = c.ink, fontSize = 14.sp, fontWeight = FontWeight.Medium)
                            Text(
                                "${model.formatTokenCount(curSettingCompact)} Tokens",
                                color = c.accent,
                                fontSize = 13.5.sp,
                                fontWeight = FontWeight.Bold,
                                style = TextStyle(fontFeatureSettings = "tnum")
                            )
                        }
                        Slider(
                            value = curSettingCompact.coerceIn(16000, 200000).toFloat(),
                            onValueChange = { v ->
                                val stepped = ((v / 4000f).roundToInt() * 4000).coerceIn(16000, 200000)
                                model.updateSettingsField(autoCompactTokens = stepped.toString())
                            },
                            onValueChangeFinished = {
                                val finalTok = (s.autoCompactTokens.toIntOrNull() ?: 200000).coerceIn(8000, 1000000)
                                model.applyContextWindowTokens(finalTok)
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
                                val selected = curSettingCompact == tokInt
                                Text(
                                    label,
                                    Modifier.weight(1f)
                                        .clip(RoundedCornerShape(8.dp))
                                        .background(if (selected) c.accent else c.fill)
                                        .iosPressScale(0.95f) { model.applyContextWindowTokens(tokInt) }
                                        .padding(vertical = 7.dp),
                                    color = if (selected) Color.White else c.ink,
                                    fontSize = 11.5.sp,
                                    fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
                                    textAlign = TextAlign.Center,
                                    style = TextStyle(fontFeatureSettings = "tnum")
                                )
                            }
                        }
                        PulseInputField(
                            label = "自定义上下文窗口（Tokens，8000-1000000）",
                            value = s.autoCompactTokens,
                            placeholder = "200000",
                            onChange = { model.updateSettingsField(autoCompactTokens = it.filter { ch -> ch.isDigit() }) })
                        HorizontalDivider(color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text("缓存保活与空闲压缩", color = c.ink, fontSize = 14.sp)
                                Text("上下文 ≥ 40K 时短暂保活，空闲到点压缩后停止", color = c.muted, fontSize = 11.5.sp)
                            }
                            Switch(checked = s.cacheKeepaliveEnabled, onCheckedChange = { model.updateSettingsField(cacheKeepaliveEnabled = it) })
                        }
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Box(Modifier.weight(1f)) {
                                PulseInputField(
                                    label = "保活次数 (0-5)",
                                    value = s.cacheKeepaliveMaxPings,
                                    placeholder = "1",
                                    onChange = { model.updateSettingsField(cacheKeepaliveMaxPings = it.filter { ch -> ch.isDigit() }) })
                            }
                            Box(Modifier.weight(1f)) {
                                PulseInputField(
                                    label = "保活间隔 (秒)",
                                    value = s.cacheKeepaliveIntervalSec,
                                    placeholder = "260",
                                    onChange = { model.updateSettingsField(cacheKeepaliveIntervalSec = it.filter { ch -> ch.isDigit() }) })
                            }
                            Box(Modifier.weight(1f)) {
                                PulseInputField(
                                    label = "空闲压缩 (分,0=关)",
                                    value = s.idleCompactMinutes,
                                    placeholder = "9",
                                    onChange = { model.updateSettingsField(idleCompactMinutes = it.filter { ch -> ch.isDigit() }) })
                            }
                        }
                    }
                    Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Text("记忆与向量检索", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
                        Text("本地向量模型（闲置 5 分钟自动卸载）", color = c.muted, fontSize = 12.sp)
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                            listOf(
                                "off" to "关闭",
                                "local_gte_small_zh" to "GTE-S",
                                "local_dmeta_zh_small" to "Dmeta",
                                "local_gte_base_zh" to "BGE-B"
                            ).forEach { (tierId, lbl) ->
                                val sel = s.embeddingTier == tierId
                                Text(
                                    lbl,
                                    Modifier.weight(1f)
                                        .clip(RoundedCornerShape(8.dp))
                                        .background(if (sel) c.accent else c.fill)
                                        .iosPressScale(0.95f) { model.updateSettingsField(embeddingTier = tierId) }
                                        .padding(vertical = 8.dp),
                                    color = if (sel) Color.White else c.ink,
                                    fontSize = 11.5.sp,
                                    fontWeight = if (sel) FontWeight.SemiBold else FontWeight.Normal,
                                    textAlign = TextAlign.Center
                                )
                            }
                        }
                        Text(
                            when (s.embeddingTier) {
                                "off" -> "仅使用查表与全文检索（0 MB）"
                                "local_dmeta_zh_small" -> "Dmeta-zh-small INT8 · ~170 MB · 1024 上下文"
                                "local_gte_base_zh" -> "bge-base-zh-v1.5 INT8 · ~240 MB · 适合 2G+ 内存"
                                else -> "gte-small-zh INT8 · ~100 MB · 推荐"
                            },
                            color = c.muted,
                            fontSize = 11.5.sp
                        )
                        PulseInputField(
                            label = "闲置记忆提炼等待 (分钟，默认 8 分，错开 9 分热压缩)",
                            value = s.sleepConsolidateIdleMinutes,
                            placeholder = "8",
                            onChange = { model.updateSettingsField(sleepConsolidateIdleMinutes = it.filter { ch -> ch.isDigit() }) }
                        )
                        Box(
                            Modifier.fillMaxWidth()
                                .clip(RoundedCornerShape(10.dp))
                                .background(c.fill)
                                .iosPressScale(0.97f) { if (!s.reorganizingMemory) model.triggerMemoryReorganize() }
                                .padding(horizontal = 12.dp, vertical = 10.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                if (s.reorganizingMemory) "正在整理记忆…" else "重新整理记忆（备份并迁移旧数据）",
                                color = if (s.reorganizingMemory) c.muted else c.accent,
                                fontSize = 13.5.sp,
                                fontWeight = FontWeight.SemiBold
                            )
                        }
                        if (s.memoryReorgNotice.isNotBlank()) {
                            Text(s.memoryReorgNotice, color = c.muted, fontSize = 11.5.sp)
                        }
                    }
                }
                "rhythm" -> {
                    Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Text("主动巡检与后台通知", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text("启用后台主动巡检", color = c.ink, fontSize = 14.5.sp)
                                Text("闲置时按间隔执行已启用的助手巡检", color = c.muted, fontSize = 11.5.sp)
                            }
                            Switch(checked = s.curiosityEnabledGlobal, onCheckedChange = { model.updateSettingsField(curiosityEnabled = it) })
                        }
                        PulseInputField(
                            label = "巡检间隔（分钟，5-1440）",
                            value = s.curiosityMinutes,
                            placeholder = "30",
                            onChange = { model.updateSettingsField(curiosityMinutes = it.filter { ch -> ch.isDigit() }) })
                        HorizontalDivider(color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text("手机后台常驻接收通知", color = c.ink, fontSize = 14.5.sp)
                                Text("保持加密 WebSocket 实时连接，支持后台虚拟屏操作与提醒", color = c.muted, fontSize = 11.5.sp)
                            }
                            Switch(checked = s.backgroundService, onCheckedChange = { model.updateSettingsField(backgroundService = it) })
                        }
                        HorizontalDivider(color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
                        val notifyOk = phoneState.notifications && phoneState.overlay
                        Row(
                            Modifier.fillMaxWidth()
                                .clip(RoundedCornerShape(10.dp))
                                .background(c.fill)
                                .iosPressScale(0.97f) { model.openUnifiedNotificationSettings() }
                                .padding(horizontal = 12.dp, vertical = 10.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text("分级通知（弱通知栏 · 强悬浮窗）", color = c.ink, fontSize = 14.sp, fontWeight = FontWeight.Medium)
                                Text("弱通知走通知栏，强通知直接在手机桌面弹悬浮窗", color = c.muted, fontSize = 11.5.sp)
                            }
                            Text(
                                when {
                                    notifyOk -> "已开启"
                                    phoneState.notifications -> "待开悬浮窗"
                                    else -> "去开启"
                                },
                                color = if (notifyOk) c.green else c.amber,
                                fontSize = 12.sp,
                                fontWeight = FontWeight.SemiBold
                            )
                        }
                    }
                }
                "appearance" -> SettingsAppearanceSubPage(model)
                "update" -> {
                    LaunchedEffect(subPage) { if (!s.updateChecked && !s.checkingUpdate) model.checkForUpdates(silent = true) }
                    Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(11.dp)
                    ) {
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text("版本与软件更新", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
                                Text(
                                    buildString {
                                        append("当前版本 v${BuildConfig.VERSION_NAME} (Build ${BuildConfig.VERSION_CODE})")
                                        if (s.updateChecked && s.latestReleaseVersion.isNotBlank()) {
                                            append(" · 云端最新 v${s.latestReleaseVersion}")
                                            if (s.expectedApkSize > 0L) append(" (${String.format(java.util.Locale.US, "%.1f", s.expectedApkSize / 1048576.0)} MB)")
                                        } else append(" · 尚未检查云端版本")
                                    },
                                    color = if (s.updateAvailable) c.green else c.muted,
                                    fontSize = 12.sp,
                                    style = TextStyle(fontFeatureSettings = "tnum"))
                            }
                            Text(
                                if (s.checkingUpdate) "检查中…" else "检查更新",
                                Modifier.clip(RoundedCornerShape(10.dp))
                                    .background(c.fill)
                                    .iosPressScale(0.95f, enabled = !s.checkingUpdate) { model.checkForUpdates(false) }
                                    .padding(horizontal = 12.dp, vertical = 7.dp),
                                color = c.ink,
                                fontSize = 12.sp,
                                fontWeight = FontWeight.Medium)
                        }
                        if (s.latestReleaseNotes.isNotBlank()) {
                            Text(s.latestReleaseNotes, color = c.muted, fontSize = 12.sp, lineHeight = 17.sp)
                        }
                        Box(
                            Modifier.fillMaxWidth()
                                .clip(RoundedCornerShape(12.dp))
                                .background(if (s.updateAvailable) c.green else c.accent)
                                .iosPressScale(0.97f, enabled = !s.downloadingApk) { model.downloadLatestApk() }
                                .padding(vertical = 11.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(
                                when {
                                    s.downloadingApk -> "正在应用内下载更新… ${s.downloadProgress}% (下载完自动安装)"
                                    s.updateAvailable -> "发现新版本 v${s.latestReleaseVersion} · 立即下载安装"
                                    !s.updateChecked -> "点此检查并下载最新客户端 APK"
                                    else -> "重新下载并覆盖安装最新客户端 APK"
                                },
                                color = Color.White,
                                fontSize = 13.5.sp,
                                fontWeight = FontWeight.SemiBold,
                                style = TextStyle(fontFeatureSettings = "tnum"))
                        }
                    }
                }
            }
            Spacer(Modifier.height(28.dp))
        }
        }
    }
}

@Composable
internal fun ConnectionHealthDialog(model: NativePulseModel, onDismiss: () -> Unit) {
    val c = LocalPulsePalette.current
    val ctx = LocalContext.current
    val phoneSt by RootPhoneHarness.changes.collectAsState()
    val overlayMode by FloatingVirtualDisplay.overlayModeFlow.collectAsState()
    val lifecycleStarted = rememberLifecycleStarted()
    val powerSave = LocalPowerSaveMode.current
    var health by remember { mutableStateOf(AgentForegroundService.currentHealth()) }
    var probing by remember { mutableStateOf(false) }

    LaunchedEffect(lifecycleStarted, powerSave) {
        if (!lifecycleStarted) return@LaunchedEffect
        val intervalMs = if (powerSave) 3000L else 1000L
        while (true) {
            health = AgentForegroundService.currentHealth()
            kotlinx.coroutines.delay(intervalMs)
        }
    }

    val nowMs = System.currentTimeMillis()
    val inboundAgo = if (health.lastInboundMs > 0L) "${((nowMs - health.lastInboundMs) / 1000L).coerceAtLeast(0L)}s 前" else "无记录"
    val rttLabel = if (health.lastPongRttMs >= 0L) "${health.lastPongRttMs} ms" else "未测"
    val phaseOk = health.phase == "CONNECTED"
    val phaseColor = if (phaseOk) Color(0xFF34C759) else Color(0xFFFF9F0A)

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = c.surface,
        titleContentColor = c.ink,
        textContentColor = c.ink,
        title = {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Box(Modifier.size(9.dp).clip(CircleShape).background(phaseColor))
                Text("连接与虚拟屏实时自检", fontSize = 17.sp, fontWeight = FontWeight.SemiBold)
            }
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("WS 长连状态：${health.phase} (代次 #${health.wsGeneration})", fontSize = 13.5.sp, color = c.ink, style = TextStyle(fontFeatureSettings = "tnum"))
                Text("最近收包：$inboundAgo · 实测 RTT：$rttLabel", fontSize = 13.sp, color = c.muted, style = TextStyle(fontFeatureSettings = "tnum"))
                Text("挂起 RPC 队列：${health.pendingRpcCount} · PC 节点：${if (health.pcOnline) "在线" else "离线"}", fontSize = 13.sp, color = c.muted, style = TextStyle(fontFeatureSettings = "tnum"))
                Text("最近链路说明：${health.lastReason}", fontSize = 12.5.sp, color = c.muted, maxLines = 2, overflow = TextOverflow.Ellipsis)
                HorizontalDivider(color = c.line.copy(alpha = 0.6f), thickness = 0.5.dp)
                Text(
                    "虚拟屏状态：${overlayMode.label}" + if (phoneSt.displayCreated) " · #${phoneSt.displayId}" else " · 未创建",
                    fontSize = 13.5.sp,
                    color = c.ink,
                    style = TextStyle(fontFeatureSettings = "tnum")
                )
                Text("工作区阶段：${phoneSt.phase.ifBlank { "idle" }} · 权限：${phoneSt.privilegeMode.ifBlank { "none" }}", fontSize = 12.5.sp, color = c.muted)
            }
        },
        confirmButton = {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(
                    enabled = !probing,
                    onClick = {
                        probing = true
                        AgentForegroundService.probeConnectionHealth(ctx) { updated ->
                            health = updated
                            probing = false
                        }
                    }
                ) {
                    Text(if (probing) "探测中…" else "探测 RTT", color = c.accent)
                }
                TextButton(
                    onClick = {
                        AgentForegroundService.forceReconnectFromUi(ctx)
                        health = AgentForegroundService.currentHealth()
                    }
                ) {
                    Text("重建长连", color = c.accent)
                }
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("关闭", color = c.muted) }
        }
    )
}

// --- 2. Telegram Contact Info Style Bot Profile & Context/Cache Secondary Page ---
