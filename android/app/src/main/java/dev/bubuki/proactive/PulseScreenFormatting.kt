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


private val RE_FENCED_CODE_OPEN = Regex("```[a-zA-Z0-9_+-]*")
private val RE_INLINE_CODE = Regex("`([^`]+)`")
private val RE_MD_IMAGE = Regex("!\\[[^\\]]*]\\([^)]+\\)")
private val RE_MD_LINK = Regex("\\[([^\\]]+)]\\([^)]+\\)")
private val RE_MD_HEADING = Regex("(?m)(?:^|(?<=\\s))#{1,6}\\s+")
private val RE_MD_QUOTE = Regex("(?m)^>\\s?")
private val RE_MD_BULLET = Regex("(?m)^\\s*[-*+]\\s+")
private val RE_MD_STRONG = Regex("(\\*\\*|__|~~)")
private val RE_MD_ITALIC = Regex("(?<![A-Za-z0-9_])([*_])(?=\\S)(.+?)(?<=\\S)\\1(?![A-Za-z0-9_])")
private val RE_WHITESPACE = Regex("\\s+")

internal fun stripMarkdownForPreview(raw: String): String {
    if (raw.isBlank()) return ""
    return raw
        .replace(RE_FENCED_CODE_OPEN, " ")
        .replace(RE_INLINE_CODE, "$1")
        .replace(RE_MD_IMAGE, "[图片]")
        .replace(RE_MD_LINK, "$1")
        .replace(RE_MD_HEADING, "")
        .replace(RE_MD_QUOTE, "")
        .replace(RE_MD_BULLET, "• ")
        .replace(RE_MD_STRONG, "")
        .replace(RE_MD_ITALIC, "$2")
        .replace(RE_WHITESPACE, " ")
        .trim()
}

internal fun humanizeToolName(tool: String, args: org.json.JSONObject?): String {
    val op = args?.optString("op").orEmpty()
    return when (tool) {
        "phone_workspace" -> when (op) {
            "ui_tree" -> "读取虚拟屏界面节点"
            "shell", "bash", "cmd" -> "手机本地命令: ${args?.optString("command").orEmpty().take(28)}"
            "device_info", "phone_info" -> "查询手机当前状态"
            "pull_file" -> "从手机拉取文件: ${args?.optString("path", "latest").orEmpty().substringAfterLast('/')}"
            "push_file" -> "推送文件到手机: ${args?.optString("path").orEmpty().substringAfterLast('/')}"
            "list_files", "files" -> "查看手机目录: ${args?.optString("path", "/sdcard/Download").orEmpty()}"
            "send_screenshot" -> "截取画面发到聊天框"
            "notifications" -> "查看/过滤收敛通知栏"
            "tap_node" -> {
                val target = args?.optString("ref")?.takeIf { it.isNotBlank() }
                    ?: args?.optString("text")?.takeIf { it.isNotBlank() }
                    ?: args?.optString("view_id").orEmpty()
                "点击界面节点 ${target.ifBlank { "" }}".trim()
            }
            "type_node" -> "输入文字「${args?.optString("text").orEmpty().take(18)}」"
            "launch" -> "启动应用「${args?.optString("app")?.ifBlank { args.optString("package") }.orEmpty()}」"
            "shell" -> "执行系统指令"
            "swipe" -> "滑动浏览虚拟屏"
            "tap" -> "点击屏幕坐标 (${args?.optInt("x", 0)}, ${args?.optInt("y", 0)})"
            "key" -> "触发按键 ${args?.optString("key").orEmpty()}"
            "capture" -> "截取虚拟屏画面"
            "notifications", "get_notifications", "recent_notifications", "notifs", "notification" -> {
                val filterApp = args?.optString("app").orEmpty()
                if (filterApp.isNotBlank()) "读取「$filterApp」最近通知" else "读取手机最近通知"
            }
            "release", "auto_release" -> "释放虚拟屏工作区"
            else -> "操作虚拟屏 ${op.ifBlank { "" }}".trim()
        }
        "phone_ui_tree" -> "读取虚拟屏界面节点"
        "phone_tap_node" -> "点击节点 ${args?.optString("ref")?.ifBlank { args.optString("text") }.orEmpty()}"
        "phone_type_node" -> "输入文字「${args?.optString("text").orEmpty().take(18)}」"
        "phone_launch_app" -> "启动应用「${args?.optString("app").orEmpty()}」"
        "web_search" -> "搜索网页「${args?.optString("query").orEmpty().take(24)}」"
        "web_fetch" -> "读取网页 ${args?.optString("url").orEmpty().take(32)}"
        "read_file" -> "读取文件 ${args?.optString("path").orEmpty().substringAfterLast('/')}"
        "write_file" -> "写入文件 ${args?.optString("path").orEmpty().substringAfterLast('/')}"
        "edit_file" -> "修改文件 ${args?.optString("path").orEmpty().substringAfterLast('/')}"
        "memory_search" -> "检索记忆「${args?.optString("query").orEmpty().take(20)}」"
        "run_bash" -> "执行命令 ${args?.optString("command").orEmpty().take(28)}"
        "schedule_task" -> {
            val act = args?.optString("action").orEmpty()
            val atTime = args?.optString("at_time").orEmpty()
            when (act) {
                "list" -> "查阅日程与闹钟列表"
                "add_timer" -> "设定提醒 $atTime".trim()
                "update" -> "更新日程提醒"
                "remove" -> "移除旧提醒"
                "snooze" -> "开启免打扰模式"
                "wake" -> "恢复主动联系"
                else -> "管理日程与提醒"
            }
        }
        "send_message" -> "派发协作给 ${args?.optString("to_bot_id").orEmpty()}"
        "manage_bot" -> "配置团队助手"
        "list_models" -> "查询可用模型列表"
        "cloud_desktop" -> when (op) {
            "bash", "exec", "shell", "terminal", "run" -> {
                val cmd = args?.optString("command")?.takeIf { it.isNotBlank() } ?: args?.optString("goal").orEmpty()
                "在虚拟电脑执行「${cmd.take(22)}」"
            }
            "navigate" -> "在虚拟电脑打开网页 ${args?.optString("url").orEmpty().take(26)}"
            "launch_app", "app", "open_app" -> "在虚拟电脑打开应用 ${args?.optString("app").orEmpty()}"
            "batch" -> "在虚拟电脑连招执行 ${args?.optJSONArray("steps")?.length() ?: 0} 步"
            "click", "double_click", "right_click" -> "在虚拟电脑点击 (${args?.optInt("x", 0)}, ${args?.optInt("y", 0)})"
            "type" -> "在虚拟电脑输入「${args?.optString("text").orEmpty().take(18)}」"
            "key" -> "在虚拟电脑按键 ${args?.optString("key").orEmpty()}"
            "scroll" -> "在虚拟电脑滚动屏幕"
            "screenshot", "capture", "frame", "status" -> "读取云电脑屏幕画面"
            else -> "操作云电脑"
        }
        "notify_user" -> "发送通知「${args?.optString("title").orEmpty().take(20)}」"
        "emergency_alarm" -> "拉响紧急闹钟提醒「${args?.optString("title").orEmpty().take(18)}」"
        "conversation_search" -> "检索归档对话「${args?.optString("query").orEmpty().take(20)}」"
        "ctx_search" -> "按需检索长输出「${args?.optString("query").orEmpty().take(20)}」"
        "present_choices" -> "生成快捷选项胶囊"
        "memory_pin" -> "管理核心钉住记忆"
        "blackboard" -> "同步团队黑板与步骤账本"
        "watchdog_task" -> "管理零 Token 哨兵监控脚本"
        "custom_tool" -> "热插拔自造工具 ${args?.optString("name").orEmpty()}"
        "phone_macro" -> "执行手机动作宏 ${args?.optString("name").orEmpty()}"
        "gmail_connector" -> "操作 Gmail 邮件 (${args?.optString("action").orEmpty()})"
        "integration_hub" -> "调用第三方应用接口 (${args?.optString("service").orEmpty()}:${args?.optString("action").orEmpty()})"
        else -> "执行 $tool"
    }
}


internal val wikiTokenRegex = Regex("""(?s)(```.*?```|`[^`\n]+`|\[\[([^\]\n]+)\]\])""")

internal fun nativeWikiMarkdown(text: String): String {
    if (!text.contains("[[")) return text
    return wikiTokenRegex.replace(text) { match ->
        val full = match.value
        if (full.startsWith("`")) {
            full
        } else {
            val inner = match.groupValues[2]
            val parts = inner.split('|', limit = 2)
            val path = parts[0]
            val label = parts.getOrElse(1) { path }.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            "[$label](#vault:${android.net.Uri.encode(path)})"
        }
    }
}

@Composable
internal fun PulseAction(text: String, filled: Boolean = true, click: () -> Unit) {
    val c = LocalPulsePalette.current
    val haptics = LocalHapticFeedback.current
    Text(
        text,
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(14.dp))
            .background(if (filled) c.accent else c.surface)
            .iosPressScale(0.97f) {
                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                click()
            }
            .padding(horizontal = 16.dp, vertical = 14.dp),
        color = if (filled) Color.White else c.ink,
        fontSize = 15.sp,
        fontWeight = FontWeight.SemiBold,
        textAlign = TextAlign.Center
    )
}

