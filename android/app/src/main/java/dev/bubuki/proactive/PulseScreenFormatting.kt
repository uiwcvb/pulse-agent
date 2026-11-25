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
    val act = args?.optString("action").orEmpty()
    return when (tool) {
        "phone", "phone_workspace" -> when (op) {
            "ui_tree" -> "读取虚拟屏界面节点"
            "shell", "bash", "cmd" -> {
                val cmd = args?.optString("command").orEmpty().take(28)
                if (cmd.isNotBlank()) "手机本地命令: $cmd" else "执行手机系统指令"
            }
            "device_info", "phone_info" -> "查询手机当前状态"
            "pull_file" -> "从手机拉取文件: ${args?.optString("path", "latest").orEmpty().substringAfterLast('/')}"
            "push_file" -> "推送文件到手机: ${args?.optString("path").orEmpty().substringAfterLast('/')}"
            "list_files", "files" -> "查看手机目录: ${args?.optString("path", "/sdcard/Download").orEmpty()}"
            "send_screenshot" -> "截取画面发到聊天框"
            "notifications", "get_notifications", "recent_notifications", "notifs", "notification" -> {
                val filterApp = args?.optString("app").orEmpty()
                if (filterApp.isNotBlank()) "读取「$filterApp」最近通知" else "查看/过滤收敛通知栏"
            }
            "tap_node" -> {
                val target = args?.optString("ref")?.takeIf { it.isNotBlank() }
                    ?: args?.optString("text")?.takeIf { it.isNotBlank() }
                    ?: args?.optString("view_id").orEmpty()
                "点击界面节点 ${target.ifBlank { "" }}".trim()
            }
            "type_node" -> "输入文字「${args?.optString("text").orEmpty().take(18)}」"
            "launch", "open_app" -> "启动应用「${args?.optString("app")?.ifBlank { args.optString("package") }.orEmpty()}」"
            "open_uri" -> "在虚拟屏打开链接"
            "swipe" -> "滑动浏览虚拟屏"
            "tap" -> "点击屏幕坐标 (${args?.optInt("x", 0)}, ${args?.optInt("y", 0)})"
            "key" -> "触发按键 ${args?.optString("key").orEmpty()}"
            "capture", "screenshot" -> "截取虚拟屏画面"
            "release", "auto_release", "stop" -> "释放虚拟屏工作区"
            else -> "操作虚拟屏 ${op.ifBlank { "" }}".trim()
        }
        "phone_ui_tree" -> "读取虚拟屏界面节点"
        "phone_tap_node" -> "点击节点 ${args?.optString("ref")?.ifBlank { args.optString("text") }.orEmpty()}"
        "phone_type_node" -> "输入文字「${args?.optString("text").orEmpty().take(18)}」"
        "phone_launch_app" -> "启动应用「${args?.optString("app").orEmpty()}」"
        "search_files" -> {
            val pat = args?.optString("pattern")?.ifBlank { args.optString("query") }.orEmpty()
            val dir = args?.optString("path")?.ifBlank { args.optString("directory") }.orEmpty()
            val hint = pat.ifBlank { dir }.take(24)
            if (hint.isNotBlank()) "搜索文件「$hint」" else "检索工作区文件"
        }
        "web" -> {
            val url = args?.optString("url").orEmpty()
            val query = args?.optString("query").orEmpty()
            when {
                op == "fetch" || (url.isNotBlank() && query.isBlank()) -> "读取网页 ${url.take(32)}".trim()
                else -> "搜索网页「${query.take(24)}」"
            }
        }
        "web_search" -> "搜索网页「${args?.optString("query").orEmpty().take(24)}」"
        "web_fetch" -> "读取网页 ${args?.optString("url").orEmpty().take(32)}"
        "read_file" -> "读取文件 ${args?.optString("path").orEmpty().substringAfterLast('/')}"
        "stage_1_backup" -> "快照备份与全域盘点"
        "stage_2_preclean" -> "预清洗与白名单锁定"
        "stage_3_cluster" -> "本地语义聚类分组"
        "stage_4_canonicalize" -> "全局实体别名归一"
        "stage_5_adjudicate" -> {
            val detail = args?.optString("detail").orEmpty().take(28)
            if (detail.isNotBlank()) "裁决时间线 $detail" else "簇内时间线裁决"
        }
        "stage_6_critic" -> "Critic 复审与核心块重铸"
        "stage_7_commit" -> "原子切换与旧轨归档"
        "stage_8_finalize" -> "重建本地向量与审计报告"
        "write_file" -> "写入文件 ${args?.optString("path").orEmpty().substringAfterLast('/')}"
        "edit_file" -> "修改文件 ${args?.optString("path").orEmpty().substringAfterLast('/')}"
        "list_dir" -> "列出目录 ${args?.optString("path").orEmpty().ifBlank { "." }}"
        "glob_files" -> "匹配文件 ${args?.optString("pattern").orEmpty().take(24)}"
        "grep_search" -> "检索文本「${args?.optString("query").orEmpty().take(24)}」"
        "memory" -> when (op.ifBlank { act }.lowercase()) {
            "pin", "add", "remember" -> "钉住关键记忆"
            "unpin", "remove", "delete", "forget" -> "取消钉住记忆"
            "list_pins", "pins", "list" -> "查阅钉住记忆列表"
            "conversation", "conv" -> "检索历史对话「${args?.optString("query").orEmpty().take(20)}」"
            "ctx", "context" -> "按需检索长输出「${args?.optString("query").orEmpty().take(20)}」"
            else -> "检索记忆库「${args?.optString("query").orEmpty().take(20)}」"
        }
        "memory_search" -> "检索记忆「${args?.optString("query").orEmpty().take(20)}」"
        "bash", "run_bash" -> "执行命令 ${args?.optString("command").orEmpty().take(28)}".trim()
        "schedule_task" -> {
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
        "subtask", "run_subtask" -> "派发子任务 ${args?.optString("goal").orEmpty().take(22)}".trim()
        "admin" -> when (op.lowercase()) {
            "status", "doctor", "context", "all", "check", "" -> "检查系统与通道健康"
            "config", "system_config" -> "管理系统配置"
            "bot", "manage_bot" -> "配置团队助手"
            "skill" -> "管理扩展技能包"
            "custom_tool", "plugin" -> "管理自定义工具/插件"
            "models", "list_models" -> "查询可用模型列表"
            "deploy_cloud", "deploy_cloud_computer" -> "部署云电脑工作区"
            else -> "管理后台控制台 ($op)"
        }
        "manage_bot" -> "配置团队助手"
        "list_models" -> "查询可用模型列表"
        "pc", "dsh_workspace" -> when (op) {
            "bash", "shell", "exec", "run" -> "在电脑端执行「${args?.optString("command").orEmpty().take(22)}」"
            "screenshot", "capture", "screen" -> "截取电脑屏幕画面"
            "click", "tap" -> "在电脑屏幕点击"
            "type" -> "在电脑端输入文字"
            "status", "info" -> "查询电脑端在线状态"
            else -> if (op.isNotBlank()) "操控电脑端 ($op)" else "操控电脑端工作区"
        }
        "cloud", "cloud_desktop" -> when (op) {
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
        "reply" -> when (args?.optString("op")?.lowercase().orEmpty()) {
            "sticker", "send_sticker", "emoji" -> {
                val code = args?.optString("code")?.ifBlank { args.optString("sticker") }.orEmpty()
                if (code.isNotBlank()) "附带表情贴纸 · $code" else "附带表情贴纸"
            }
            "choices", "present_choices" -> "生成快捷选项"
            "send_image", "image" -> "发送图片"
            "send_file", "file", "send_pdf", "send_doc" -> "发送文件"
            "notify", "notify_user" -> "发送通知"
            "alarm", "emergency_alarm" -> "拉响紧急提醒"
            "silent", "stay_silent", "dnd" -> "保持安静"
            "complete_goal", "finish_goal" -> "标记目标完成"
            else -> if (op.isNotBlank()) "发送交互回应 ($op)" else "发送交互回应"
        }
        "gmail_connector" -> "操作 Gmail 邮件 (${args?.optString("action").orEmpty()})"
        "integration_hub" -> "调用第三方应用接口 (${args?.optString("service").orEmpty()}:${args?.optString("action").orEmpty()})"
        else -> "执行 $tool"
    }
}

private val RE_RESIDUAL_STICKER_COLON = Regex("""<sticker\s*[:：]\s*[^>\n]*?\s*/?>""", RegexOption.IGNORE_CASE)
private val RE_RESIDUAL_STICKER_ATTR = Regex("""<sticker\b[^>]*>""", RegexOption.IGNORE_CASE)
private val RE_RESIDUAL_STICKER_PAIR = Regex("""<sticker\b[^>]*>[^<\n]*</sticker>""", RegexOption.IGNORE_CASE)
private val RE_RESIDUAL_STICKER_BRACKET = Regex("""\[(?:sticker|表情|贴纸)\s*[:：]\s*[^\]\n]+?\]""", RegexOption.IGNORE_CASE)
private val RE_RESIDUAL_THINK_TAG = Regex("""</?think>""", RegexOption.IGNORE_CASE)
private val RE_MULTI_NEWLINE_STK = Regex("""\n{3,}""")

internal fun stripResidualControlTags(raw: String): String {
    if (raw.isBlank()) return raw.trim()
    return raw
        .replace(RE_RESIDUAL_STICKER_PAIR, "")
        .replace(RE_RESIDUAL_STICKER_COLON, "")
        .replace(RE_RESIDUAL_STICKER_ATTR, "")
        .replace(RE_RESIDUAL_STICKER_BRACKET, "")
        .replace(RE_RESIDUAL_THINK_TAG, "")
        .replace(RE_MULTI_NEWLINE_STK, "\n\n")
        .trim()
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

