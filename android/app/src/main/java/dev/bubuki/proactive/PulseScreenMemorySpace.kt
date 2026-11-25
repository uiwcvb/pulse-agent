package dev.bubuki.proactive

import android.graphics.Paint
import android.graphics.Typeface
import androidx.compose.animation.animateContentSize
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.drawIntoCanvas
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import kotlin.math.hypot
import kotlin.math.roundToInt

/**
 * [Module] PulseScreenMemorySpace
 * [Responsibility]
 * Real 2D Vector Semantic Space & Keyword Network panel (`NativeMemoryVectorSpacePanel`).
 * Visualizes actual `memory_vectors` cosine-similarity links, PCA-projected clusters,
 * interactive keyword anchors, live query vector probes, inline fact CRUD, and one-click
 * reorganize / backup rollback (with fallback toggle to raw Markdown Vault editor).
 */

internal data class MemoryVecNode(
    val id: String, val itemType: String, val entity: String, val attr: String,
    val label: String, val summary: String, val fullText: String, val ttlType: String,
    val accessCount: Int, val hasVector: Boolean, val x: Float, val y: Float,
    val degree: Int, val keywords: List<String>
)

internal data class MemoryVecEdge(
    val source: String, val target: String, val weight: Float, val kind: String, val sharedKeywords: List<String>
)

internal data class MemoryKeywordAnchor(
    val word: String, val count: Int, val x: Float, val y: Float, val nodeIds: Set<String>
)

internal data class MemoryProbeMatch(val nodeId: String, val score: Float, val vecSim: Float)

internal data class MemoryVectorProbe(
    val query: String, val x: Float, val y: Float, val mode: String, val matches: List<MemoryProbeMatch>
)

internal data class MemoryVectorSpaceData(
    val nodes: List<MemoryVecNode> = emptyList(),
    val edges: List<MemoryVecEdge> = emptyList(),
    val keywords: List<MemoryKeywordAnchor> = emptyList(),
    val probe: MemoryVectorProbe? = null,
    val embeddingModel: String = "", val vectorizedNodes: Int = 0, val activeFacts: Int = 0, val episodes: Int = 0
)

private fun parseStringList(arr: JSONArray?): List<String> =
    if (arr == null) emptyList() else (0 until arr.length()).mapNotNull { arr.optString(it).takeIf { s -> s.isNotBlank() } }

private fun parseVectorSpace(res: JSONObject): MemoryVectorSpaceData {
    val nArr = res.optJSONArray("nodes") ?: JSONArray()
    val nodes = (0 until nArr.length()).mapNotNull { i ->
        val o = nArr.optJSONObject(i) ?: return@mapNotNull null
        val id = o.optString("id").trim().takeIf { it.isNotBlank() } ?: return@mapNotNull null
        MemoryVecNode(
            id = id, itemType = o.optString("item_type", "fact"), entity = o.optString("entity", ""),
            attr = o.optString("attr", ""), label = o.optString("label", id),
            summary = o.optString("summary", ""), fullText = o.optString("full_text", o.optString("summary", "")),
            ttlType = o.optString("ttl_type", "stateful"), accessCount = o.optInt("access_count", 0),
            hasVector = o.optBoolean("has_vector", false),
            x = o.optDouble("x", 0.5).toFloat().coerceIn(0.04f, 0.96f),
            y = o.optDouble("y", 0.5).toFloat().coerceIn(0.06f, 0.94f),
            degree = o.optInt("degree", 0), keywords = parseStringList(o.optJSONArray("keywords"))
        )
    }
    val eArr = res.optJSONArray("edges") ?: JSONArray()
    val edges = (0 until eArr.length()).mapNotNull { i ->
        val o = eArr.optJSONObject(i) ?: return@mapNotNull null
        val src = o.optString("source")
        val dst = o.optString("target")
        if (src.isBlank() || dst.isBlank()) return@mapNotNull null
        MemoryVecEdge(
            source = src,
            target = dst,
            weight = o.optDouble("weight", 0.5).toFloat().coerceIn(0f, 1f),
            kind = o.optString("kind", "vector"),
            sharedKeywords = parseStringList(o.optJSONArray("shared_keywords"))
        )
    }
    val kArr = res.optJSONArray("keywords") ?: JSONArray()
    val keywords = (0 until kArr.length()).mapNotNull { i ->
        val o = kArr.optJSONObject(i) ?: return@mapNotNull null
        val w = o.optString("word").trim()
        if (w.isBlank()) return@mapNotNull null
        MemoryKeywordAnchor(
            word = w,
            count = o.optInt("count", 1),
            x = o.optDouble("x", 0.5).toFloat().coerceIn(0.05f, 0.95f),
            y = o.optDouble("y", 0.5).toFloat().coerceIn(0.07f, 0.93f),
            nodeIds = parseStringList(o.optJSONArray("node_ids")).toSet()
        )
    }
    val pObj = res.optJSONObject("probe")
    val probe = pObj?.let { p ->
        val mArr = p.optJSONArray("matches") ?: JSONArray()
        val ms = (0 until mArr.length()).mapNotNull { i ->
            val mo = mArr.optJSONObject(i) ?: return@mapNotNull null
            val nid = mo.optString("node_id")
            if (nid.isBlank()) return@mapNotNull null
            MemoryProbeMatch(
                nodeId = nid,
                score = mo.optDouble("score", 0.0).toFloat(),
                vecSim = mo.optDouble("vec_sim", 0.0).toFloat()
            )
        }
        MemoryVectorProbe(
            query = p.optString("query", ""),
            x = p.optDouble("x", 0.5).toFloat(),
            y = p.optDouble("y", 0.5).toFloat(),
            mode = p.optString("mode", "vector+keyword"),
            matches = ms
        )
    }
    val st = res.optJSONObject("stats") ?: JSONObject()
    return MemoryVectorSpaceData(
        nodes = nodes,
        edges = edges,
        keywords = keywords,
        probe = probe,
        embeddingModel = st.optString("embedding_model", ""),
        vectorizedNodes = st.optInt("vectorized_nodes", 0),
        activeFacts = st.optInt("active_facts", 0),
        episodes = st.optInt("episodes", 0)
    )
}

internal suspend fun NativePulseModel.fetchMemoryVectorSpace(query: String = ""): MemoryVectorSpaceData? {
    val enc = if (query.isNotBlank()) URLEncoder.encode(query.trim(), "UTF-8") else ""
    val path = if (enc.isNotBlank()) "/api/memory/vector-space?q=$enc&limit=180" else "/api/memory/vector-space?limit=180"
    val res = runCatching { request("GET", path) }.getOrNull() ?: return null
    if (!res.optBoolean("ok", true)) return null
    return parseVectorSpace(res)
}

internal suspend fun NativePulseModel.fetchMemoryBackupsList(): List<Pair<String, String>> {
    val res = runCatching { request("GET", "/api/memory/backups") }.getOrNull() ?: return emptyList()
    val arr = res.optJSONArray("backups") ?: return emptyList()
    return (0 until arr.length()).mapNotNull { i ->
        val o = arr.optJSONObject(i) ?: return@mapNotNull null
        val id = o.optString("backup_id").ifBlank { o.optString("id") }
        if (id.isBlank()) return@mapNotNull null
        val meta = o.optString("created_at", o.optString("path", ""))
        id to meta
    }
}

@Composable
internal fun NativeMemoryVectorSpacePanel(
    model: NativePulseModel,
    onSwitchToRawMarkdown: () -> Unit,
    modifier: Modifier = Modifier
) {
    val c = LocalPulsePalette.current
    val isDark = model.ui.dark
    val focus = LocalFocusManager.current
    val scope = rememberCoroutineScope()

    var space by remember { mutableStateOf(MemoryVectorSpaceData()) }
    var loading by remember { mutableStateOf(true) }
    var query by remember { mutableStateOf("") }
    var selectedNodeId by remember { mutableStateOf<String?>(null) }
    var selectedKeyword by remember { mutableStateOf<String?>(null) }
    var canvasExpanded by remember { mutableStateOf(true) }
    var showManageDialog by remember { mutableStateOf(false) }
    var showAddFactDialog by remember { mutableStateOf(false) }
    var editTarget by remember { mutableStateOf<MemoryVecNode?>(null) }
    var deleteTarget by remember { mutableStateOf<MemoryVecNode?>(null) }
    var backups by remember { mutableStateOf<List<Pair<String, String>>>(emptyList()) }
    var statusNotice by remember { mutableStateOf("") }

    fun reloadSpace(q: String = query) {
        scope.launch {
            loading = true
            val data = model.fetchMemoryVectorSpace(q)
            if (data != null) space = data
            loading = false
        }
    }

    LaunchedEffect(Unit) { reloadSpace("") }
    LaunchedEffect(query) {
        delay(320L)
        val data = model.fetchMemoryVectorSpace(query)
        if (data != null) space = data
        loading = false
    }

    val nodesById = remember(space.nodes) { space.nodes.associateBy { it.id } }
    val neighborWeights = remember(space.edges, selectedNodeId) {
        val sid = selectedNodeId ?: return@remember emptyMap<String, Float>()
        buildMap<String, Float> {
            space.edges.forEach { e ->
                if (e.source == sid) put(e.target, e.weight)
                else if (e.target == sid) put(e.source, e.weight)
            }
        }
    }
    val probeScoreById = remember(space.probe) {
        space.probe?.matches?.associate { it.nodeId to it.score } ?: emptyMap()
    }
    val keywordNodeIds = remember(space.keywords, selectedKeyword) {
        val kw = selectedKeyword ?: return@remember emptySet<String>()
        space.keywords.firstOrNull { it.word == kw }?.nodeIds ?: emptySet()
    }

    val visibleNodes = remember(space.nodes, selectedNodeId, selectedKeyword, space.probe) {
        when {
            space.probe != null && query.isNotBlank() -> {
                space.probe!!.matches.mapNotNull { m -> nodesById[m.nodeId] }
            }
            selectedNodeId != null -> {
                val center = nodesById[selectedNodeId]
                val nbs = neighborWeights.entries.sortedByDescending { it.value }.mapNotNull { nodesById[it.key] }
                listOfNotNull(center) + nbs
            }
            selectedKeyword != null -> {
                space.nodes.filter { it.id in keywordNodeIds }
            }
            else -> space.nodes
        }
    }

    Column(modifier.background(c.bg)) {
        // 1. Minimalist iOS Header
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
                    Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("向量记忆空间", color = c.ink, fontSize = 16.5.sp, fontWeight = FontWeight.SemiBold)
                        val sub = if (loading && space.nodes.isEmpty()) "正在投影语义向量…"
                        else "${space.nodes.size} 条记忆 · ${space.edges.size} 条关联 · ${space.vectorizedNodes} 已向量化"
                        Text(sub, color = c.muted, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                        Box(
                            Modifier.size(36.dp).clip(CircleShape).background(c.fill)
                                .iosPressScale(0.92f) {
                                    focus.clearFocus()
                                    showManageDialog = true
                                    scope.launch { backups = model.fetchMemoryBackupsList() }
                                },
                            contentAlignment = Alignment.Center
                        ) { PulseIcon("spark", c.accent, Modifier.size(18.dp)) }
                        Box(
                            Modifier.size(36.dp).clip(CircleShape).background(c.fill)
                                .iosPressScale(0.92f) { focus.clearFocus(); onSwitchToRawMarkdown() },
                            contentAlignment = Alignment.Center
                        ) { PulseIcon("file", c.muted, Modifier.size(18.dp)) }
                    }
                }
                HorizontalDivider(color = c.line.copy(alpha = 0.65f), thickness = 0.5.dp)
            }
        }

        // 2. Live Semantic Vector Probe Search Bar
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 10.dp)
                .clip(RoundedCornerShape(14.dp))
                .background(c.surface)
                .border(0.5.dp, c.line.copy(alpha = 0.7f), RoundedCornerShape(14.dp))
                .padding(horizontal = 12.dp, vertical = 9.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            PulseIcon("search", if (query.isNotBlank()) c.accent else c.muted, Modifier.size(17.dp))
            Spacer(Modifier.width(8.dp))
            Box(Modifier.weight(1f)) {
                if (query.isEmpty()) {
                    Text("输入任意语句投放语义探针，匹配关联向量与关键词…", color = c.muted, fontSize = 13.5.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                BasicTextField(
                    value = query,
                    onValueChange = {
                        query = it
                        if (it.isNotBlank()) {
                            selectedNodeId = null
                            selectedKeyword = null
                        }
                    },
                    singleLine = true,
                    textStyle = TextStyle(color = c.ink, fontSize = 14.sp),
                    cursorBrush = SolidColor(c.accent),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                    keyboardActions = KeyboardActions(onSearch = { focus.clearFocus(); reloadSpace(query) }),
                    modifier = Modifier.fillMaxWidth()
                )
            }
            if (query.isNotBlank()) {
                Box(
                    Modifier.size(24.dp).clip(CircleShape).background(c.fill)
                        .iosPressScale(0.9f) { query = ""; reloadSpace("") },
                    contentAlignment = Alignment.Center
                ) { PulseIcon("close", c.muted, Modifier.size(13.dp)) }
            }
        }

        if (statusNotice.isNotBlank()) {
            Text(
                statusNotice,
                color = c.accent,
                fontSize = 12.sp,
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 2.dp)
            )
        }

        // 3. Interactive 2D Vector Space Canvas (zoom / pan / tap / cosine edges / keyword anchors / live probe)
        Box(
            Modifier.fillMaxWidth()
                .padding(horizontal = 14.dp)
                .animateContentSize()
                .height(if (canvasExpanded) 276.dp else 44.dp)
                .clip(RoundedCornerShape(18.dp))
                .background(
                    if (isDark) Brush.radialGradient(listOf(Color(0xFF131B2E), Color(0xFF0A0E18)))
                    else Brush.verticalGradient(listOf(Color(0xFFF6F9FE), Color(0xFFEDF3FA)))
                )
                .border(0.7.dp, if (isDark) Color(0xFF23304A) else c.line, RoundedCornerShape(18.dp))
        ) {
            if (canvasExpanded) {
                VectorSemanticCanvas(
                    space = space,
                    isDark = isDark,
                    selectedNodeId = selectedNodeId,
                    selectedKeyword = selectedKeyword,
                    keywordNodeIds = keywordNodeIds,
                    neighborWeights = neighborWeights,
                    probeScoreById = probeScoreById,
                    onSelectNode = { nid ->
                        selectedKeyword = null
                        selectedNodeId = if (selectedNodeId == nid) null else nid
                    },
                    onToggleCollapse = { canvasExpanded = false }
                )
            } else {
                Row(
                    Modifier.fillMaxSize().iosPressScale(0.98f) { canvasExpanded = true }.padding(horizontal = 14.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    PulseIcon("vault", c.accent, Modifier.size(16.dp))
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "展开向量空间画布（${space.nodes.size} 节点 · ${space.edges.size} 条相似度连线）",
                        color = c.ink,
                        fontSize = 12.5.sp,
                        modifier = Modifier.weight(1f)
                    )
                    PulseIcon("chevron_down", c.muted, Modifier.size(16.dp))
                }
            }
        }

        // 4. Interactive Keyword Strip
        if (space.keywords.isNotEmpty()) {
            LazyRow(
                contentPadding = PaddingValues(horizontal = 14.dp, vertical = 10.dp),
                horizontalArrangement = Arrangement.spacedBy(7.dp)
            ) {
                if (selectedNodeId != null || selectedKeyword != null) {
                    item(key = "__clear__") {
                        Text(
                            "全部",
                            Modifier.clip(RoundedCornerShape(10.dp))
                                .background(c.surface)
                                .border(0.5.dp, c.line, RoundedCornerShape(10.dp))
                                .iosPressScale(0.95f) { selectedNodeId = null; selectedKeyword = null }
                                .padding(horizontal = 11.dp, vertical = 6.dp),
                            color = c.muted,
                            fontSize = 12.sp
                        )
                    }
                }
                items(space.keywords, key = { it.word }) { kw ->
                    val sel = selectedKeyword == kw.word
                    Text(
                        "${kw.word} · ${kw.count}",
                        Modifier.clip(RoundedCornerShape(10.dp))
                            .background(if (sel) c.accent else c.surface)
                            .border(0.5.dp, if (sel) c.accent else c.line.copy(alpha = 0.7f), RoundedCornerShape(10.dp))
                            .iosPressScale(0.95f) {
                                selectedNodeId = null
                                selectedKeyword = if (sel) null else kw.word
                            }
                            .padding(horizontal = 11.dp, vertical = 6.dp),
                        color = if (sel) Color.White else c.ink,
                        fontSize = 12.sp,
                        fontWeight = if (sel) FontWeight.SemiBold else FontWeight.Normal
                    )
                }
            }
        } else {
            Spacer(Modifier.height(8.dp))
        }

        // 5. Filtered Memory List (shows cosine similarity / probe score / keywords / tap to edit or delete)
        LazyColumn(
            Modifier.weight(1f).fillMaxWidth(),
            contentPadding = PaddingValues(start = 14.dp, end = 14.dp, bottom = 20.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            if (visibleNodes.isEmpty() && !loading) {
                item {
                    Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(16.dp)).background(c.surface).padding(22.dp),
                        horizontalAlignment = Alignment.CenterHorizontally
                    ) {
                        Text(
                            if (query.isNotBlank()) "未检索到高度相关的记忆向量" else "暂无向量化记忆",
                            color = c.ink,
                            fontSize = 14.5.sp,
                            fontWeight = FontWeight.Medium
                        )
                        Spacer(Modifier.height(6.dp))
                        Text(
                            "点右上角整理按钮可将历史记忆归入 5+1 向量空间，或直接新增一条永久记忆。",
                            color = c.muted,
                            fontSize = 12.5.sp
                        )
                    }
                }
            }
            items(visibleNodes, key = { it.id }) { nd ->
                val isCenter = nd.id == selectedNodeId
                val simBadge = when {
                    probeScoreById.containsKey(nd.id) -> "匹配 ${(probeScoreById[nd.id]!! * 100).roundToInt()}%"
                    neighborWeights.containsKey(nd.id) -> "cos ${"%.2f".format(neighborWeights[nd.id]!!)}"
                    isCenter -> "当前焦点"
                    else -> ""
                }
                MemorySpaceItemCard(
                    node = nd,
                    isHighlighted = isCenter || probeScoreById.containsKey(nd.id),
                    simBadge = simBadge,
                    onSelect = {
                        selectedKeyword = null
                        selectedNodeId = if (selectedNodeId == nd.id) null else nd.id
                    },
                    onEdit = { if (nd.itemType == "fact") editTarget = nd },
                    onDelete = { if (nd.itemType == "fact" || nd.itemType == "episode") deleteTarget = nd }
                )
            }
        }
    }

    // Manage Dialog: Reorganize / Add Fact / Rollback Snapshots
    if (showManageDialog) {
        AlertDialog(
            onDismissRequest = { showManageDialog = false },
            containerColor = c.surface,
            titleContentColor = c.ink,
            textContentColor = c.muted,
            title = { Text("记忆空间管理", fontSize = 16.5.sp, fontWeight = FontWeight.SemiBold) },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text(
                        "模型：${space.embeddingModel.ifBlank { "未启用" }} · 事实 ${space.activeFacts} · 事件 ${space.episodes}",
                        fontSize = 12.5.sp,
                        color = c.muted
                    )
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Box(
                            Modifier.weight(1f).clip(RoundedCornerShape(12.dp)).background(c.accent)
                                .iosPressScale(0.96f) {
                                    showManageDialog = false
                                    model.triggerMemoryReorganize()
                                }.padding(vertical = 10.dp),
                            contentAlignment = Alignment.Center
                        ) { Text("一键整理记忆", color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold) }
                        Box(
                            Modifier.weight(1f).clip(RoundedCornerShape(12.dp)).background(c.fill)
                                .iosPressScale(0.96f) {
                                    showManageDialog = false
                                    showAddFactDialog = true
                                }.padding(vertical = 10.dp),
                            contentAlignment = Alignment.Center
                        ) { Text("新增记忆事实", color = c.ink, fontSize = 13.sp, fontWeight = FontWeight.Medium) }
                    }
                    if (backups.isNotEmpty()) {
                        HorizontalDivider(color = c.line.copy(alpha = 0.6f))
                        Text("时光机快照回滚", color = c.ink, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                        backups.take(4).forEach { (bid, _) ->
                            Row(
                                Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(c.fill)
                                    .iosPressScale(0.97f) {
                                        showManageDialog = false
                                        scope.launch {
                                            val r = runCatching {
                                                model.request("POST", "/api/memory/rollback", JSONObject().put("backup_id", bid))
                                            }.getOrNull()
                                            statusNotice = if (r?.optBoolean("ok") == true) "已回滚至快照 $bid" else "回滚失败"
                                            reloadSpace("")
                                        }
                                    }.padding(horizontal = 10.dp, vertical = 8.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                PulseIcon("clock", c.accent, Modifier.size(14.dp))
                                Spacer(Modifier.width(7.dp))
                                Text(bid, color = c.ink, fontSize = 12.sp, modifier = Modifier.weight(1f), maxLines = 1, overflow = TextOverflow.Ellipsis)
                                Text("回滚", color = c.accent, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
                            }
                        }
                    }
                }
            },
            confirmButton = { TextButton(onClick = { showManageDialog = false }) { Text("关闭", color = c.accent) } }
        )
    }

    // Add or Edit Fact Dialog
    val activeEdit = editTarget
    if (showAddFactDialog || activeEdit != null) {
        var fEntity by remember(activeEdit) { mutableStateOf(activeEdit?.entity ?: "") }
        var fAttr by remember(activeEdit) { mutableStateOf(activeEdit?.attr ?: "关键事实") }
        var fVal by remember(activeEdit) { mutableStateOf(activeEdit?.fullText ?: "") }
        AlertDialog(
            onDismissRequest = { showAddFactDialog = false; editTarget = null },
            containerColor = c.surface,
            titleContentColor = c.ink,
            title = { Text(if (activeEdit != null) "编辑记忆事实" else "新增永久记忆事实", fontSize = 16.sp, fontWeight = FontWeight.SemiBold) },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    PulseInputField(label = "主题 / 实体", value = fEntity, placeholder = "例如：VirtualDisplay / 主人偏好", onChange = { fEntity = it })
                    PulseInputField(label = "属性标签", value = fAttr, placeholder = "例如：铁律 / 配置 / 偏好", onChange = { fAttr = it })
                    PulseInputField(label = "记忆内容", value = fVal, placeholder = "具体内容（写入后自动增量生成向量）", onChange = { fVal = it })
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    val ent = fEntity.trim()
                    val v = fVal.trim()
                    if (ent.isNotBlank() && v.isNotBlank()) {
                        showAddFactDialog = false
                        editTarget = null
                        scope.launch {
                            val r = runCatching {
                                model.request(
                                    "POST",
                                    "/api/memory/facts",
                                    JSONObject().put("entity", ent).put("attr", fAttr.trim().ifBlank { "事实" }).put("value", v).put("ttl_type", activeEdit?.ttlType ?: "permanent")
                                )
                            }.getOrNull()
                            statusNotice = if (r?.optBoolean("ok") == true) "已保存并增量更新向量" else "保存失败"
                            reloadSpace(query)
                        }
                    }
                }) { Text("保存", color = c.accent, fontWeight = FontWeight.SemiBold) }
            },
            dismissButton = {
                TextButton(onClick = { showAddFactDialog = false; editTarget = null }) { Text("取消", color = c.muted) }
            }
        )
    }

    // Hard Delete Confirmation
    deleteTarget?.let { target ->
        AlertDialog(
            onDismissRequest = { deleteTarget = null },
            containerColor = c.surface,
            titleContentColor = c.ink,
            textContentColor = c.muted,
            title = { Text("彻底删除这条记忆？", fontSize = 16.sp, fontWeight = FontWeight.SemiBold) },
            text = { Text("将从数据库和向量索引中永久清除「${target.label}」，AI 不会再召回它。", fontSize = 13.sp) },
            confirmButton = {
                TextButton(onClick = {
                    val del = target
                    deleteTarget = null
                    scope.launch {
                        val ep = if (del.itemType == "episode") "/api/memory/episodes/${del.id}?hard=true" else "/api/memory/facts/${del.id}?hard=true"
                        val r = runCatching { model.request("DELETE", ep) }.getOrNull()
                        if (selectedNodeId == del.id) selectedNodeId = null
                        statusNotice = if (r?.optBoolean("ok") == true) "已彻底清除记忆与向量" else "删除失败"
                        reloadSpace(query)
                    }
                }) { Text("彻底删除", color = c.warn, fontWeight = FontWeight.SemiBold) }
            },
            dismissButton = { TextButton(onClick = { deleteTarget = null }) { Text("取消", color = c.accent) } }
        )
    }
}

@Composable
private fun VectorSemanticCanvas(
    space: MemoryVectorSpaceData,
    isDark: Boolean,
    selectedNodeId: String?,
    selectedKeyword: String?,
    keywordNodeIds: Set<String>,
    neighborWeights: Map<String, Float>,
    probeScoreById: Map<String, Float>,
    onSelectNode: (String) -> Unit,
    onToggleCollapse: () -> Unit
) {
    val c = LocalPulsePalette.current
    var zoom by remember { mutableFloatStateOf(1f) }
    var pan by remember { mutableStateOf(Offset.Zero) }
    var canvasSize by remember { mutableStateOf(IntSize(1, 1)) }

    val hasFocus = selectedNodeId != null || selectedKeyword != null || space.probe != null
    val nodesById = remember(space.nodes) { space.nodes.associateBy { it.id } }

    fun toScreen(nx: Float, ny: Float, w: Float, h: Float): Offset {
        val cx = w * 0.5f
        val cy = h * 0.5f
        val bx = (nx * w - cx) * zoom + cx + pan.x
        val by = (ny * h - cy) * zoom + cy + pan.y
        return Offset(bx, by)
    }

    val labelPaint = remember(isDark) {
        Paint(Paint.ANTI_ALIAS_FLAG).apply {
            textSize = 27f
            typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
            textAlign = Paint.Align.CENTER
        }
    }
    val weightPaint = remember(isDark) {
        Paint(Paint.ANTI_ALIAS_FLAG).apply {
            textSize = 23f
            typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD)
            textAlign = Paint.Align.CENTER
        }
    }

    Box(Modifier.fillMaxSize()) {
        Canvas(
            Modifier.fillMaxSize()
                .onSizeChanged { canvasSize = it }
                .pointerInput(space.nodes, zoom, pan) {
                    detectTransformGestures { _, panChange, zoomChange, _ ->
                        zoom = (zoom * zoomChange).coerceIn(0.75f, 3.8f)
                        val maxShift = canvasSize.width.coerceAtLeast(300) * 0.65f * zoom
                        pan = Offset(
                            (pan.x + panChange.x).coerceIn(-maxShift, maxShift),
                            (pan.y + panChange.y).coerceIn(-maxShift, maxShift)
                        )
                    }
                }
                .pointerInput(space.nodes, zoom, pan) {
                    detectTapGestures(
                        onDoubleTap = { zoom = 1f; pan = Offset.Zero },
                        onTap = { tap ->
                            val w = canvasSize.width.toFloat().coerceAtLeast(1f)
                            val h = canvasSize.height.toFloat().coerceAtLeast(1f)
                            var bestId: String? = null
                            var bestDist = 48f
                            for (nd in space.nodes) {
                                val pt = toScreen(nd.x, nd.y, w, h)
                                val d = hypot(pt.x - tap.x, pt.y - tap.y)
                                if (d < bestDist) {
                                    bestDist = d
                                    bestId = nd.id
                                }
                            }
                            if (bestId != null) onSelectNode(bestId)
                        }
                    )
                }
        ) {
            val w = size.width
            val h = size.height

            // Subtle dot grid
            val gridColor = if (isDark) Color(0xFF1E293B).copy(alpha = 0.55f) else Color(0xFFCBD5E1).copy(alpha = 0.55f)
            val step = 36f
            var gx = step
            while (gx < w) {
                var gy = step
                while (gy < h) {
                    drawCircle(gridColor, radius = 1.1f, center = Offset(gx, gy))
                    gy += step
                }
                gx += step
            }

            // 1. Cosine-similarity & keyword edges
            for (e in space.edges) {
                val a = nodesById[e.source] ?: continue
                val b = nodesById[e.target] ?: continue
                val pa = toScreen(a.x, a.y, w, h)
                val pb = toScreen(b.x, b.y, w, h)
                val isHot = when {
                    selectedNodeId != null -> e.source == selectedNodeId || e.target == selectedNodeId
                    selectedKeyword != null -> e.source in keywordNodeIds && e.target in keywordNodeIds
                    space.probe != null -> e.source in probeScoreById && e.target in probeScoreById
                    else -> false
                }
                val baseColor = if (e.kind == "vector") {
                    if (isDark) Color(0xFF38BDF8) else Color(0xFF0284C7)
                } else {
                    if (isDark) Color(0xFFA78BFA) else Color(0xFF7C3AED)
                }
                val alpha = when {
                    isHot -> (0.48f + e.weight * 0.48f).coerceIn(0.45f, 0.96f)
                    hasFocus -> 0.08f
                    else -> (0.14f + e.weight * 0.34f).coerceIn(0.12f, 0.52f)
                }
                val strokeW = if (isHot) (1.8f + e.weight * 2.2f) else (0.9f + e.weight * 1.1f)
                drawLine(
                    color = baseColor.copy(alpha = alpha),
                    start = pa,
                    end = pb,
                    strokeWidth = strokeW,
                    cap = StrokeCap.Round,
                    pathEffect = if (e.kind == "keyword" && !isHot) PathEffect.dashPathEffect(floatArrayOf(6f, 6f)) else null
                )
                if (isHot && selectedNodeId != null) {
                    val mid = Offset((pa.x + pb.x) * 0.5f, (pa.y + pb.y) * 0.5f)
                    drawIntoCanvas { canvas ->
                        weightPaint.color = (if (isDark) Color(0xFFE0F2FE) else Color(0xFF0369A1)).toArgb()
                        canvas.nativeCanvas.drawText("%.2f".format(e.weight), mid.x, mid.y - 6f, weightPaint)
                    }
                }
            }

            // 2. Live query probe rays
            space.probe?.let { pr ->
                val pPt = toScreen(pr.x, pr.y, w, h)
                val probeColor = if (isDark) Color(0xFF34D399) else Color(0xFF059669)
                pr.matches.take(8).forEach { m ->
                    val target = nodesById[m.nodeId] ?: return@forEach
                    val tPt = toScreen(target.x, target.y, w, h)
                    drawLine(
                        color = probeColor.copy(alpha = (0.35f + m.score * 0.6f).coerceIn(0.35f, 0.95f)),
                        start = pPt,
                        end = tPt,
                        strokeWidth = 2.0f + m.score * 1.8f,
                        cap = StrokeCap.Round,
                        pathEffect = PathEffect.dashPathEffect(floatArrayOf(10f, 6f))
                    )
                    val mid = Offset((pPt.x + tPt.x) * 0.5f, (pPt.y + tPt.y) * 0.5f)
                    drawIntoCanvas { canvas ->
                        weightPaint.color = probeColor.toArgb()
                        canvas.nativeCanvas.drawText("${(m.score * 100).roundToInt()}%", mid.x, mid.y - 5f, weightPaint)
                    }
                }
                drawCircle(probeColor.copy(alpha = 0.22f), radius = 19f, center = pPt)
                drawCircle(probeColor, radius = 7.5f, center = pPt)
            }

            // 3. Vector nodes
            for (nd in space.nodes) {
                val pt = toScreen(nd.x, nd.y, w, h)
                val isSelected = nd.id == selectedNodeId
                val isNeighbor = nd.id in neighborWeights
                val isKwHit = nd.id in keywordNodeIds
                val isProbeHit = nd.id in probeScoreById
                val isActive = isSelected || isNeighbor || isKwHit || isProbeHit
                val nodeColor = when (nd.itemType) {
                    "episode" -> if (isDark) Color(0xFFA78BFA) else Color(0xFF7C3AED)
                    "legacy" -> if (isDark) Color(0xFFFBBF24) else Color(0xFFD97706)
                    else -> if (isDark) Color(0xFF38BDF8) else Color(0xFF0284C7)
                }
                val alpha = if (!hasFocus || isActive) 1f else 0.22f
                val baseR = (4.8f + (nd.degree.coerceAtMost(6) * 0.85f)) * zoom.coerceIn(0.85f, 1.5f)
                val r = when {
                    isSelected -> baseR + 4.2f
                    isActive -> baseR + 2.0f
                    else -> baseR
                }
                if (isActive || nd.accessCount > 0) {
                    drawCircle(nodeColor.copy(alpha = if (isSelected) 0.34f else 0.16f * alpha), radius = r * 2.15f, center = pt)
                }
                drawCircle(nodeColor.copy(alpha = alpha), radius = r, center = pt)
                if (isSelected) {
                    drawCircle(Color.White, radius = r * 0.44f, center = pt)
                }
                if (isSelected || isProbeHit || (selectedNodeId != null && isNeighbor)) {
                    drawIntoCanvas { canvas ->
                        labelPaint.textSize = if (isSelected) 28f else 23f
                        labelPaint.color = (if (isDark) Color(0xFFF8FAFC) else Color(0xFF0F172A)).toArgb()
                        canvas.nativeCanvas.drawText(nd.label.take(14), pt.x, pt.y - r - 8f, labelPaint)
                    }
                }
            }

            // 4. Semantic keyword anchors floating over cluster centroids when no single node is focused
            if (selectedNodeId == null && space.probe == null) {
                val anchors = if (selectedKeyword != null) {
                    space.keywords.filter { it.word == selectedKeyword }
                } else {
                    space.keywords.take(10)
                }
                anchors.forEach { kw ->
                    val pt = toScreen(kw.x, kw.y, w, h)
                    val isSel = kw.word == selectedKeyword
                    drawIntoCanvas { canvas ->
                        labelPaint.textSize = if (isSel) 31f else 25f
                        labelPaint.color = when {
                            isSel -> (if (isDark) Color(0xFF7DD3FC) else Color(0xFF0369A1)).toArgb()
                            isDark -> Color(0xFFE2E8F0).copy(alpha = 0.72f).toArgb()
                            else -> Color(0xFF334155).copy(alpha = 0.75f).toArgb()
                        }
                        canvas.nativeCanvas.drawText(kw.word, pt.x, pt.y + 8f, labelPaint)
                    }
                }
            }
        }

        // Bottom-right micro controls (Reset zoom + Collapse)
        Row(
            Modifier.align(Alignment.BottomEnd).padding(8.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp)
        ) {
            if (zoom != 1f || pan != Offset.Zero) {
                Text(
                    "复位",
                    Modifier.clip(RoundedCornerShape(8.dp))
                        .background(c.surface.copy(alpha = 0.85f))
                        .iosPressScale(0.94f) { zoom = 1f; pan = Offset.Zero }
                        .padding(horizontal = 9.dp, vertical = 4.dp),
                    color = c.accent,
                    fontSize = 11.sp,
                    fontWeight = FontWeight.Medium
                )
            }
            Text(
                "收起",
                Modifier.clip(RoundedCornerShape(8.dp))
                    .background(c.surface.copy(alpha = 0.85f))
                    .iosPressScale(0.94f) { onToggleCollapse() }
                    .padding(horizontal = 9.dp, vertical = 4.dp),
                color = c.muted,
                fontSize = 11.sp
            )
        }
    }
}

@Composable
private fun MemorySpaceItemCard(
    node: MemoryVecNode, isHighlighted: Boolean, simBadge: String,
    onSelect: () -> Unit, onEdit: () -> Unit, onDelete: () -> Unit
) {
    val c = LocalPulsePalette.current
    val typeLabel = when (node.itemType) {
        "episode" -> "事件"
        "legacy" -> "待整理"
        else -> when (node.ttlType) { "permanent" -> "永久"; "project" -> "项目"; "ephemeral" -> "临时"; else -> "事实" }
    }
    Column(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(15.dp)).background(c.surface)
            .border(if (isHighlighted) 1.dp else 0.5.dp, if (isHighlighted) c.accent.copy(alpha = 0.85f) else c.line.copy(alpha = 0.65f), RoundedCornerShape(15.dp))
            .iosPressScale(0.985f) { onSelect() }
            .padding(horizontal = 14.dp, vertical = 12.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp)
    ) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(node.label, color = c.ink, fontSize = 14.sp, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
            if (simBadge.isNotBlank()) {
                Spacer(Modifier.width(6.dp))
                Text(simBadge, Modifier.clip(RoundedCornerShape(6.dp)).background(c.accent.copy(alpha = 0.14f)).padding(horizontal = 7.dp, vertical = 2.dp), color = c.accent, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
            }
            Spacer(Modifier.width(6.dp))
            Text(typeLabel, Modifier.clip(RoundedCornerShape(6.dp)).background(c.fill).padding(horizontal = 6.dp, vertical = 2.dp), color = c.muted, fontSize = 10.5.sp)
        }
        Text(
            if (isHighlighted) node.fullText else node.summary,
            color = c.ink.copy(alpha = 0.86f), fontSize = 13.sp, lineHeight = 19.sp,
            maxLines = if (isHighlighted) 8 else 3, overflow = TextOverflow.Ellipsis
        )
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Row(Modifier.weight(1f), horizontalArrangement = Arrangement.spacedBy(5.dp)) {
                node.keywords.take(4).forEach { kw ->
                    Text("#$kw", color = c.muted, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
            }
            if (isHighlighted && (node.itemType == "fact" || node.itemType == "episode")) {
                if (node.itemType == "fact") {
                    Text("编辑", Modifier.clip(RoundedCornerShape(7.dp)).background(c.fill).iosPressScale(0.94f) { onEdit() }.padding(horizontal = 9.dp, vertical = 4.dp), color = c.accent, fontSize = 11.5.sp, fontWeight = FontWeight.Medium)
                    Spacer(Modifier.width(6.dp))
                }
                Text("删除", Modifier.clip(RoundedCornerShape(7.dp)).background(c.warn.copy(alpha = 0.12f)).iosPressScale(0.94f) { onDelete() }.padding(horizontal = 9.dp, vertical = 4.dp), color = c.warn, fontSize = 11.5.sp, fontWeight = FontWeight.Medium)
            }
        }
    }
}
