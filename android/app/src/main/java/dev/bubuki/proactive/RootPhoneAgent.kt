package dev.bubuki.proactive

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Our phone harness: OpenAI-compatible tools execute only in an authenticated Root workspace.
 * Pure Vision (`phone_capture` + normalized `nx, ny`) and Accessibility Node Tree (`phone_ui_tree` + `@eN`)
 * are co-equal peers selected by context.
 */
internal class RootPhoneAgent {
    @Volatile private var currentCall: Call? = null
    private val client = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).readTimeout(90, TimeUnit.SECONDS).callTimeout(110, TimeUnit.SECONDS).build()
    fun cancel() { currentCall?.cancel() }
    data class Outcome(val text: String, val needsUser: Boolean = false)

    suspend fun run(goal: String, config: JSONObject, workspace: JSONObject, history: List<PulseMessage>,
        execute: suspend (String, JSONObject) -> JSONObject, onStep: suspend (JSONObject) -> Unit): Outcome = withContext(Dispatchers.IO) {
        val base = config.getString("base_url").trim().trimEnd('/')
        val endpoint = if (base.endsWith("/chat/completions")) base else "$base/chat/completions"
        require(endpoint.startsWith("https://") || endpoint.startsWith("http://")) { "执行模型地址须为 HTTP(S)" }
        val messages = JSONArray().put(JSONObject().put("role", "system").put("content", """
            你是私人助手内的手机执行引擎，所有操作通过本次 Root 独立虚拟屏工具完成。
            当前工作区：$workspace
            【操作原则（纯视觉与无障碍节点树并重）】：
            1. 打开应用：调用 `phone_launch`（可直接传应用名如 "学习通" 或 package/.Activity）。
            2. 观察界面：纯视觉场景（图文/卡片/WebView/Canvas/状态确认）调用 `phone_capture` 获取带尺寸的截图；标准原生控件/列表也可调用 `phone_ui_tree` 获取 `@e1, @e2` 节点索引与中心坐标，两者平等按需选用。
            3. 点击交互：视觉定位优先用 `phone_tap(nx, ny)`（`0..1000` 归一化比例坐标）或像素坐标 `(x, y)`；节点定位可用 `phone_tap_node(ref="@e1")` 或 `text="..."`。
            4. 文本输入：对指定输入框可用 `phone_type_node(ref, text, clear, submit)`；对已聚焦输入框（或纯视觉先点开输入框后）用 `phone_text(text, clear, submit)`。
            5. 遇到登录、图形验证码、支付或人脸识别，调用 `need_user` 请用户接管。任务完成时直接用简短中文交回结果。
        """.trimIndent()))
        history.takeLast(12).forEach { message -> messages.put(JSONObject().put("role", if (message.role == "user") "user" else "assistant").put("content", message.text.take(6000))) }
        messages.put(JSONObject().put("role", "user").put("content", goal))
        val tools = tools()
        var index = 0
        while (index < 200) {
            currentCoroutineContext().ensureActive()
            val body = JSONObject().put("model", config.getString("model")).put("messages", messages).put("tools", tools).put("stream", false)
            val request = Request.Builder().url(endpoint).header("Authorization", "Bearer " + config.getString("api_key"))
                .post(body.toString().toRequestBody("application/json; charset=utf-8".toMediaType())).build()
            val call = client.newCall(request); currentCall = call
            val response = try { call.execute().use { reply ->
                if (!reply.isSuccessful) throw Exception("执行模型返回 HTTP ${reply.code}，请检查模型连接")
                val source = reply.body ?: throw Exception("执行模型没有返回内容")
                val bytes = Io.read(source.byteStream(), 8_000_000)
                JSONObject(String(bytes, Charsets.UTF_8)).getJSONArray("choices").getJSONObject(0).getJSONObject("message")
            } } catch (error: Exception) { currentCoroutineContext().ensureActive(); throw error } finally { currentCall = null }
            currentCoroutineContext().ensureActive()
            val calls = response.optJSONArray("tool_calls")
            if (calls == null || calls.length() == 0) return@withContext Outcome(response.optString("content").ifBlank { "本轮没有返回可执行动作，请补充一个具体目标。" })
            messages.put(response)
            val images = JSONArray()
            for (position in 0 until calls.length()) {
                currentCoroutineContext().ensureActive()
                val toolCall = calls.getJSONObject(position); val function = toolCall.getJSONObject("function")
                val name = function.getString("name"); val arguments = JSONObject(function.optString("arguments", "{}"))
                if (name == "need_user") return@withContext Outcome(arguments.optString("question").ifBlank { "请在工作区完成当前确认后继续。" }, true)
                val start = android.os.SystemClock.uptimeMillis()
                val result = try { execute(name, arguments) } catch (error: Exception) {
                    if (error is kotlinx.coroutines.CancellationException) throw error
                    JSONObject().put("error", MainActivity.safeError(error))
                }
                val image = result.optString("image").ifBlank { result.optString("_image_b64") }
                val clean = RootPhoneHarness.shallowCopyWithoutImage(result)
                // Keep tool response compact: if `ui_tree` has `summary`, omit verbose `nodes` array in LLM tool message
                if (name == "phone_ui_tree" && clean.has("summary")) {
                    clean.remove("nodes")
                }
                val trace = JSONObject().put("step", index + 1).put("tool", name).put("arguments", arguments)
                    .put("result", clean).put("duration_ms", android.os.SystemClock.uptimeMillis() - start)
                onStep(trace)
                messages.put(JSONObject().put("role", "tool").put("tool_call_id", toolCall.getString("id")).put("content", clean.toString()))
                if (image.isNotBlank()) {
                    val mime = if (image.startsWith("iVBOR")) "image/png" else "image/jpeg"
                    val lw = clean.optInt("width")
                    val lh = clean.optInt("height")
                    val iw = clean.optInt("image_width", lw)
                    val ih = clean.optInt("image_height", lh)
                    images.put(JSONObject().put("type", "text").put("text", "刚刚获取的工作区截图，显示逻辑尺寸 ${lw}×${lh}，编码图尺寸 ${iw}×${ih}，display_id=${clean.optInt("displayId")}。推荐使用归一化比例坐标 nx, ny (0..1000)。"))
                    images.put(JSONObject().put("type", "image_url").put("image_url", JSONObject().put("url", "data:$mime;base64,$image")))
                }
            }
            if (images.length() > 0) {
                // Keep a 3-frame rolling visual window (initial anchor + previous + latest)
                messages.put(JSONObject().put("role", "user").put("content", images))
                val imgMsgIndices = mutableListOf<Int>()
                for (mIdx in 0 until messages.length()) {
                    val msg = messages.optJSONObject(mIdx) ?: continue
                    if (msg.optString("role") == "user") {
                        val contentArr = msg.optJSONArray("content") ?: continue
                        for (cIdx in 0 until contentArr.length()) {
                            if (contentArr.optJSONObject(cIdx)?.optString("type") == "image_url") {
                                imgMsgIndices.add(mIdx)
                                break
                            }
                        }
                    }
                }
                if (imgMsgIndices.size > 3) {
                    val keepSet = setOf(imgMsgIndices.first(), imgMsgIndices[imgMsgIndices.size - 2], imgMsgIndices.last())
                    for (mIdx in imgMsgIndices) {
                        if (mIdx !in keepSet) {
                            messages.optJSONObject(mIdx)?.put("content", "[历史工作区截图已省略]")
                        }
                    }
                }
            }
            index++
        }
        Outcome("本轮连续执行步骤较多已暂存进度，你可以查看执行记录，或继续发送下一条指令。", false)
    }

    private fun tools(): JSONArray {
        fun field(type: String, description: String) = JSONObject().put("type", type).put("description", description)
        fun definition(name: String, description: String, fields: Map<String, JSONObject> = emptyMap(), required: List<String> = fields.keys.toList()): JSONObject =
            JSONObject().put("type", "function").put("function", JSONObject().put("name", name).put("description", description)
                .put("parameters", JSONObject().put("type", "object").put("properties", JSONObject(fields)).put("required", JSONArray(required)).put("additionalProperties", false)))
        val display = field("integer", "本次工作区 displayId，必须大于 0").put("minimum", 1)
        val nx = field("integer", "归一化水平坐标 0..1000（推荐）").put("minimum", 0).put("maximum", 1000)
        val ny = field("integer", "归一化垂直坐标 0..1000（推荐）").put("minimum", 0).put("maximum", 1000)
        val x = field("integer", "逻辑屏幕水平像素坐标").put("minimum", 0)
        val y = field("integer", "逻辑屏幕垂直像素坐标").put("minimum", 0)
        return JSONArray(listOf(
            definition("phone_launch", "在独立虚拟屏启动应用（直接传应用名称如 '学习通' 或组件名）", mapOf("display_id" to display, "component" to field("string", "应用名称（如 '学习通'）或 package/.Activity")), listOf("component")),
            definition("phone_capture", "截取当前独立虚拟屏画面（纯视觉观察）", mapOf("display_id" to display, "grid" to field("boolean", "是否叠加参考网格")), emptyList()),
            definition("phone_tap", "按坐标点击独立虚拟屏（优先传 nx, ny ∈ [0, 1000]，也兼容像素 x, y）", mapOf("display_id" to display, "nx" to nx, "ny" to ny, "x" to x, "y" to y), emptyList()),
            definition("phone_text", "向当前已聚焦输入框输入文字或清空（可独立配合 clear/submit）", mapOf("display_id" to display, "text" to field("string", "待输入文字，最多 4000 字"), "clear" to field("boolean", "是否先清空输入框内容"), "submit" to field("boolean", "输入后是否发送回车提交")), emptyList()),
            definition("phone_ui_tree", "读取当前独立虚拟屏的无障碍 UI 节点树，返回带 @e1, @e2 编号、角色、文字与中心坐标的紧凑元素列表", mapOf("display_id" to display), emptyList()),
            definition("phone_tap_node", "根据 phone_ui_tree 返回的节点编号 ref（如 '@e3'）或可见文字 text 精准点击控件", mapOf("display_id" to display, "ref" to field("string", "节点编号，如 '@e1'"), "text" to field("string", "控件上的文字或描述，如 '签到' / '作业'"), "id" to field("string", "可选 resource_id")), emptyList()),
            definition("phone_type_node", "在指定输入框节点（ref 或 target 提示词）中输入文字", mapOf("display_id" to display, "ref" to field("string", "输入框节点编号，如 '@e2'"), "target" to field("string", "可选输入框提示文字"), "text" to field("string", "要输入的文字"), "clear" to field("boolean", "是否先清空输入框"), "submit" to field("boolean", "输入后是否提交回车")), emptyList()),
            definition("phone_swipe", "在独立虚拟屏滑动（如上下翻页，支持 nx1/ny1/nx2/ny2 或 x1/y1/x2/y2）", mapOf("display_id" to display, "x1" to x, "y1" to y, "x2" to x, "y2" to y, "nx1" to nx, "ny1" to ny, "nx2" to nx, "ny2" to ny), emptyList()),
            definition("phone_key", "发送独立屏按键", mapOf("display_id" to display, "key" to field("string", "工作区按键").put("enum", JSONArray(listOf("back", "home", "enter", "tab", "delete", "up", "down", "left", "right")))), listOf("key")),
            definition("phone_apps", "列出此手机可启动的应用与组件名"),
            definition("phone_status", "读取当前 Root / 无线 ADB 工作区状态"),
            definition("phone_read_otp", "读取最近短信与通知栏里的验证码（按时间倒序提取候选验证码）", mapOf("query" to field("string", "可选筛选关键词或应用名"), "limit" to field("integer", "最多检查条数，默认 15")), emptyList()),
            definition("need_user", "需要用户完成登录、验证码或授权时停止并请求接管", mapOf("question" to field("string", "用户需要处理的具体事项")))
        ))
    }
}
