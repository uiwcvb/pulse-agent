package dev.bubuki.proactive

import android.content.ClipData
import android.content.ClipboardManager
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import kotlinx.coroutines.launch

@Composable
internal fun LocalBootstrapWizardOverlay(
    model: NativePulseModel,
    initialBaseUrl: String,
    initialApiKey: String,
    initialModel: String,
    startWithSshDialog: Boolean = false,
    onSwitchToPairExisting: () -> Unit,
    onDismiss: () -> Unit
) {
    val c = LocalPulsePalette.current
    val scope = rememberCoroutineScope()
    val haptics = LocalHapticFeedback.current
    val shizuku = remember(model.activity) { ShizukuController.get(model.activity) }
    val shizukuState by shizuku.getStateFlow().collectAsState()
    val listState = rememberLazyListState()

    var showSshDialog by remember { mutableStateOf(startWithSshDialog) }
    var showShizukuSheet by remember { mutableStateOf(false) }
    var deployedResult by remember { mutableStateOf<VpsDeployResult?>(null) }
    var sending by remember { mutableStateOf(false) }
    var draft by remember { mutableStateOf("") }

    val messages = remember {
        mutableStateListOf(
            BootstrapChatMessage(
                role = "assistant",
                content = buildString {
                    appendLine("接口已经验通啦，当前直连模型 `${initialModel.ifBlank { "deepseek-chat" }}`！")
                    appendLine()
                    appendLine("接下来我带你把整套系统搭好，全程只需 3 步：")
                    appendLine("1. **搭起云端大脑 + 24/7 云桌面**：点下方「填 VPS SSH 一键搭建」，把一台空白 Ubuntu/Debian VPS 的 SSH 填进加密卡片（**凭据只在本机直连 SSH，绝不发给大模型**），余会自动把中枢和云端桌面一次装齐；")
                    appendLine("2. **手机免 Root 控屏**：打开系统无线调试启动 Shizuku 后点一键授权（切 5G 流量不断线）；")
                    appendLine("3. **用启动 Skill 自动配好电脑**：云端就绪后会生成一段指令，发给你电脑上的 AI，电脑会自动配对上线。")
                    appendLine()
                    append("有什么问题直接问我，或者现在点下面按钮开始 👇")
                },
                actions = listOf("OPEN_SSH_DEPLOY", "OPEN_SHIZUKU", "OPEN_PAIR_EXISTING")
            )
        )
    }

    LaunchedEffect(messages.size) {
        shizuku.refreshState()
        if (messages.isNotEmpty()) {
            runCatching { listState.animateScrollToItem(messages.size - 1) }
        }
    }

    fun copyToClipboard(label: String, text: String) {
        val cm = model.activity.getSystemService(ClipboardManager::class.java)
        cm?.setPrimaryClip(ClipData.newPlainText(label, text))
        haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
        model.toast("已复制$label")
    }

    fun sendQuestion(questionText: String) {
        val clean = questionText.trim()
        if (clean.isEmpty() || sending) return
        draft = ""
        messages.add(BootstrapChatMessage("user", clean))
        sending = true
        scope.launch {
            try {
                val reply = VpsBootstrapEngine.callBootstrapLlm(
                    baseUrl = initialBaseUrl,
                    apiKey = initialApiKey,
                    modelName = initialModel,
                    history = messages.toList(),
                    deployedResult = deployedResult,
                    shizukuState = shizukuState
                )
                messages.add(reply)
            } catch (e: Exception) {
                messages.add(
                    BootstrapChatMessage(
                        role = "assistant",
                        content = "直连模型回复失败：${PulseErrors.safeError(e)}。你也可以直接点击下方安全卡片继续搭建。",
                        actions = listOf("OPEN_SSH_DEPLOY", "OPEN_SHIZUKU")
                    )
                )
            } finally {
                sending = false
            }
        }
    }

    Box(
        Modifier
            .fillMaxSize()
            .background(c.bg)
            .windowInsetsPadding(WindowInsets.systemBars.union(WindowInsets.ime))
    ) {
        Column(Modifier.fillMaxSize()) {
            Surface(color = c.surface.copy(alpha = 0.96f), tonalElevation = 0.dp) {
                Column {
                    Row(
                        Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        PulseRoundButton("close", "关闭向导", {
                        val done = deployedResult
                        if (done?.ok == true) {
                            model.connectServer(done.serverUrl, done.deviceToken)
                        }
                        onDismiss()
                    }, background = true)
                        Column(Modifier.padding(start = 10.dp).weight(1f)) {
                            Text("AI 装机向导（仅 API Key 直连）", color = c.ink, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                            Text(
                                if (deployedResult?.ok == true) "云端与云桌面已就绪 · 可复制电脑接入指令"
                                else "尚未连接中枢 · 准备一键搭建云端或配对现有服务",
                                color = if (deployedResult?.ok == true) c.green else c.muted,
                                fontSize = 11.5.sp
                            )
                        }
                        Box(
                            Modifier.clip(RoundedCornerShape(10.dp))
                                .background(c.accent.copy(alpha = 0.14f))
                                .iosPressScale(0.95f) { showSshDialog = true }
                                .padding(horizontal = 10.dp, vertical = 6.dp)
                        ) {
                            Text("SSH 搭建", color = c.accent, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                        }
                    }
                    HorizontalDivider(color = c.line.copy(alpha = 0.5f), thickness = 0.5.dp)
                }
            }

            LazyColumn(
                state = listState,
                modifier = Modifier.weight(1f).fillMaxWidth().padding(horizontal = 14.dp),
                contentPadding = PaddingValues(vertical = 12.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                val res = deployedResult
                if (res?.ok == true) {
                    item(key = "deploy_done_card") {
                        DeployedCompletionCard(
                            res = res,
                            onCopyPrompt = { copyToClipboard("电脑 AI 启动 Skill 指令", res.pcAiPrompt) },
                            onCopyCli = { copyToClipboard("电脑 CLI 命令", res.pcCliCmd) },
                            onOpenShizuku = { showShizukuSheet = true },
                            onEnterWorkspace = {
                                model.connectServer(res.serverUrl, res.deviceToken)
                                onDismiss()
                            }
                        )
                    }
                }

                items(messages) { msg ->
                    val isUser = msg.role == "user"
                    Column(
                        Modifier.fillMaxWidth(),
                        horizontalAlignment = if (isUser) Alignment.End else Alignment.Start
                    ) {
                        Box(
                            Modifier
                                .widthIn(max = 330.dp)
                                .clip(RoundedCornerShape(18.dp))
                                .background(if (isUser) c.userBubble else c.aiBubble)
                                .border(
                                    0.5.dp,
                                    if (isUser) Color.Transparent else c.line.copy(alpha = 0.45f),
                                    RoundedCornerShape(18.dp)
                                )
                                .padding(horizontal = 13.dp, vertical = 10.dp)
                        ) {
                            Text(
                                msg.content,
                                color = if (isUser) c.userInk else c.ink,
                                fontSize = 14.sp,
                                lineHeight = 20.sp
                            )
                        }
                        if (!isUser && msg.actions.isNotEmpty()) {
                            Spacer(Modifier.height(6.dp))
                            Row(
                                Modifier.horizontalScroll(rememberScrollState()),
                                horizontalArrangement = Arrangement.spacedBy(6.dp)
                            ) {
                                msg.actions.forEach { act ->
                                    val label = when (act) {
                                        "OPEN_SSH_DEPLOY" -> "填 VPS SSH 一键搭建"
                                        "OPEN_SHIZUKU" -> "配置免 Root Shizuku"
                                        "OPEN_PAIR_EXISTING" -> "已有中枢去配对"
                                        "COPY_PC_SKILL" -> "复制电脑 Skill 指令"
                                        else -> act
                                    }
                                    val primary = act == "OPEN_SSH_DEPLOY" || act == "COPY_PC_SKILL"
                                    Box(
                                        Modifier.clip(RoundedCornerShape(10.dp))
                                            .background(if (primary) c.accent else c.surface)
                                            .border(0.5.dp, c.line.copy(alpha = 0.4f), RoundedCornerShape(10.dp))
                                            .iosPressScale(0.95f) {
                                                when (act) {
                                                    "OPEN_SSH_DEPLOY" -> showSshDialog = true
                                                    "OPEN_SHIZUKU" -> showShizukuSheet = true
                                                    "OPEN_PAIR_EXISTING" -> {
                                                        onDismiss()
                                                        onSwitchToPairExisting()
                                                    }
                                                    "COPY_PC_SKILL" -> {
                                                        val prompt = deployedResult?.pcAiPrompt
                                                        if (!prompt.isNullOrBlank()) {
                                                            copyToClipboard("电脑 AI 启动 Skill 指令", prompt)
                                                        } else {
                                                            model.toast("请先完成云端部署以生成电脑配对指令")
                                                        }
                                                    }
                                                }
                                            }
                                            .padding(horizontal = 11.dp, vertical = 7.dp)
                                    ) {
                                        Text(
                                            label,
                                            color = if (primary) Color.White else c.ink,
                                            fontSize = 12.sp,
                                            fontWeight = FontWeight.SemiBold
                                        )
                                    }
                                }
                            }
                        }
                    }
                }

                if (sending) {
                    item(key = "typing") {
                        Row(
                            Modifier.clip(RoundedCornerShape(16.dp)).background(c.aiBubble).padding(horizontal = 12.dp, vertical = 8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp, color = c.accent)
                            Text("正在思考…", color = c.muted, fontSize = 12.5.sp)
                        }
                    }
                }
            }

            Surface(color = c.surface, tonalElevation = 0.dp) {
                Column {
                    HorizontalDivider(color = c.line.copy(alpha = 0.5f), thickness = 0.5.dp)
                    Row(
                        Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Box(
                            Modifier.weight(1f)
                                .clip(RoundedCornerShape(18.dp))
                                .background(c.fill)
                                .padding(horizontal = 14.dp, vertical = 10.dp)
                        ) {
                            if (draft.isEmpty()) {
                                Text("问装机问题（勿在聊天里发 SSH 密码）…", color = c.faint, fontSize = 13.5.sp)
                            }
                            BasicTextField(
                                value = draft,
                                onValueChange = { draft = it },
                                textStyle = TextStyle(color = c.ink, fontSize = 14.sp),
                                cursorBrush = SolidColor(c.accent),
                                modifier = Modifier.fillMaxWidth()
                            )
                        }
                        Box(
                            Modifier.clip(CircleShape)
                                .background(if (draft.isNotBlank() && !sending) c.accent else c.fill)
                                .iosPressScale(0.92f, enabled = draft.isNotBlank() && !sending) { sendQuestion(draft) }
                                .padding(horizontal = 14.dp, vertical = 9.dp)
                        ) {
                            Text(
                                "发送",
                                color = if (draft.isNotBlank() && !sending) Color.White else c.faint,
                                fontSize = 13.sp,
                                fontWeight = FontWeight.SemiBold
                            )
                        }
                    }
                }
            }
        }
    }

    if (showSshDialog) {
        SecureSshDeployDialog(
            apiBaseUrl = initialBaseUrl,
            apiKey = initialApiKey,
            defaultModel = initialModel,
            onSuccess = { res ->
                deployedResult = res
                showSshDialog = false
                messages.add(
                    BootstrapChatMessage(
                        role = "assistant",
                        content = buildString {
                            appendLine("搞定！你的云端大脑已经在 `${res.serverUrl}` 上线，API Key 已同步完成。")
                            if (res.cloudDesktopReady) appendLine("24/7 云端桌面（Xvfb + Chromium + 终端）也已就绪并绑定到中枢。")
                            appendLine("接下来只需把置顶卡片里的 **启动 Skill 指令** 复制发给你电脑上的 AI，电脑就会自动接入，然后点卡片右下角「进入 Pulse Agent」！")
                        },
                        actions = listOf("COPY_PC_SKILL", "OPEN_SHIZUKU")
                    )
                )
            },
            onDismiss = { showSshDialog = false }
        )
    }

    if (showShizukuSheet) {
        Dialog(onDismissRequest = { showShizukuSheet = false }, properties = DialogProperties(usePlatformDefaultWidth = false)) {
            Box(
                Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.55f)).padding(20.dp),
                contentAlignment = Alignment.Center
            ) {
                Column(
                    Modifier.fillMaxWidth().widthIn(max = 440.dp).clip(RoundedCornerShape(20.dp)).background(c.bg).padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Text("手机免 Root · Shizuku 权限配置", Modifier.weight(1f), color = c.ink, fontSize = 16.sp, fontWeight = FontWeight.Bold)
                        PulseRoundButton("close", "关闭", { showShizukuSheet = false }, background = true)
                    }
                    ShizukuSettingsCard(model)
                    WirelessAdbSettingsCard(model)
                }
            }
        }
    }
}

@Composable
private fun DeployedCompletionCard(
    res: VpsDeployResult,
    onCopyPrompt: () -> Unit,
    onCopyCli: () -> Unit,
    onOpenShizuku: () -> Unit,
    onEnterWorkspace: () -> Unit
) {
    val c = LocalPulsePalette.current
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(18.dp))
            .background(c.surface)
            .border(0.8.dp, c.green.copy(alpha = 0.45f), RoundedCornerShape(18.dp))
            .padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        Text("云端中枢与工作站部署完成", color = c.green, fontSize = 15.sp, fontWeight = FontWeight.Bold)
        Text(
            buildString {
                append("• 中枢地址：${res.serverUrl}\n")
                append("• 24/7 云端桌面：${if (res.cloudDesktopReady) "已上线并自动绑定" else "未勾选或待检查"}\n")
                if (!res.publicReachable) append("• 注意：若 80 端口未在云厂商安全组放行，请放通 TCP 80/443 后点击下方进入。")
            }.trimEnd(),
            color = c.muted,
            fontSize = 12.sp,
            lineHeight = 18.sp
        )
        Box(
            Modifier.fillMaxWidth()
                .clip(RoundedCornerShape(12.dp))
                .background(c.fill)
                .padding(10.dp)
        ) {
            Text(
                res.pcAiPrompt,
                color = c.ink,
                fontSize = 11.5.sp,
                lineHeight = 16.sp,
                fontFamily = FontFamily.Monospace
            )
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Box(
                Modifier.weight(1f).clip(RoundedCornerShape(11.dp)).background(c.accent)
                    .iosPressScale(0.96f, onClick = onCopyPrompt).padding(vertical = 9.dp),
                contentAlignment = Alignment.Center
            ) {
                Text("复制发给电脑 AI", color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
            }
            Box(
                Modifier.weight(1f).clip(RoundedCornerShape(11.dp)).background(c.fill)
                    .iosPressScale(0.96f, onClick = onCopyCli).padding(vertical = 9.dp),
                contentAlignment = Alignment.Center
            ) {
                Text("复制单行 CLI", color = c.ink, fontSize = 12.sp, fontWeight = FontWeight.Medium)
            }
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Box(
                Modifier.weight(1f).clip(RoundedCornerShape(11.dp)).background(c.fill)
                    .iosPressScale(0.96f, onClick = onOpenShizuku).padding(vertical = 10.dp),
                contentAlignment = Alignment.Center
            ) {
                Text("授权手机 Shizuku", color = c.ink, fontSize = 12.5.sp, fontWeight = FontWeight.Medium)
            }
            Box(
                Modifier.weight(1.4f).clip(RoundedCornerShape(11.dp)).background(c.green)
                    .iosPressScale(0.96f, onClick = onEnterWorkspace).padding(vertical = 10.dp),
                contentAlignment = Alignment.Center
            ) {
                Text("进入 Pulse Agent", color = Color.White, fontSize = 12.5.sp, fontWeight = FontWeight.SemiBold)
            }
        }
    }
}

@Composable
private fun SecureSshDeployDialog(
    apiBaseUrl: String,
    apiKey: String,
    defaultModel: String,
    onSuccess: (VpsDeployResult) -> Unit,
    onDismiss: () -> Unit
) {
    val c = LocalPulsePalette.current
    val scope = rememberCoroutineScope()
    var host by remember { mutableStateOf("") }
    var portStr by remember { mutableStateOf("22") }
    var username by remember { mutableStateOf("root") }
    var useKeyAuth by remember { mutableStateOf(false) }
    var password by remember { mutableStateOf("") }
    var showPassword by remember { mutableStateOf(false) }
    var privateKey by remember { mutableStateOf("") }
    var installDesktop by remember { mutableStateOf(true) }
    var deploying by remember { mutableStateOf(false) }
    var progress by remember { mutableStateOf<VpsDeployProgress?>(null) }
    var deployErr by remember { mutableStateOf("") }

    Dialog(
        onDismissRequest = { if (!deploying) onDismiss() },
        properties = DialogProperties(usePlatformDefaultWidth = false, dismissOnClickOutside = !deploying)
    ) {
        Box(
            Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.62f)).padding(20.dp),
            contentAlignment = Alignment.Center
        ) {
            Column(
                Modifier.fillMaxWidth()
                    .widthIn(max = 460.dp)
                    .clip(RoundedCornerShape(22.dp))
                    .background(c.surface)
                    .border(0.5.dp, c.line.copy(alpha = 0.45f), RoundedCornerShape(22.dp))
                    .verticalScroll(rememberScrollState())
                    .padding(18.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("安全 SSH 一键搭建云端", color = c.ink, fontSize = 17.sp, fontWeight = FontWeight.Bold)
                        Text("凭据仅手机本地直连 SSH，绝不发给第三方模型", color = c.green, fontSize = 11.5.sp, fontWeight = FontWeight.Medium)
                    }
                    if (!deploying) {
                        PulseRoundButton("close", "关闭", onDismiss, background = true)
                    }
                }

                PulseInputField(
                    label = "VPS 公网 IP 或域名",
                    value = host,
                    placeholder = "例如 198.51.100.10 或 my.vps.example",
                    onChange = { if (!deploying) host = it }
                )

                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Box(Modifier.weight(1.4f)) {
                        PulseInputField(
                            label = "SSH 用户名",
                            value = username,
                            placeholder = "root",
                            onChange = { if (!deploying) username = it }
                        )
                    }
                    Box(Modifier.weight(1f)) {
                        PulseInputField(
                            label = "端口",
                            value = portStr,
                            placeholder = "22",
                            onChange = { if (!deploying) portStr = it.filter { ch -> ch.isDigit() }.take(5) }
                        )
                    }
                }

                Row(
                    Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(c.fill).padding(3.dp),
                    horizontalArrangement = Arrangement.spacedBy(4.dp)
                ) {
                    listOf(false to "SSH 密码", true to "SSH 私钥").forEach { (isKey, label) ->
                        val selected = useKeyAuth == isKey
                        Box(
                            Modifier.weight(1f).clip(RoundedCornerShape(8.dp))
                                .background(if (selected) c.surface else Color.Transparent)
                                .clickable(enabled = !deploying) { useKeyAuth = isKey }
                                .padding(vertical = 8.dp),
                            contentAlignment = Alignment.Center
                        ) {
                            Text(label, color = if (selected) c.ink else c.muted, fontSize = 12.5.sp, fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Medium)
                        }
                    }
                }

                if (!useKeyAuth) {
                    Column(Modifier.fillMaxWidth()) {
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                            Text("SSH 登录密码", Modifier.weight(1f), color = c.muted, fontSize = 12.sp)
                            Text(
                                if (showPassword) "隐藏" else "显示",
                                color = c.accent,
                                fontSize = 12.sp,
                                modifier = Modifier.clickable { showPassword = !showPassword }
                            )
                        }
                        Spacer(Modifier.height(5.dp))
                        Box(
                            Modifier.fillMaxWidth()
                                .clip(RoundedCornerShape(12.dp))
                                .background(c.fill)
                                .padding(horizontal = 12.dp, vertical = 11.dp)
                        ) {
                            if (password.isEmpty()) {
                                Text("输入服务器登录密码", color = c.faint, fontSize = 13.5.sp)
                            }
                            BasicTextField(
                                value = password,
                                onValueChange = { if (!deploying) password = it },
                                singleLine = true,
                                visualTransformation = if (showPassword) VisualTransformation.None else PasswordVisualTransformation(),
                                textStyle = TextStyle(color = c.ink, fontSize = 14.sp),
                                cursorBrush = SolidColor(c.accent),
                                modifier = Modifier.fillMaxWidth()
                            )
                        }
                    }
                } else {
                    PulseInputField(
                        label = "SSH 私钥 (OpenSSH / PEM 原文)",
                        value = privateKey,
                        placeholder = "-----BEGIN OPENSSH PRIVATE KEY----- ...",
                        singleLine = false,
                        onChange = { if (!deploying) privateKey = it }
                    )
                }

                Row(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(12.dp))
                        .background(c.fill.copy(alpha = 0.7f))
                        .clickable(enabled = !deploying) { installDesktop = !installDesktop }
                        .padding(horizontal = 12.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(Modifier.weight(1f)) {
                        Text("同时搭建 24/7 云端桌面（推荐）", color = c.ink, fontSize = 13.5.sp, fontWeight = FontWeight.Medium)
                        Text("自动安装 Xvfb + Openbox + Chromium + Tmux 并绑定中枢", color = c.muted, fontSize = 11.sp)
                    }
                    Text(
                        if (installDesktop) "已开启" else "仅装大脑",
                        color = if (installDesktop) c.green else c.muted,
                        fontSize = 12.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                }

                AnimatedVisibility(visible = progress != null) {
                    val p = progress
                    if (p != null) {
                        Column(
                            Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(c.fill).padding(12.dp),
                            verticalArrangement = Arrangement.spacedBy(6.dp)
                        ) {
                            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                if (deploying) CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp, color = c.accent)
                                Text("步骤 ${p.step}/${p.totalSteps} · ${p.title}", color = c.ink, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                            }
                            if (p.logLine.isNotBlank()) {
                                Text(p.logLine, color = c.muted, fontSize = 11.5.sp)
                            }
                        }
                    }
                }

                if (deployErr.isNotBlank()) {
                    Text(
                        deployErr,
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(c.warn.copy(alpha = 0.12f)).padding(10.dp),
                        color = c.warn,
                        fontSize = 12.sp
                    )
                }

                Box(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(13.dp))
                        .background(if (deploying) c.fill else c.accent)
                        .iosPressScale(0.97f, enabled = !deploying) {
                            deployErr = ""
                            deploying = true
                            val params = VpsDeployParams(
                                host = host.trim(),
                                port = portStr.toIntOrNull() ?: 22,
                                username = username.trim().ifBlank { "root" },
                                password = if (useKeyAuth) "" else password,
                                privateKey = if (useKeyAuth) privateKey else "",
                                apiBaseUrl = apiBaseUrl,
                                apiKey = apiKey,
                                defaultModel = defaultModel,
                                installCloudDesktop = installDesktop
                            )
                            scope.launch {
                                val res = VpsBootstrapEngine.deployVpsOverSsh(params) { prog ->
                                    progress = prog
                                }
                                password = ""
                                privateKey = ""
                                deploying = false
                                if (res.ok) {
                                    onSuccess(res)
                                } else {
                                    deployErr = res.error.ifBlank { "SSH 部署失败" }
                                }
                            }
                        }
                        .padding(vertical = 12.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Text(
                        if (deploying) "正在自动部署 VPS，请保持屏幕常亮…" else "开始一键搭建中枢 + 云桌面",
                        color = if (deploying) c.muted else Color.White,
                        fontSize = 14.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                }
            }
        }
    }
}
