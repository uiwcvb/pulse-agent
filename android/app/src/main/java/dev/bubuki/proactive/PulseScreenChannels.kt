package dev.bubuki.proactive

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

@Composable
internal fun ApiChannelsSection(model: NativePulseModel) {
    val c = LocalPulsePalette.current
    val s = model.ui
    var showEditor by remember { mutableStateOf(false) }
    var editingChannel by remember { mutableStateOf<PulseApiChannel?>(null) }

    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("3. 多 API 渠道聚合", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
                Text(
                    "添加多个 OpenAI 兼容来源（DeepSeek / OpenRouter / 中转站等），自动聚合模型、按优先级路由与同模型容灾。",
                    color = c.muted,
                    fontSize = 12.sp,
                    lineHeight = 16.sp
                )
            }
            Text(
                "+ 添加渠道",
                Modifier.clip(RoundedCornerShape(10.dp))
                    .background(c.accent)
                    .iosPressScale(0.95f) {
                        editingChannel = null
                        showEditor = true
                    }
                    .padding(horizontal = 11.dp, vertical = 7.dp),
                color = Color.White,
                fontSize = 12.sp,
                fontWeight = FontWeight.SemiBold
            )
        }

        if (s.channels.isEmpty()) {
            Box(
                Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(c.fill).padding(12.dp)
            ) {
                Text(
                    "尚未添加额外渠道（当前使用上方主接口）。点右上角「+ 添加渠道」即可聚合多个 AI 来源。",
                    color = c.muted,
                    fontSize = 12.sp,
                    lineHeight = 17.sp
                )
            }
        } else {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                s.channels.forEachIndexed { idx, ch ->
                    if (idx > 0) {
                        HorizontalDivider(color = c.line.copy(alpha = 0.5f), thickness = 0.5.dp)
                    }
                    Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(c.fill.copy(alpha = 0.65f)).padding(12.dp),
                        verticalArrangement = Arrangement.spacedBy(6.dp)
                    ) {
                        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Text(
                                ch.name.ifBlank { ch.id },
                                color = if (ch.enabled) c.ink else c.muted,
                                fontSize = 14.sp,
                                fontWeight = FontWeight.SemiBold,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                                modifier = Modifier.weight(1f)
                            )
                            Text(
                                "P${ch.priority}",
                                Modifier.clip(RoundedCornerShape(6.dp)).background(c.accent.copy(alpha = 0.16f)).padding(horizontal = 6.dp, vertical = 2.dp),
                                color = c.accent,
                                fontSize = 11.sp,
                                fontWeight = FontWeight.SemiBold
                            )
                            Switch(
                                checked = ch.enabled,
                                onCheckedChange = { model.toggleApiChannel(ch, it) }
                            )
                        }
                        Text(
                            ch.baseUrl,
                            color = c.muted,
                            fontSize = 11.5.sp,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis
                        )
                        Text(
                            buildString {
                                append(if (ch.hasApiKey) "Key: ${ch.apiKeyMasked.ifBlank { "已保存" }}" else "未配置 Key")
                                append(" · 已缓存 ${ch.models.size} 个模型")
                            },
                            color = if (ch.hasApiKey) c.muted else c.amber,
                            fontSize = 11.5.sp
                        )
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(top = 2.dp)) {
                            val testing = s.testingChannelId == ch.id
                            val dangerRed = Color(0xFFFF453A)
                            Text(
                                if (testing) "测速中…" else "测速拉模型",
                                Modifier.clip(RoundedCornerShape(8.dp))
                                    .background(c.accent.copy(alpha = 0.16f))
                                    .iosPressScale(0.95f, enabled = !testing) { model.testApiChannel(ch.id) }
                                    .padding(horizontal = 10.dp, vertical = 6.dp),
                                color = c.accent,
                                fontSize = 11.5.sp,
                                fontWeight = FontWeight.Medium
                            )
                            Text(
                                "编辑",
                                Modifier.clip(RoundedCornerShape(8.dp))
                                    .background(c.surface)
                                    .iosPressScale(0.95f) {
                                        editingChannel = ch
                                        showEditor = true
                                    }
                                    .padding(horizontal = 10.dp, vertical = 6.dp),
                                color = c.ink,
                                fontSize = 11.5.sp,
                                fontWeight = FontWeight.Medium
                            )
                            Text(
                                "删除",
                                Modifier.clip(RoundedCornerShape(8.dp))
                                    .background(dangerRed.copy(alpha = 0.14f))
                                    .iosPressScale(0.95f) { model.deleteApiChannel(ch.id) }
                                    .padding(horizontal = 10.dp, vertical = 6.dp),
                                color = dangerRed,
                                fontSize = 11.5.sp,
                                fontWeight = FontWeight.Medium
                            )
                        }
                    }
                }
            }
        }
    }

    if (showEditor) {
        ChannelEditorDialog(
            initial = editingChannel,
            onDismiss = { showEditor = false },
            onSave = { id, name, url, key, prio ->
                model.saveApiChannel(
                    id = id,
                    name = name,
                    baseUrl = url,
                    apiKey = key,
                    priority = prio,
                    enabled = editingChannel?.enabled ?: true,
                    autoFetch = true,
                    onDone = { showEditor = false }
                )
            }
        )
    }
}

@Composable
private fun ChannelEditorDialog(
    initial: PulseApiChannel?,
    onDismiss: () -> Unit,
    onSave: (id: String, name: String, baseUrl: String, apiKey: String, priority: Int) -> Unit
) {
    val c = LocalPulsePalette.current
    var name by remember(initial) { mutableStateOf(initial?.name.orEmpty()) }
    var baseUrl by remember(initial) { mutableStateOf(initial?.baseUrl.orEmpty()) }
    var apiKey by remember(initial) { mutableStateOf("") }
    var priority by remember(initial) { mutableStateOf((initial?.priority ?: 100).toString()) }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = c.surface,
        titleContentColor = c.ink,
        textContentColor = c.ink,
        title = {
            Text(
                if (initial == null) "添加 AI API 渠道" else "编辑渠道 · ${initial.name}",
                fontSize = 16.5.sp,
                fontWeight = FontWeight.SemiBold
            )
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                PulseInputField(
                    label = "渠道名称",
                    value = name,
                    placeholder = "例如：DeepSeek 官方 / OpenRouter / SiliconFlow",
                    onChange = { name = it }
                )
                PulseInputField(
                    label = "API Base URL（不含 /chat/completions）",
                    value = baseUrl,
                    placeholder = "https://api.deepseek.com/v1",
                    onChange = { baseUrl = it }
                )
                PulseInputField(
                    label = if (initial?.hasApiKey == true) "API Key（留空保持原有密钥不变）" else "API Key",
                    value = apiKey,
                    placeholder = if (initial?.hasApiKey == true) "${initial.apiKeyMasked}（如需更换请填写）" else "sk-...",
                    onChange = { apiKey = it }
                )
                PulseInputField(
                    label = "优先级（数字越小越优先，默认 100）",
                    value = priority,
                    placeholder = "100",
                    onChange = { priority = it.filter { ch -> ch.isDigit() }.take(4) }
                )
            }
        },
        confirmButton = {
            TextButton(onClick = {
                onSave(
                    initial?.id.orEmpty(),
                    name.trim(),
                    baseUrl.trim(),
                    apiKey.trim(),
                    (priority.toIntOrNull() ?: 100).coerceIn(0, 1000)
                )
            }) {
                Text("保存并拉取模型", color = c.accent, fontWeight = FontWeight.SemiBold)
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("取消", color = c.muted)
            }
        }
    )
}

@Composable
internal fun ChannelAwareModelPicker(
    entries: List<PulseModelEntry>,
    channels: List<PulseApiChannel>,
    selectedValue: String,
    accentColor: Color,
    searchQuery: String = "",
    showSearchBox: Boolean = false,
    onSearchChange: ((String) -> Unit)? = null,
    onSelect: (String) -> Unit
) {
    val c = LocalPulsePalette.current
    if (entries.isEmpty()) return

    var selectedChannelId by remember { mutableStateOf("__all__") }
    var localSearch by remember { mutableStateOf("") }
    val effQuery = if (onSearchChange != null) searchQuery else localSearch

    val channelTabs = remember(entries) {
        val counts = LinkedHashMap<String, Pair<String, Int>>()
        entries.forEach { e ->
            val prev = counts[e.channelId]
            counts[e.channelId] = (prev?.first ?: e.channelName) to ((prev?.second ?: 0) + 1)
        }
        counts.entries.map { (cid, pair) -> Triple(cid, pair.first, pair.second) }
    }

    val filtered = remember(entries, selectedChannelId, effQuery) {
        entries.filter { e ->
            val chOk = selectedChannelId == "__all__" || e.channelId == selectedChannelId
            val qOk = effQuery.isBlank() ||
                e.model.contains(effQuery, ignoreCase = true) ||
                e.channelName.contains(effQuery, ignoreCase = true) ||
                e.value.contains(effQuery, ignoreCase = true)
            chOk && qOk
        }
    }

    val currentSourceLabel = remember(selectedValue, entries, channels) {
        val raw = selectedValue.trim()
        if (raw.isBlank()) return@remember ""
        val exact = entries.firstOrNull { it.value == raw || it.model == raw }
        if (exact != null) return@remember exact.channelName
        if (raw.startsWith("ch:") && raw.indexOf('/') > 3) {
            val cid = raw.substring(3, raw.indexOf('/'))
            return@remember channels.firstOrNull { it.id == cid }?.name?.ifBlank { cid } ?: cid
        }
        val byCh = channels.filter { it.enabled }.sortedBy { it.priority }.firstOrNull { raw in it.models }
        byCh?.name?.ifBlank { byCh.id } ?: "主接口"
    }

    Column(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
        if (currentSourceLabel.isNotBlank()) {
            Text(
                "当前来源渠道：$currentSourceLabel",
                color = accentColor,
                fontSize = 11.5.sp,
                fontWeight = FontWeight.Medium
            )
        }
        if (showSearchBox) {
            PulseInputField(
                label = "筛选已聚合的 ${entries.size} 个模型（支持搜模型名或渠道名）",
                value = effQuery,
                placeholder = "输入模型名或渠道名过滤…",
                onChange = { q ->
                    if (onSearchChange != null) onSearchChange(q) else localSearch = q
                }
            )
        }
        if (channelTabs.size > 1) {
            LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.fillMaxWidth()) {
                item {
                    val allSel = selectedChannelId == "__all__"
                    Text(
                        "全部 (${entries.size})",
                        Modifier.clip(RoundedCornerShape(8.dp))
                            .background(if (allSel) accentColor.copy(alpha = 0.2f) else c.fill)
                            .iosPressScale(0.95f) { selectedChannelId = "__all__" }
                            .padding(horizontal = 9.dp, vertical = 5.dp),
                        color = if (allSel) accentColor else c.muted,
                        fontSize = 11.sp,
                        fontWeight = if (allSel) FontWeight.SemiBold else FontWeight.Medium
                    )
                }
                items(channelTabs) { (cid, cname, cnt) ->
                    val tabSel = selectedChannelId == cid
                    Text(
                        "$cname ($cnt)",
                        Modifier.clip(RoundedCornerShape(8.dp))
                            .background(if (tabSel) accentColor.copy(alpha = 0.2f) else c.fill)
                            .iosPressScale(0.95f) { selectedChannelId = cid }
                            .padding(horizontal = 9.dp, vertical = 5.dp),
                        color = if (tabSel) accentColor else c.muted,
                        fontSize = 11.sp,
                        fontWeight = if (tabSel) FontWeight.SemiBold else FontWeight.Medium
                    )
                }
            }
        }
        LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            items(filtered.take(80)) { item ->
                val selected = item.value == selectedValue || (selectedValue.isNotBlank() && item.model == selectedValue && !selectedValue.startsWith("ch:"))
                Column(
                    Modifier.clip(RoundedCornerShape(10.dp))
                        .background(if (selected) accentColor else c.fill)
                        .iosPressScale(0.95f) { onSelect(item.value) }
                        .padding(horizontal = 11.dp, vertical = 7.dp),
                    verticalArrangement = Arrangement.spacedBy(2.dp)
                ) {
                    Text(
                        item.model,
                        color = if (selected) Color.White else c.ink,
                        fontSize = 12.sp,
                        fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Medium,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                    Text(
                        item.channelName,
                        color = if (selected) Color.White.copy(alpha = 0.85f) else if (item.isChannel) accentColor else c.muted,
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Medium,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                }
            }
        }
    }
}
