package dev.bubuki.proactive

import android.util.Base64
import com.jcraft.jsch.ChannelExec
import com.jcraft.jsch.JSch
import com.jcraft.jsch.Session
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.nio.charset.StandardCharsets
import java.util.Properties
import java.util.concurrent.TimeUnit

internal data class BootstrapProviderPreset(
    val id: String,
    val label: String,
    val baseUrl: String,
    val defaultModel: String,
    val hint: String
)

internal data class BootstrapChatMessage(
    val role: String,
    val content: String,
    val actions: List<String> = emptyList()
)

internal data class VpsDeployParams(
    val host: String,
    val port: Int = 22,
    val username: String = "root",
    val password: String = "",
    val privateKey: String = "",
    val passphrase: String = "",
    val apiBaseUrl: String,
    val apiKey: String,
    val defaultModel: String,
    val installCloudDesktop: Boolean = true,
    val repoUrl: String = "https://github.com/uiwcvb/proactive-personal-agent.git"
)

internal data class VpsDeployProgress(
    val step: Int,
    val totalSteps: Int,
    val title: String,
    val logLine: String = ""
)

internal data class VpsDeployResult(
    val ok: Boolean,
    val serverUrl: String = "",
    val deviceToken: String = "",
    val pairCode: String = "",
    val pcAiPrompt: String = "",
    val pcCliCmd: String = "",
    val cloudDesktopReady: Boolean = false,
    val publicReachable: Boolean = false,
    val error: String = ""
)

internal object VpsBootstrapEngine {
    val PROVIDER_PRESETS = listOf(
        BootstrapProviderPreset("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat", "高性价比日常+推理"),
        BootstrapProviderPreset("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "deepseek/deepseek-chat-v3-0324", "多模型聚合网关"),
        BootstrapProviderPreset("siliconflow", "硅基流动", "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3", "国内高速节点"),
        BootstrapProviderPreset("gemini", "Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", "Google 多模态"),
        BootstrapProviderPreset("openai", "OpenAI", "https://api.openai.com/v1", "gpt-4.1-mini", "官方接口"),
        BootstrapProviderPreset("custom", "自定义", "", "", "任意 OpenAI 兼容接口")
    )

    private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()
    private val ACTION_TAG_REGEX = Regex("\\[\\[ACTION:([A-Z0-9_]+)\\]\\]")

    fun inferPresetForKey(apiKey: String): BootstrapProviderPreset? {
        val k = apiKey.trim()
        return when {
            k.startsWith("sk-or-") -> PROVIDER_PRESETS.first { it.id == "openrouter" }
            k.startsWith("AIza") -> PROVIDER_PRESETS.first { it.id == "gemini" }
            else -> null
        }
    }

    fun parsePairCode(raw: String): Pair<String, String>? {
        var clean = raw.trim()
        if (clean.isEmpty()) return null
        if (clean.startsWith("DSCLAW1:", ignoreCase = true)) {
            clean = clean.substring("DSCLAW1:".length).trim()
        }
        val sep = clean.lastIndexOf('|')
        if (sep <= 0 || sep >= clean.length - 1) return null
        val url = clean.substring(0, sep).trim().trimEnd('/')
        val tok = clean.substring(sep + 1).trim()
        if (!url.startsWith("http://") && !url.startsWith("https://")) return null
        if (tok.isEmpty()) return null
        return url to tok
    }

    fun extractActions(rawText: String): Pair<String, List<String>> {
        val actions = mutableListOf<String>()
        val cleaned = ACTION_TAG_REGEX.replace(rawText) { m ->
            val act = m.groupValues[1].trim()
            if (act.isNotEmpty() && act !in actions) actions.add(act)
            ""
        }.trim()
        return cleaned to actions
    }

    suspend fun verifyApiKeyDirect(
        baseUrl: String,
        apiKey: String,
        preferredModel: String
    ): Triple<Boolean, String, String> = withContext(Dispatchers.IO) {
        val cleanBase = baseUrl.trim().trimEnd('/')
        val cleanKey = apiKey.trim()
        val cleanModel = preferredModel.trim()
        if (!cleanBase.startsWith("http://") && !cleanBase.startsWith("https://")) {
            return@withContext Triple(false, cleanModel, "请填写有效的 API Base URL")
        }
        if (cleanKey.isBlank()) {
            return@withContext Triple(false, cleanModel, "请先填写 API Key")
        }
        val client = SecureTransport.HTTP.newBuilder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(25, TimeUnit.SECONDS)
            .callTimeout(30, TimeUnit.SECONDS)
            .build()
        var resolvedModel = cleanModel
        try {
            val modReq = Request.Builder()
                .url("$cleanBase/models")
                .header("Authorization", "Bearer $cleanKey")
                .get()
                .build()
            client.newCall(modReq).execute().use { resp ->
                val txt = resp.body?.string().orEmpty()
                if (resp.isSuccessful) {
                    val data = runCatching { JSONObject(txt).optJSONArray("data") }.getOrNull()
                    if (resolvedModel.isBlank() && data != null && data.length() > 0) {
                        resolvedModel = data.optJSONObject(0)?.optString("id", "").orEmpty()
                    }
                    if (resolvedModel.isBlank()) resolvedModel = "deepseek-chat"
                    return@withContext Triple(true, resolvedModel, "接口验证通过，已就绪 ($resolvedModel)")
                }
                if (resp.code == 401 || resp.code == 403) {
                    return@withContext Triple(false, resolvedModel, "API Key 无效或无权限 (HTTP ${resp.code})")
                }
            }
        } catch (_: Exception) {
        }

        val testModel = resolvedModel.ifBlank { "deepseek-chat" }
        try {
            val bodyJson = JSONObject()
                .put("model", testModel)
                .put("max_tokens", 8)
                .put(
                    "messages",
                    JSONArray().put(JSONObject().put("role", "user").put("content", "ping"))
                )
            val chatReq = Request.Builder()
                .url("$cleanBase/chat/completions")
                .header("Authorization", "Bearer $cleanKey")
                .post(bodyJson.toString().toRequestBody(JSON_MEDIA))
                .build()
            client.newCall(chatReq).execute().use { resp ->
                val raw = resp.body?.string().orEmpty()
                if (resp.isSuccessful) {
                    return@withContext Triple(true, testModel, "模型验证通过 ($testModel)")
                }
                val errMsg = runCatching {
                    JSONObject(raw).optJSONObject("error")?.optString("message").orEmpty()
                }.getOrDefault("")
                Triple(false, testModel, errMsg.ifBlank { "验证失败 HTTP ${resp.code}" })
            }
        } catch (e: Exception) {
            Triple(false, testModel, PulseErrors.safeError(e))
        }
    }

    suspend fun callBootstrapLlm(
        baseUrl: String,
        apiKey: String,
        modelName: String,
        history: List<BootstrapChatMessage>,
        deployedResult: VpsDeployResult?,
        shizukuState: ShizukuController.State
    ): BootstrapChatMessage = withContext(Dispatchers.IO) {
        val cleanBase = baseUrl.trim().trimEnd('/')
        val cleanKey = apiKey.trim()
        val sysPrompt = buildString {
            appendLine("你是 Pulse Agent 的零门槛装机与全端配置向导，聪明利落、友善清楚（默认简体中文）。")
            appendLine("当前用户只有 API Key，正在通过手机本地直连向你咨询并完成整套部署：")
            appendLine("1) 给 AI 提供空白 Linux VPS 的 SSH，自动搭建云端中枢大脑 + 24/7 隔离云端桌面 (Xvfb + Chromium + Tmux)；")
            appendLine("2) 手机免 Root 推荐通过 Shizuku 无线调试启动（关掉 Wi-Fi 切 5G 流量不断连），或使用内置无线 ADB；")
            appendLine("3) 云端就绪后，复制一条附带 `/bootstrap/pc-skill.md` 启动 Skill 和 `DSCLAW1:` 配对码的指令发给电脑上的 AI（Claude Code / Codex / Antigravity / Cursor），由电脑 AI 自动配好电脑端。")
            appendLine()
            appendLine("【铁律】")
            appendLine("- 严禁让用户在聊天框里发送 SSH 密码或 SSH 私钥正文！一旦需要填 SSH，请明确告诉用户点击下方安全表单按钮，并在回复末尾附加 `[[ACTION:OPEN_SSH_DEPLOY]]`（系统会在卡片里直接完成加密 SSH 部署，凭据绝不发给第三方模型）。")
            appendLine("- 如果用户需要配置手机免 Root 权限，说明 Shizuku 无线启动步骤并附加 `[[ACTION:OPEN_SHIZUKU]]`。")
            appendLine("- 如果用户已有中枢服务器地址和配对码，可附加 `[[ACTION:OPEN_PAIR_EXISTING]]`。")
            if (deployedResult?.ok == true) {
                appendLine("- 当前状态：VPS 云端大脑与云桌面已部署成功！地址 `${deployedResult.serverUrl}`，电脑端接入指令已生成，若用户问电脑怎么连，附加 `[[ACTION:COPY_PC_SKILL]]`。")
            } else {
                appendLine("- 当前状态：云端中枢尚未搭建。")
            }
            appendLine(
                "- 手机 Shizuku 状态：installed=${shizukuState.installed}, running=${shizukuState.binderAlive}, granted=${shizukuState.granted}"
            )
        }
        val messagesJson = JSONArray()
            .put(JSONObject().put("role", "system").put("content", sysPrompt))
        for (m in history.takeLast(14)) {
            messagesJson.put(JSONObject().put("role", m.role).put("content", m.content))
        }
        val payload = JSONObject()
            .put("model", modelName.ifBlank { "deepseek-chat" })
            .put("messages", messagesJson)
            .put("temperature", 0.5)
        val client = SecureTransport.HTTP.newBuilder()
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .callTimeout(75, TimeUnit.SECONDS)
            .build()
        val req = Request.Builder()
            .url("$cleanBase/chat/completions")
            .header("Authorization", "Bearer $cleanKey")
            .post(payload.toString().toRequestBody(JSON_MEDIA))
            .build()
        client.newCall(req).execute().use { resp ->
            val raw = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                val detail = runCatching {
                    JSONObject(raw).optJSONObject("error")?.optString("message").orEmpty()
                }.getOrDefault("")
                throw Exception(detail.ifBlank { "LLM HTTP ${resp.code}" })
            }
            val obj = JSONObject(raw)
            val content = obj.optJSONArray("choices")
                ?.optJSONObject(0)
                ?.optJSONObject("message")
                ?.optString("content", "")
                .orEmpty()
                .trim()
            val (cleanText, actions) = extractActions(content)
            BootstrapChatMessage(role = "assistant", content = cleanText, actions = actions)
        }
    }

    suspend fun deployVpsOverSsh(
        params: VpsDeployParams,
        onProgress: (VpsDeployProgress) -> Unit
    ): VpsDeployResult = withContext(Dispatchers.IO) {
        val cleanHost = params.host.trim()
        val cleanUser = params.username.trim().ifBlank { "root" }
        val cleanPort = params.port.coerceIn(1, 65535)
        if (cleanHost.isEmpty()) {
            return@withContext VpsDeployResult(ok = false, error = "请填写 VPS 服务器 IP 或域名")
        }
        if (params.password.isEmpty() && params.privateKey.isBlank()) {
            return@withContext VpsDeployResult(ok = false, error = "请填写 SSH 登录密码或私钥")
        }

        val totalSteps = if (params.installCloudDesktop) 6 else 5
        var session: Session? = null
        try {
            onProgress(VpsDeployProgress(1, totalSteps, "连接 VPS ($cleanUser@$cleanHost:$cleanPort)…"))
            val jsch = JSch()
            if (params.privateKey.isNotBlank()) {
                val privBytes = params.privateKey.trim().toByteArray(StandardCharsets.UTF_8)
                val passBytes = if (params.passphrase.isNotEmpty()) params.passphrase.toByteArray(StandardCharsets.UTF_8) else null
                jsch.addIdentity("pulse-user", privBytes, null, passBytes)
            }
            val sess = jsch.getSession(cleanUser, cleanHost, cleanPort)
            session = sess
            if (params.password.isNotEmpty()) {
                sess.setPassword(params.password.toByteArray(StandardCharsets.UTF_8))
            }
            val cfg = Properties()
            cfg["StrictHostKeyChecking"] = "no"
            cfg["PreferredAuthentications"] = "publickey,password,keyboard-interactive"
            sess.setConfig(cfg)
            sess.connect(20_000)

            val sudo = if (cleanUser == "root") "" else "sudo -n "
            val preflightCmd = """
                set -eu
                ${sudo}true || { echo "NEED_ROOT_OR_PASSWORDLESS_SUDO"; exit 1; }
                command -v apt-get >/dev/null 2>&1 || { echo "UNSUPPORTED_DISTRO_NEED_APT"; exit 1; }
                ${sudo}sh -c '
                  MEM=$(free -m | awk "/^Mem:/{print \$2}")
                  SWP=$(swapon --show --noheadings | wc -l)
                  if [ "${'$'}{MEM:-0}" -lt 1900 ] && [ "${'$'}{SWP:-0}" -eq 0 ] && [ ! -f /swapfile ]; then
                    (fallocate -l 1536M /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=1536 status=none)
                    chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile || true
                  fi
                '
                echo "PREFLIGHT_OK"
            """.trimIndent()
            val (rc1, out1) = execSsh(sess, preflightCmd, 90_000)
            if (rc1 != 0 || !out1.contains("PREFLIGHT_OK")) {
                return@withContext VpsDeployResult(
                    ok = false,
                    error = when {
                        out1.contains("NEED_ROOT_OR_PASSWORDLESS_SUDO") -> "非 root 用户需要免密 sudo 权限，建议直接使用 root 登录"
                        out1.contains("UNSUPPORTED_DISTRO_NEED_APT") -> "目前一键部署仅支持 Ubuntu / Debian (apt-get) 系统"
                        else -> "VPS 预检失败: ${out1.takeLast(240)}"
                    }
                )
            }

            onProgress(VpsDeployProgress(2, totalSteps, "安装 Python、Nginx${if (params.installCloudDesktop) " 与 Xvfb 云桌面组件" else ""}…", "正在 apt-get 安装运行依赖（首次约 1–2 分钟）"))
            val desktopPkgs = if (params.installCloudDesktop) {
                "xvfb openbox xdotool scrot imagemagick dbus-x11 xauth xclip x11-utils fonts-noto-cjk tmux"
            } else ""
            val pkgCmd = """
                set -eu
                ${sudo}sh -c '
                  export DEBIAN_FRONTEND=noninteractive
                  apt-get update -y -qq
                  apt-get install -y -qq git python3 python3-venv python3-pip nginx openssl curl ca-certificates $desktopPkgs
                '
                echo "PKGS_OK"
            """.trimIndent()
            val (rc2, out2) = execSsh(sess, pkgCmd, 360_000)
            if (rc2 != 0 || !out2.contains("PKGS_OK")) {
                return@withContext VpsDeployResult(ok = false, error = "安装系统依赖失败: ${out2.takeLast(300)}")
            }

            onProgress(VpsDeployProgress(3, totalSteps, "拉取 Pulse Agent 源码并初始化 Python 环境…"))
            val repoB64 = Base64.encodeToString(params.repoUrl.trim().toByteArray(StandardCharsets.UTF_8), Base64.NO_WRAP)
            val cloneCmd = """
                set -eu
                ${sudo}sh -s <<'REMOTE_CLONE'
                set -eu
                REPO=$(printf '%s' '$repoB64' | base64 -d)
                install -d -m 755 /opt/proactive-agent /opt/proactive-agent/data
                if [ -d /opt/proactive-agent/src/.git ]; then
                  git -C /opt/proactive-agent/src pull --ff-only || true
                else
                  rm -rf /opt/proactive-agent/src
                  git clone --depth 1 "${'$'}REPO" /opt/proactive-agent/src
                fi
                rm -rf /opt/proactive-agent/server /opt/proactive-agent/.agents
                cp -r /opt/proactive-agent/src/server /opt/proactive-agent/server
                if [ -d /opt/proactive-agent/src/.agents ]; then cp -r /opt/proactive-agent/src/.agents /opt/proactive-agent/.agents; fi
                if ! id proactive-agent >/dev/null 2>&1; then useradd --system --home-dir /opt/proactive-agent/data --shell /usr/sbin/nologin proactive-agent; fi
                chown -R proactive-agent:proactive-agent /opt/proactive-agent/data
                chmod 700 /opt/proactive-agent/data
                if [ ! -s /opt/proactive-agent/data/device-token ]; then
                  openssl rand -hex 24 > /opt/proactive-agent/data/device-token
                  chown proactive-agent:proactive-agent /opt/proactive-agent/data/device-token
                  chmod 600 /opt/proactive-agent/data/device-token
                fi
                chmod -R a+rX /opt/proactive-agent/server /opt/proactive-agent/.agents || true
                if [ ! -x /opt/proactive-agent/venv/bin/python ]; then python3 -m venv /opt/proactive-agent/venv; fi
                /opt/proactive-agent/venv/bin/pip install --disable-pip-version-check -q -r /opt/proactive-agent/server/requirements.txt
                REMOTE_CLONE
                echo "HUB_VENV_OK"
            """.trimIndent()
            val (rc3, out3) = execSsh(sess, cloneCmd, 360_000)
            if (rc3 != 0 || !out3.contains("HUB_VENV_OK")) {
                return@withContext VpsDeployResult(ok = false, error = "拉取代码或安装 Python 依赖失败: ${out3.takeLast(320)}")
            }

            onProgress(VpsDeployProgress(4, totalSteps, "启动中枢大脑服务与 Nginx WebSocket 网关…"))
            val pubUrlB64 = Base64.encodeToString("http://$cleanHost/proactive-agent".toByteArray(StandardCharsets.UTF_8), Base64.NO_WRAP)
            val hostB64 = Base64.encodeToString(cleanHost.toByteArray(StandardCharsets.UTF_8), Base64.NO_WRAP)
            val hubStartCmd = """
                set -eu
                ${sudo}sh -s <<'REMOTE_HUB'
                set -eu
                PUB_URL=$(printf '%s' '$pubUrlB64' | base64 -d)
                HOST_VAL=$(printf '%s' '$hostB64' | base64 -d)
                cat > /etc/systemd/system/proactive-agent.service <<UNIT
[Unit]
Description=Proactive Personal Agent Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=proactive-agent
Group=proactive-agent
WorkingDirectory=/opt/proactive-agent/server
Environment=AGENT_DATA_DIR=/opt/proactive-agent/data
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=BRAIN_ENGINE=legacy
Environment=DS_CLAW_PUBLIC_URL=${'$'}PUB_URL
Environment=PUBLIC_BLOCKED_HOSTS=${'$'}HOST_VAL
ExecStart=/opt/proactive-agent/venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 18190 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1 --root-path /proactive-agent --no-access-log
Restart=on-failure
RestartSec=5
UMask=0077
MemoryMax=1280M

[Install]
WantedBy=multi-user.target
UNIT
                mkdir -p /etc/nginx/snippets
                cat > /etc/nginx/snippets/proactive-agent.conf <<'NGINX'
location = /proactive-agent { return 301 /proactive-agent/; }
location ^~ /proactive-agent/ {
    proxy_pass http://127.0.0.1:18190/;
    proxy_http_version 1.1;
    proxy_set_header Host ${'$'}host;
    proxy_set_header X-Real-IP ${'$'}remote_addr;
    proxy_set_header X-Forwarded-For ${'$'}proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto ${'$'}scheme;
    proxy_set_header Upgrade ${'$'}http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 180s;
    proxy_send_timeout 180s;
    proxy_buffering off;
    client_max_body_size 12m;
}
NGINX
                /opt/proactive-agent/venv/bin/python - <<'PY'
from pathlib import Path
import re
cands = [Path('/etc/nginx/sites-available/default')] + sorted(Path('/etc/nginx/conf.d').glob('*.conf'))
target = next((p for p in cands if p.is_file() and re.search(r'(?m)^\s*server\s*\{', p.read_text())), None)
if target is not None:
    txt = target.read_text()
    inc = '    include /etc/nginx/snippets/proactive-agent.conf;\n'
    if 'proactive-agent.conf' not in txt:
        m = re.search(r'(?m)^\s*server\s*\{', txt)
        if m:
            bak = Path('/opt/proactive-agent/nginx-' + target.name + '.bak')
            if not bak.exists():
                bak.write_text(txt)
            target.write_text(txt[:m.end()] + '\n' + inc + txt[m.end():])
PY
                nginx -t && systemctl enable --now nginx && systemctl reload nginx
                systemctl daemon-reload
                systemctl enable --now proactive-agent.service
                systemctl restart proactive-agent.service
                REMOTE_HUB
                for i in $(seq 1 20); do
                  curl -fsS http://127.0.0.1:18190/health >/dev/null 2>&1 && break
                  sleep 1
                done
                curl -fsS http://127.0.0.1:18190/health >/dev/null
                echo "HUB_LIVE_OK"
            """.trimIndent()
            val (rc4, out4) = execSsh(sess, hubStartCmd, 120_000)
            if (rc4 != 0 || !out4.contains("HUB_LIVE_OK")) {
                return@withContext VpsDeployResult(ok = false, error = "启动中枢或 Nginx 失败: ${out4.takeLast(320)}")
            }

            var cloudDesktopReady = false
            if (params.installCloudDesktop) {
                onProgress(VpsDeployProgress(5, totalSteps, "搭建 24/7 云端桌面 (Xvfb + Chromium + 终端)…", "首次安装 Playwright Chromium 可能需要 1–3 分钟"))
                val desktopCmd = """
                    set -eu
                    ${sudo}sh -c '
                      install -d -m 755 /opt/proactive-agent/cloud-computer/core /opt/proactive-agent/cloud-computer/desktop-home/workspace /opt/proactive-agent/cloud-computer/desktop-home/Downloads /opt/proactive-agent/browser-runtime /run/proactive-desktop
                      (id -u proactive-desktop >/dev/null 2>&1 || useradd -r -M -d /opt/proactive-agent/cloud-computer/desktop-home -s /bin/bash proactive-desktop)
                      for f in computer-openbox.xml computer_api.py computer_browser.py computer_desktop.py computer_files.py computer_models.py computer_requirements.txt computer_runtime.py computer_security.py computer_shell.py sandbox.py; do
                        if [ -f "/opt/proactive-agent/server/${'$'}f" ]; then cp "/opt/proactive-agent/server/${'$'}f" "/opt/proactive-agent/cloud-computer/${'$'}f"; fi
                      done
                      for cf in __init__.py search_engine.py; do
                        if [ -f "/opt/proactive-agent/server/core/${'$'}cf" ]; then cp "/opt/proactive-agent/server/core/${'$'}cf" "/opt/proactive-agent/cloud-computer/core/${'$'}cf"; fi
                      done
                      if [ -d /opt/proactive-agent/server/cloud_os ]; then
                        rm -rf /opt/proactive-agent/cloud-computer/cloud_os
                        cp -r /opt/proactive-agent/server/cloud_os /opt/proactive-agent/cloud-computer/cloud_os
                      fi
                      chown -R proactive-desktop:proactive-desktop /opt/proactive-agent/cloud-computer/desktop-home /run/proactive-desktop
                      chmod 0750 /opt/proactive-agent/cloud-computer/desktop-home /run/proactive-desktop
                      chmod -R a+rX /opt/proactive-agent/cloud-computer
                      if [ ! -x /opt/proactive-agent/desktop-venv/bin/python ]; then python3 -m venv /opt/proactive-agent/desktop-venv; fi
                      /opt/proactive-agent/desktop-venv/bin/pip install -q -r /opt/proactive-agent/cloud-computer/computer_requirements.txt
                      export PLAYWRIGHT_BROWSERS_PATH=/opt/proactive-agent/browser-runtime
                      (/opt/proactive-agent/desktop-venv/bin/python -m playwright install chromium --with-deps || /opt/proactive-agent/desktop-venv/bin/python -m playwright install chromium || true)
                      chmod -R a+rX /opt/proactive-agent/desktop-venv /opt/proactive-agent/browser-runtime
                      if [ ! -s /opt/proactive-agent/data/computer-token ]; then
                        openssl rand -hex 24 > /opt/proactive-agent/data/computer-token
                        chown proactive-desktop:proactive-desktop /opt/proactive-agent/data/computer-token
                        chmod 600 /opt/proactive-agent/data/computer-token
                      fi
                      cat > /etc/systemd/system/proactive-desktop.service <<UNIT
[Unit]
Description=Proactive Agent Isolated Cloud Desktop Service
After=network.target

[Service]
Type=simple
User=proactive-desktop
Group=proactive-desktop
RuntimeDirectory=proactive-desktop
RuntimeDirectoryMode=0750
WorkingDirectory=/opt/proactive-agent/cloud-computer
Environment=HOME=/opt/proactive-agent/cloud-computer/desktop-home
Environment=USER=proactive-desktop
Environment=XDG_RUNTIME_DIR=/run/proactive-desktop
Environment=LANG=C.UTF-8
Environment=LC_ALL=C.UTF-8
Environment=PYTHONPATH=/opt/proactive-agent/cloud-computer
Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/proactive-agent/browser-runtime
Environment=COMPUTER_XAUTH_EXECUTABLE=/usr/bin/xauth
Environment=COMPUTER_HOST=127.0.0.1
Environment=COMPUTER_PORT=18193
Environment=COMPUTER_TOKEN_FILE=/opt/proactive-agent/data/computer-token
MemoryMax=2560M
CPUQuota=200%
ExecStart=/opt/proactive-agent/desktop-venv/bin/python /opt/proactive-agent/cloud-computer/computer_runtime.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
                      systemctl daemon-reload
                      systemctl enable --now proactive-desktop.service
                      systemctl restart proactive-desktop.service
                    '
                    COMP_TOK=$(${sudo}cat /opt/proactive-agent/data/computer-token)
                    for i in $(seq 1 15); do
                      curl -sf -H "Authorization: Bearer ${'$'}COMP_TOK" http://127.0.0.1:18193/status >/dev/null 2>&1 && break
                      sleep 1
                    done
                    curl -sf -H "Authorization: Bearer ${'$'}COMP_TOK" http://127.0.0.1:18193/status >/dev/null
                    echo "CLOUD_DESKTOP_OK"
                """.trimIndent()
                val (rc5, out5) = execSsh(sess, desktopCmd, 480_000)
                cloudDesktopReady = rc5 == 0 && out5.contains("CLOUD_DESKTOP_OK")
            }

            onProgress(VpsDeployProgress(totalSteps, totalSteps, "同步 API Key 与云桌面凭证并验收…"))
            val apiCfgJson = JSONObject()
                .put("api_base_url", params.apiBaseUrl.trim().trimEnd('/'))
                .put("api_key", params.apiKey.trim())
                .put("default_model", params.defaultModel.trim().ifBlank { "deepseek-chat" })
            val apiCfgB64 = Base64.encodeToString(apiCfgJson.toString().toByteArray(StandardCharsets.UTF_8), Base64.NO_WRAP)
            val syncCmd = """
                set -eu
                DEV_TOK=$(${sudo}cat /opt/proactive-agent/data/device-token)
                COMP_TOK=$(${sudo}sh -c 'cat /opt/proactive-agent/data/computer-token 2>/dev/null || true')
                export DEV_TOK COMP_TOK
                /opt/proactive-agent/venv/bin/python - <<'PY'
import base64, json, os, urllib.request
patch = json.loads(base64.b64decode('$apiCfgB64').decode('utf-8'))
model = patch.get('default_model') or 'deepseek-chat'
patch['small_brain'] = {'base_url': patch['api_base_url'], 'api_key': patch['api_key'], 'model': model}
patch['big_brain'] = {'base_url': patch['api_base_url'], 'api_key': patch['api_key'], 'model': model}
comp_tok = os.environ.get('COMP_TOK', '').strip()
dev_tok = os.environ.get('DEV_TOK', '').strip()
if '$cloudDesktopReady'.lower() == 'true' and comp_tok:
    patch['cloud_computer_mode'] = 'http'
    patch['cloud_computer_url'] = 'http://127.0.0.1:18193'
    patch['cloud_computer_token'] = comp_tok
    patch['cloud_computer_status'] = 'ready'
req = urllib.request.Request(
    'http://127.0.0.1:18190/api/config',
    data=json.dumps(patch).encode('utf-8'),
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + dev_tok},
    method='POST',
)
with urllib.request.urlopen(req, timeout=20) as resp:
    assert resp.status == 200
print('__TOKEN_BEGIN__' + dev_tok + '__TOKEN_END__')
PY
            """.trimIndent()
            val (rc6, out6) = execSsh(sess, syncCmd, 60_000)
            val m = Regex("__TOKEN_BEGIN__([A-Za-z0-9_\\-]+)__TOKEN_END__").find(out6)
            val deviceToken = m?.groupValues?.get(1)?.trim().orEmpty()
            if (rc6 != 0 || deviceToken.isEmpty()) {
                return@withContext VpsDeployResult(ok = false, error = "同步远端配置失败: ${out6.takeLast(260)}")
            }

            val serverUrl = "http://$cleanHost/proactive-agent"
            val publicOk = checkPublicHealth(serverUrl)
            val pairCode = "DSCLAW1:$serverUrl|$deviceToken"
            val pcPrompt = "请读取 $serverUrl/bootstrap/pc-skill.md（或仓库内的 .agents/skills/pulse-pc-bootstrap/SKILL.md），按其中步骤把这台电脑接入我的 Pulse Agent，配对码：$pairCode"
            val pcCli = "python desktop/desktop_agent.py --pair-code \"$pairCode\" --save --check"
            VpsDeployResult(
                ok = true,
                serverUrl = serverUrl,
                deviceToken = deviceToken,
                pairCode = pairCode,
                pcAiPrompt = pcPrompt,
                pcCliCmd = pcCli,
                cloudDesktopReady = cloudDesktopReady,
                publicReachable = publicOk
            )
        } catch (e: Exception) {
            VpsDeployResult(ok = false, error = PulseErrors.safeError(e))
        } finally {
            runCatching { session?.disconnect() }
        }
    }

    private fun checkPublicHealth(serverUrl: String): Boolean {
        return try {
            val client = SecureTransport.HTTP.newBuilder()
                .connectTimeout(6, TimeUnit.SECONDS)
                .readTimeout(6, TimeUnit.SECONDS)
                .callTimeout(8, TimeUnit.SECONDS)
                .build()
            val req = Request.Builder().url("$serverUrl/health").get().build()
            client.newCall(req).execute().use { it.isSuccessful }
        } catch (_: Exception) {
            false
        }
    }

    private fun execSsh(session: Session, script: String, timeoutMs: Long): Pair<Int, String> {
        val channel = session.openChannel("exec") as ChannelExec
        val outBuf = ByteArrayOutputStream()
        val b64 = Base64.encodeToString(script.toByteArray(StandardCharsets.UTF_8), Base64.NO_WRAP)
        channel.setCommand("printf '%s' '$b64' | base64 -d | bash")
        channel.inputStream = null
        channel.setErrStream(outBuf)
        val stdout = channel.inputStream
        channel.connect(20_000)
        val deadline = System.currentTimeMillis() + timeoutMs
        val buf = ByteArray(4096)
        while (System.currentTimeMillis() < deadline) {
            while (stdout.available() > 0) {
                val n = stdout.read(buf)
                if (n <= 0) break
                outBuf.write(buf, 0, n)
            }
            if (channel.isClosed) {
                if (stdout.available() > 0) continue
                break
            }
            Thread.sleep(150)
        }
        val rc = if (channel.isClosed) channel.exitStatus else -1
        channel.disconnect()
        return rc to outBuf.toString(StandardCharsets.UTF_8.name())
    }
}
