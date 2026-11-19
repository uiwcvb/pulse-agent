package dev.bubuki.proactive

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/**
 * [Module] OnboardingApiGate
 * [Responsibility]
 * 1. 首次启动零配置 API 门禁弹窗 (`WhaleChanApiGate`)：只需填 API Base URL + API Key 并选模型，
 *    保存后直接进入默认助理通过对话完成压缩阈值、PC/VPS 云电脑等全部后续设置。
 * 2. 免 Root 无线 ADB 独立虚拟屏设置卡片 (`WirelessAdbSettingsCard`)：
 *    支持本机无线调试 6 位配对码发现与自动连接 (`uid = 2000`)。
 */

internal fun NativePulseModel.dismissQuickGate() {
    ui = ui.copy(quickGateDismissed = true)
}

internal fun NativePulseModel.connectServer(
    serverUrlInput: String,
    deviceTokenInput: String
) {
    if (ui.testingConnection || ui.savingSettings) return
    val cleanServer = serverUrlInput.trim().ifBlank { SecureTransport.DEFAULT_SERVER }.trimEnd('/')
    val cleanToken = deviceTokenInput.trim()
    if (!cleanServer.startsWith("http://") && !cleanServer.startsWith("https://")) {
        ui = ui.copy(connectionTestOk = false, connectionTestMsg = "请输入以 http:// 或 https:// 开头的服务器地址")
        toast("请输入合法的服务器地址")
        return
    }
    if (cleanToken.isBlank()) {
        ui = ui.copy(connectionTestOk = false, connectionTestMsg = "请填写设备配对码 (Device Token)")
        toast("请先填写设备配对码")
        return
    }
    ui = ui.copy(
        serverUrl = cleanServer,
        deviceTokenInput = cleanToken,
        testingConnection = true,
        connectionTestOk = null,
        connectionTestMsg = "正在保存配置并连接中枢服务器…"
    )
    perform {
        try {
            withContext(Dispatchers.IO) {
                synchronized(SecurePrefs::class.java) {
                    val s = SecurePrefs.read(activity)
                    s.put("serverUrl", cleanServer)
                    s.put("deviceToken", cleanToken)
                    SecurePrefs.save(activity, s)
                    prefs = s
                }
                SecureTransport.invalidateCache()
            }
            val statusRes = request("GET", "/api/status")
            val serverHasAi = statusRes.optBoolean("small_ready", false) || statusRes.optBoolean("big_ready", false)
            ui = ui.copy(
                paired = true,
                online = true,
                serverUrl = cleanServer,
                deviceTokenInput = cleanToken,
                hasApiKey = serverHasAi || ui.hasApiKey,
                quickGateDismissed = if (serverHasAi) true else ui.quickGateDismissed,
                connectionTestOk = true,
                connectionTestMsg = if (serverHasAi) "服务器已连接，AI 接口已就绪！" else "服务器已连接，请继续配置大模型接口"
            )
            refresh(force = true)
            toast(if (serverHasAi) "已连上服务器，配置已保存！" else "服务器已连接！请配置大模型 API")
        } catch (e: Exception) {
            ui = ui.copy(
                connectionTestOk = false,
                connectionTestMsg = MainActivity.safeError(e)
            )
        } finally {
            ui = ui.copy(testingConnection = false)
        }
    }
}

internal fun NativePulseModel.completeQuickApiSetup(
    serverUrlInput: String,
    deviceTokenInput: String,
    baseUrlInput: String,
    apiKeyInput: String,
    modelInput: String
) {
    if (ui.testingConnection || ui.savingSettings) return
    val cleanServer = serverUrlInput.trim().ifBlank { SecureTransport.DEFAULT_SERVER }.trimEnd('/')
    val cleanToken = deviceTokenInput.trim().ifBlank { ui.deviceTokenInput.trim() }
    val cleanBase = baseUrlInput.trim().ifBlank { "https://api.openai.com/v1" }.trimEnd('/')
    val cleanKey = apiKeyInput.trim()
    if (cleanKey.isBlank() && !ui.hasApiKey) {
        toast("请先填写 API Key")
        return
    }
    ui = ui.copy(testingConnection = true, connectionTestOk = null, connectionTestMsg = "正在验证接口与模型…")
    perform {
        try {
            withContext(Dispatchers.IO) {
                synchronized(SecurePrefs::class.java) {
                    val s = SecurePrefs.read(activity)
                    if (cleanServer.isNotBlank()) s.put("serverUrl", cleanServer)
                    if (cleanToken.isNotBlank()) s.put("deviceToken", cleanToken)
                    SecurePrefs.save(activity, s)
                    prefs = s
                }
                SecureTransport.invalidateCache()
            }
            var resolvedModel = modelInput.trim()
            if (resolvedModel.isBlank()) {
                val modPayload = JSONObject().put("base_url", cleanBase)
                if (cleanKey.isNotBlank()) modPayload.put("api_key", cleanKey)
                val modRes = request("POST", "/api/models", modPayload)
                val arr = modRes.optJSONArray("models") ?: JSONArray()
                val fetched = (0 until arr.length()).mapNotNull { arr.optString(it).takeIf { m -> m.isNotBlank() } }
                if (fetched.isNotEmpty()) {
                    resolvedModel = fetched.first()
                    ui = ui.copy(availableModels = fetched)
                }
            }
            if (resolvedModel.isBlank()) resolvedModel = "deepseek-chat"

            val updatePayload = JSONObject()
                .put("api_base_url", cleanBase)
                .put("default_model", resolvedModel)
            if (cleanKey.isNotBlank()) updatePayload.put("api_key", cleanKey)
            request("POST", "/api/config", updatePayload)

            withContext(Dispatchers.IO) {
                val s = SecurePrefs.read(activity)
                val cfg = s.optJSONObject("config") ?: JSONObject()
                cfg.put("api_base_url", cleanBase).put("default_model", resolvedModel)
                if (cleanKey.isNotBlank()) cfg.put("api_key", cleanKey)
                val brainObj = JSONObject().put("base_url", cleanBase).put("model", resolvedModel)
                if (cleanKey.isNotBlank()) brainObj.put("api_key", cleanKey)
                cfg.put("small_brain", JSONObject(brainObj.toString())).put("big_brain", JSONObject(brainObj.toString()))
                s.put("serverUrl", cleanServer).put("config", cfg)
                SecurePrefs.save(activity, s)
                prefs = s
            }
            ui = ui.copy(
                serverUrl = cleanServer,
                apiBaseUrl = cleanBase,
                apiKeyInput = cleanKey,
                defaultModel = resolvedModel,
                hasApiKey = true,
                quickGateDismissed = true,
                connectionTestOk = true,
                connectionTestMsg = "连接成功，已启用 $resolvedModel"
            )
            refresh(force = true)
            open(ui.primary)
            toast("API 已配置就绪！直接和默认助手聊天即可完成其余设置")
        } catch (e: Exception) {
            ui = ui.copy(connectionTestOk = false, connectionTestMsg = MainActivity.safeError(e))
        } finally {
            ui = ui.copy(testingConnection = false)
        }
    }
}

@Composable
internal fun WhaleChanApiGate(model: NativePulseModel) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val needPair = !s.paired
    val needAiConfig = s.paired && s.syncLoadedOnce && !s.hasApiKey
    val visible = (needPair || needAiConfig) && !s.quickGateDismissed && s.activePanel == null
    var serverUrl by remember(s.serverUrl) { mutableStateOf(s.serverUrl) }
    var deviceToken by remember(s.deviceTokenInput) { mutableStateOf(s.deviceTokenInput) }
    var baseUrl by remember(s.apiBaseUrl) { mutableStateOf(s.apiBaseUrl) }
    var apiKey by remember(s.apiKeyInput) { mutableStateOf(s.apiKeyInput) }
    var modelName by remember(s.defaultModel) { mutableStateOf(s.defaultModel) }

    AnimatedVisibility(visible = visible, enter = fadeIn(), exit = fadeOut()) {
        Box(
            Modifier.fillMaxSize()
                .background(Color.Black.copy(alpha = 0.62f))
                .padding(horizontal = 20.dp, vertical = 28.dp),
            contentAlignment = Alignment.Center
        ) {
            Column(
                Modifier.fillMaxWidth()
                    .widthIn(max = 460.dp)
                    .clip(RoundedCornerShape(22.dp))
                    .background(c.surface)
                    .border(1.dp, c.line.copy(alpha = 0.28f), RoundedCornerShape(22.dp))
                    .verticalScroll(rememberScrollState())
                    .padding(20.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Box(
                        Modifier.size(38.dp).clip(CircleShape).background(Color(0xFF2EC4B6).copy(alpha = 0.18f)),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(if (needPair) "连" else "鲸", color = Color(0xFF2EC4B6), fontSize = 17.sp, fontWeight = FontWeight.Bold)
                    }
                    Column(Modifier.weight(1f)) {
                        Text(
                            if (needPair) "连接你的中枢服务器" else "配置大模型 API 接口",
                            color = c.ink, fontSize = 17.sp, fontWeight = FontWeight.Bold
                        )
                        Text(
                            if (needPair) "先填好服务器地址和设备配对码，连上后若已配置 AI 可直接开聊" else "服务器尚未配置大模型，请填写 OpenAI 兼容接口与密钥",
                            color = c.muted, fontSize = 12.sp
                        )
                    }
                }

                if (needPair) {
                    PulseInputField(
                        label = "中枢服务器地址 (Server URL)",
                        value = serverUrl,
                        placeholder = "https://your-server-ip/proactive-agent",
                        onChange = { serverUrl = it; model.updateSettingsField(serverUrl = it) }
                    )
                    PulseInputField(
                        label = "设备配对码 (Device Token)",
                        value = deviceToken,
                        placeholder = "粘贴你的 device-token",
                        onChange = { deviceToken = it; model.updateSettingsField(deviceTokenInput = it) }
                    )
                } else {
                    PulseInputField(
                        label = "API Base URL (OpenAI 兼容接口)",
                        value = baseUrl,
                        placeholder = "https://api.deepseek.com/v1 或 https://api.openai.com/v1",
                        onChange = { baseUrl = it; model.updateSettingsField(apiBaseUrl = it) }
                    )
                    PulseInputField(
                        label = "API Key",
                        value = apiKey,
                        placeholder = "sk-...",
                        onChange = { apiKey = it; model.updateSettingsField(apiKeyInput = it) }
                    )
                    PulseInputField(
                        label = "默认模型名（留空自动获取第一个可用模型）",
                        value = modelName,
                        placeholder = "例如 deepseek-v4.1-flash / deepseek-chat",
                        onChange = { modelName = it; model.updateSettingsField(defaultModel = it) }
                    )
                }

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

                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Box(
                        Modifier.weight(1f)
                            .clip(RoundedCornerShape(12.dp))
                            .background(c.fill)
                            .iosPressScale(0.96f) { model.dismissQuickGate() }
                            .padding(vertical = 11.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text("稍后再填", color = c.muted, fontSize = 13.sp, fontWeight = FontWeight.Medium)
                    }
                    Box(
                        Modifier.weight(2f)
                            .clip(RoundedCornerShape(12.dp))
                            .background(c.accent)
                            .iosPressScale(0.96f) {
                                if (needPair) {
                                    model.connectServer(serverUrl, deviceToken)
                                } else {
                                    model.completeQuickApiSetup(serverUrl, deviceToken, baseUrl, apiKey, modelName)
                                }
                            }
                            .padding(vertical = 11.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        if (s.testingConnection) {
                            CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp, color = Color.White)
                        } else {
                            Text(
                                if (needPair) "保存并连接服务器" else "保存并完成配置",
                                color = Color.White, fontSize = 13.5.sp, fontWeight = FontWeight.SemiBold
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
internal fun WirelessAdbSettingsCard(model: NativePulseModel) {
    val c = LocalPulsePalette.current
    val scope = rememberCoroutineScope()
    val adb = remember(model.activity) { WirelessAdbController.get(model.activity) }
    var adbState by remember { mutableStateOf(adb.state) }
    var pairingCode by remember { mutableStateOf("") }
    var pairingPort by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        while (isActive) {
            adbState = adb.state
            if (adbState.pairingPort > 0 && pairingPort.isBlank()) {
                pairingPort = adbState.pairingPort.toString()
            }
            delay(900L)
        }
    }

    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("免 Root 无线 ADB 虚拟屏 (uid 2000)", color = c.ink, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
                Text(
                    adbState.statusText.ifBlank { if (adbState.connected) "无线 ADB 已连接" else "在开发者选项打开「无线调试」并配对本机" },
                    color = if (adbState.connected) c.green else c.muted,
                    fontSize = 12.sp
                )
            }
            Text(
                when {
                    adbState.connected -> "已连接"
                    adbState.paired -> "已配对"
                    adbState.listening -> "搜寻中"
                    else -> "未配对"
                },
                color = if (adbState.connected) c.green else if (adbState.paired) c.accent else c.amber,
                fontSize = 12.sp,
                fontWeight = FontWeight.SemiBold
            )
        }

        if (adbState.lastError.isNotBlank()) {
            Text(adbState.lastError, color = Color(0xFFFF453A), fontSize = 11.5.sp)
        }

        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Box(
                Modifier.weight(1f)
                    .clip(RoundedCornerShape(10.dp))
                    .background(if (adbState.listening) c.fill else c.accent)
                    .iosPressScale(0.96f) {
                        if (adbState.listening) adb.stopDiscoveryAndCancelNotification()
                        else adb.startDiscoveryAndShowNotification()
                        adbState = adb.state
                    }
                    .padding(vertical = 9.dp),
                contentAlignment = Alignment.Center
            ) {
                Text(
                    if (adbState.listening) "停止搜寻配对端口" else "开始无线配对通知",
                    color = if (adbState.listening) c.ink else Color.White,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.SemiBold
                )
            }
            Box(
                Modifier.weight(1f)
                    .clip(RoundedCornerShape(10.dp))
                    .background(c.fill)
                    .iosPressScale(0.96f) { model.connectRootVirtualDisplay() }
                    .padding(vertical = 9.dp),
                contentAlignment = Alignment.Center
            ) {
                Text("连接 ADB 虚拟屏", color = c.ink, fontSize = 12.sp, fontWeight = FontWeight.Medium)
            }
        }

        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.Bottom) {
            Box(Modifier.weight(1f)) {
                PulseInputField(
                    label = "6 位配对码",
                    value = pairingCode,
                    placeholder = "123456",
                    onChange = { pairingCode = it.filter { ch -> ch.isDigit() }.take(6) }
                )
            }
            Box(Modifier.weight(1f)) {
                PulseInputField(
                    label = "配对端口",
                    value = pairingPort,
                    placeholder = if (adbState.pairingPort > 0) adbState.pairingPort.toString() else "例如 37123",
                    onChange = { pairingPort = it.filter { ch -> ch.isDigit() }.take(5) }
                )
            }
            Box(
                Modifier.clip(RoundedCornerShape(10.dp))
                    .background(if (busy) c.fill else c.accent)
                    .iosPressScale(0.96f) {
                        val port = pairingPort.toIntOrNull() ?: adbState.pairingPort
                        if (pairingCode.length < 6 || port <= 0) {
                            model.toast("请输入 6 位配对码与有效端口")
                            return@iosPressScale
                        }
                        busy = true
                        adb.submitPairingCodeAsync(pairingCode, "127.0.0.1", port) {
                            scope.launch {
                                busy = false
                                adbState = adb.state
                                if (adbState.paired) {
                                    model.toast("无线 ADB 配对成功，正在连接…")
                                    model.connectRootVirtualDisplay()
                                }
                            }
                        }
                    }
                    .padding(horizontal = 14.dp, vertical = 11.dp),
                contentAlignment = Alignment.Center
            ) {
                Text(if (busy) "配对中" else "配对", color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
            }
        }
    }
}
