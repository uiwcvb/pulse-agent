package dev.bubuki.proactive

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.PowerManager
import android.os.SystemClock
import kotlinx.coroutines.flow.update
import org.json.JSONArray
import org.json.JSONObject

/**
 * [Module] RootPhoneDeviceShell
 * [Responsibility] Device telemetry (`device_info`), notifications dump (`notifications`),
 *                  sandboxed shell execution (`shell`), and launcher app resolution (`apps`/`launch`).
 */

internal fun RootPhoneHarness.buildDeviceInfo(base: Context): JSONObject {
    val out = JSONObject().put("ok", true).put("op", "device_info")
    runCatching {
        val bm = base.getSystemService(android.os.BatteryManager::class.java)
        val intent = base.registerReceiver(null, android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = bm?.getIntProperty(android.os.BatteryManager.BATTERY_PROPERTY_CAPACITY)
            ?: intent?.getIntExtra(android.os.BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val statusInt = intent?.getIntExtra(android.os.BatteryManager.EXTRA_STATUS, -1) ?: -1
        val charging = statusInt == android.os.BatteryManager.BATTERY_STATUS_CHARGING ||
            statusInt == android.os.BatteryManager.BATTERY_STATUS_FULL ||
            (bm?.isCharging == true)
        val tempTenths = intent?.getIntExtra(android.os.BatteryManager.EXTRA_TEMPERATURE, 0) ?: 0
        out.put("battery", JSONObject()
            .put("level", level)
            .put("charging", charging)
            .put("temp_c", if (tempTenths > 0) tempTenths / 10.0 else 0.0)
        )
    }
    runCatching {
        val am = base.getSystemService(android.app.ActivityManager::class.java)
        val mi = android.app.ActivityManager.MemoryInfo()
        am?.getMemoryInfo(mi)
        out.put("memory", JSONObject()
            .put("total_mb", mi.totalMem / (1024 * 1024))
            .put("avail_mb", mi.availMem / (1024 * 1024))
            .put("low_memory", mi.lowMemory)
        )
    }
    runCatching {
        val stat = android.os.StatFs(android.os.Environment.getExternalStorageDirectory().path)
        val freeGb = (stat.availableBytes.toDouble() / (1024.0 * 1024.0 * 1024.0) * 10.0).toInt() / 10.0
        val totalGb = (stat.totalBytes.toDouble() / (1024.0 * 1024.0 * 1024.0) * 10.0).toInt() / 10.0
        out.put("storage", JSONObject().put("free_gb", freeGb).put("total_gb", totalGb))
    }
    runCatching {
        val cm = base.getSystemService(android.net.ConnectivityManager::class.java)
        val activeCaps = cm?.activeNetwork?.let { cm.getNetworkCapabilities(it) }
        val hasWifi = activeCaps?.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI) == true
        val hasCellular = activeCaps?.hasTransport(android.net.NetworkCapabilities.TRANSPORT_CELLULAR) == true
        val hasVpn = activeCaps?.hasTransport(android.net.NetworkCapabilities.TRANSPORT_VPN) == true
        val connected = activeCaps != null && (
            activeCaps.hasCapability(android.net.NetworkCapabilities.NET_CAPABILITY_INTERNET) || hasWifi || hasCellular
            )
        val netType = when {
            activeCaps == null -> "none"
            hasWifi -> "wifi"
            hasCellular -> "cellular"
            else -> "other"
        }
        out.put("network", JSONObject()
            .put("connected", connected)
            .put("type", netType)
            .put("vpn", hasVpn)
        )
    }
    runCatching {
        val pm = base.getSystemService(PowerManager::class.java)
        val km = base.getSystemService(android.app.KeyguardManager::class.java)
        val audio = base.getSystemService(android.media.AudioManager::class.java)
        val ringer = when (audio?.ringerMode) {
            android.media.AudioManager.RINGER_MODE_SILENT -> "silent"
            android.media.AudioManager.RINGER_MODE_VIBRATE -> "vibrate"
            else -> "normal"
        }
        out.put("screen", JSONObject()
            .put("interactive", pm?.isInteractive == true)
            .put("locked", km?.isKeyguardLocked == true)
            .put("ringer_mode", ringer)
            .put("music_active", audio?.isMusicActive == true)
        )
    }
    out.put("device", JSONObject()
        .put("model", Build.MODEL ?: "")
        .put("brand", Build.BRAND ?: "")
        .put("sdk", Build.VERSION.SDK_INT)
        .put("release", Build.VERSION.RELEASE ?: "")
    )
    return out
}

internal fun splitShellSegments(rawCmd: String): List<Pair<String, String>> {
    val out = ArrayList<Pair<String, String>>()
    var start = 0
    var i = 0
    var inSingle = false
    var inDouble = false
    var escaped = false
    val len = rawCmd.length
    while (i < len) {
        val ch = rawCmd[i]
        if (inSingle) {
            if (ch == '\'') inSingle = false
            i++
            continue
        }
        if (escaped) {
            escaped = false
            i++
            continue
        }
        if (ch == '\\') {
            escaped = true
            i++
            continue
        }
        if (ch == '"') {
            inDouble = !inDouble
            i++
            continue
        }
        if (!inDouble && ch == '\'') {
            inSingle = true
            i++
            continue
        }
        if (!inDouble) {
            if (rawCmd.startsWith("&&", i) || rawCmd.startsWith("||", i)) {
                out.add(rawCmd.substring(start, i) to rawCmd.substring(i, i + 2))
                i += 2
                start = i
                continue
            }
            if (ch == ';' || ch == '|' || ch == '\n') {
                out.add(rawCmd.substring(start, i) to ch.toString())
                i += 1
                start = i
                continue
            }
        }
        i++
    }
    if (start <= len) {
        out.add(rawCmd.substring(start) to "")
    }
    return out
}

internal fun stripQuotedStrings(segment: String): String {
    val sb = StringBuilder(segment.length)
    var i = 0
    var inSingle = false
    var inDouble = false
    var escaped = false
    while (i < segment.length) {
        val ch = segment[i]
        if (inSingle) {
            if (ch == '\'') {
                inSingle = false
                sb.append('\'')
            }
            i++
            continue
        }
        if (escaped) {
            escaped = false
            sb.append(' ')
            i++
            continue
        }
        if (ch == '\\') {
            escaped = true
            i++
            continue
        }
        if (ch == '"') {
            inDouble = !inDouble
            sb.append('"')
            i++
            continue
        }
        if (inDouble) {
            i++
            continue
        }
        if (ch == '\'') {
            inSingle = true
            sb.append('\'')
            i++
            continue
        }
        sb.append(ch)
        i++
    }
    return sb.toString()
}

private fun findShellCommandStart(segment: String): Int {
    var i = 0
    val len = segment.length
    while (i < len) {
        while (i < len && segment[i].isWhitespace()) i++
        if (i >= len) break
        val tokStart = i
        var seenEq = false
        var validVarPrefix = true
        var inSingle = false
        var inDouble = false
        var escaped = false
        while (i < len) {
            val ch = segment[i]
            if (inSingle) {
                if (ch == '\'') inSingle = false
                i++
                continue
            }
            if (escaped) {
                escaped = false
                i++
                continue
            }
            if (ch == '\\') {
                escaped = true
                i++
                continue
            }
            if (ch == '"') {
                inDouble = !inDouble
                if (!seenEq) validVarPrefix = false
                i++
                continue
            }
            if (!inDouble && ch == '\'') {
                inSingle = true
                if (!seenEq) validVarPrefix = false
                i++
                continue
            }
            if (!inDouble && ch.isWhitespace()) break
            if (!seenEq) {
                if (ch == '=') {
                    val namePart = segment.substring(tokStart, i)
                    validVarPrefix = namePart.isNotEmpty() &&
                        (namePart[0].isLetter() || namePart[0] == '_') &&
                        namePart.all { it.isLetterOrDigit() || it == '_' }
                    seenEq = true
                } else if (!(ch.isLetterOrDigit() || ch == '_')) {
                    validVarPrefix = false
                }
            }
            i++
        }
        if (seenEq && validVarPrefix) continue
        return tokStart
    }
    return len
}

private val RE_GLOBAL_SETTINGS_MUTATION = Regex(
    """\bcmd\s+uimode\b|\bservice\s+call\s+uimode\b|\bsettings\b(?:\s+--user\s+\S+)?\s+put\s+(?:system|secure|global)\s+(?:ui_night_mode|screen_brightness|screen_off_timeout|font_scale|user_rotation|accelerometer_rotation|default_input_method|enabled_input_methods)\b""",
    RegexOption.IGNORE_CASE
)
private val RE_MONKEY_CMD = Regex("""(?:^|\s)(?:/[^\s/]+/)?monkey(?=\s|$)""", RegexOption.IGNORE_CASE)
private val RE_DISPLAY_ZERO_FLAG = Regex(
    """(?:^|\s)(?:--display(?:=|\s+)|--display-id(?:=|\s+)|-d\s*)0(?=\s|$)""",
    RegexOption.IGNORE_CASE
)
private val RE_WM_MUTATION = Regex("""\bwm\s+(?:size|density|overscan|user-rotation)\b""", RegexOption.IGNORE_CASE)
private val RE_HAS_MINUS_D = Regex("""(?:^|\s)-d\s*\d+(?=\s|$)""")
private val RE_HAS_DISPLAY_FLAG = Regex("""(?:^|\s)(?:--display(?:=|\s+)|--display-id(?:=|\s+))\d+(?=\s|$)""", RegexOption.IGNORE_CASE)
private val RE_HEAD_INPUT = Regex("""^((?:/[^\s/]+/)?input\s+)""")
private val RE_HEAD_AM_START = Regex("""^((?:/[^\s/]+/)?am\s+start(?:-activity)?\s+)""")
private val RE_HEAD_CMD_START = Regex("""^((?:/[^\s/]+/)?cmd\s+activity\s+start(?:-activity)?\s+)""")
private val RE_HEAD_SCREENCAP = Regex("""^((?:/[^\s/]+/)?screencap)(?=\s|$)""")

internal fun validateShellCommandSandbox(cmdStr: String, waitingForeground: Boolean, currentDisp: Int) {
    for ((seg, _) in splitShellSegments(cmdStr)) {
        val unquoted = stripQuotedStrings(seg)
        if (RE_GLOBAL_SETTINGS_MUTATION.containsMatchIn(unquoted)) {
            throw Exception("安全沙箱已拦截全局系统设置修改指令（严禁修改主屏深浅色 Night mode、亮度、字体缩放或输入法等系统全局设置）")
        }
        if (!waitingForeground) {
            if (RE_MONKEY_CMD.containsMatchIn(unquoted)) {
                throw Exception("安全沙箱已拦截 monkey 指令（monkey 会直接操作主屏幕 Display 0），请改用 phone_workspace(op=\"open\") 或 am start")
            }
            if (RE_DISPLAY_ZERO_FLAG.containsMatchIn(unquoted)) {
                throw Exception("安全沙箱已拦截指向主屏幕 (Display 0) 的指令，严禁在未接管状态下操作或启动应用到 Display 0")
            }
            if (currentDisp > 0 && RE_WM_MUTATION.containsMatchIn(unquoted) &&
                !Regex("""(?:^|\s)-d\s*${currentDisp}(?=\s|$)""").containsMatchIn(unquoted)
            ) {
                throw Exception("安全沙箱已拦截未绑定虚拟屏 (-d $currentDisp) 的 wm 显示参数修改指令，禁止修改主屏幕分辨率/DPI")
            }
        }
    }
}

internal fun sandboxShellCommandToDisplay(rawCmd: String, displayId: Int): String {
    if (displayId <= 0) return rawCmd
    val sb = StringBuilder(rawCmd.length + 32)
    for ((seg, delim) in splitShellSegments(rawCmd)) {
        val cmdStart = findShellCommandStart(seg)
        val prefix = seg.substring(0, cmdStart)
        var rest = seg.substring(cmdStart)
        val unquotedRest = stripQuotedStrings(rest)
        when {
            RE_HEAD_INPUT.containsMatchIn(unquotedRest) && !RE_HAS_MINUS_D.containsMatchIn(unquotedRest) -> {
                val m = RE_HEAD_INPUT.find(rest)
                if (m != null) rest = "${m.groupValues[1]}-d $displayId ${rest.substring(m.range.last + 1)}"
            }
            RE_HEAD_AM_START.containsMatchIn(unquotedRest) && !RE_HAS_DISPLAY_FLAG.containsMatchIn(unquotedRest) -> {
                val m = RE_HEAD_AM_START.find(rest)
                if (m != null) rest = "${m.groupValues[1]}--display $displayId ${rest.substring(m.range.last + 1)}"
            }
            RE_HEAD_CMD_START.containsMatchIn(unquotedRest) && !RE_HAS_DISPLAY_FLAG.containsMatchIn(unquotedRest) -> {
                val m = RE_HEAD_CMD_START.find(rest)
                if (m != null) rest = "${m.groupValues[1]}--display $displayId ${rest.substring(m.range.last + 1)}"
            }
            RE_HEAD_SCREENCAP.containsMatchIn(unquotedRest) && !RE_HAS_MINUS_D.containsMatchIn(unquotedRest) -> {
                val m = RE_HEAD_SCREENCAP.find(rest)
                if (m != null) rest = "${m.groupValues[1]} -d $displayId${rest.substring(m.range.last + 1)}"
            }
        }
        sb.append(prefix).append(rest).append(delim)
    }
    return sb.toString()
}

internal fun RootPhoneHarness.executeShellAction(base: Context, input: JSONObject): JSONObject {
    var cmdStr = input.optString("command", input.optString("cmd", input.optString("text", ""))).trim()
    if (cmdStr.isEmpty()) throw Exception("请提供要执行的 shell command")
    val waitingForeground = state.value.phase == "waiting_user"
    validateShellCommandSandbox(cmdStr, waitingForeground, currentDisp = -1)
    var dispObj = runCatching { JSONObject(state.value.display) }.getOrNull()
    var currentDisp = if (dispObj?.optBoolean("created", false) == true) dispObj.optInt("displayId", -1) else -1
    val touchesDisplayUi = splitShellSegments(cmdStr).any { (seg, _) ->
        val unquoted = stripQuotedStrings(seg)
        Regex(
            """\b(input|screencap|uiautomator)\b|\bam\s+(start|start-activity|display)\b|\bcmd\s+activity\s+(start|start-activity)|\bwm\s+(size|density|overscan|user-rotation)\b""",
            RegexOption.IGNORE_CASE
        ).containsMatchIn(unquoted)
    }
    if (!waitingForeground && touchesDisplayUi && currentDisp <= 0) {
        runCatching {
            val woke = command(base, JSONObject().put("op", "create").put("skip_home", false))
            currentDisp = woke.optInt("displayId", -1)
            dispObj = runCatching { JSONObject(state.value.display) }.getOrNull()
        }
        if (currentDisp <= 0) {
            throw Exception("虚拟屏尚未就绪 (displayId=$currentDisp)，安全沙箱已禁止 shell 指令操作或截取主屏幕 (Display 0)")
        }
    }
    val dW = dispObj?.optInt("width", 720)?.coerceAtLeast(1) ?: 720
    val dH = dispObj?.optInt("height", 1600)?.coerceAtLeast(1) ?: 1600
    val isBareUiautomatorDump = cmdStr.startsWith("uiautomator", ignoreCase = true) &&
        cmdStr.contains("dump", ignoreCase = true) &&
        !cmdStr.contains("&&") && !cmdStr.contains(";") && !cmdStr.contains("|")
    if (isBareUiautomatorDump && currentDisp > 0) {
        val treeJson = readUiTreeForDisplay(currentDisp, dW, dH, forceRefresh = true)
        return JSONObject()
            .put("ok", true)
            .put("uid", 0)
            .put("exit_code", 0)
            .put("output", treeJson.optString("summary", ""))
            .put("cwd", shellCwd)
    }
    if (!waitingForeground && currentDisp > 0) {
        validateShellCommandSandbox(cmdStr, waitingForeground, currentDisp)
        cmdStr = sandboxShellCommandToDisplay(cmdStr, currentDisp)
    }
    val hasPriv = privilege!!.isPrivilegedReady() || runCatching {
        val p = privilege!!.probe()
        p.optBoolean("privileged", p.optBoolean("root"))
    }.getOrDefault(false)
    if (!hasPriv) {
        throw Exception("尚未取得 Root、Shizuku 或无线 ADB 特权授权，无法执行特权 shell")
    }
    val safeCwd = "'" + shellCwd.replace("'", "'\\''") + "'"
    val wrappedCmd = "cd $safeCwd 2>/dev/null || cd /sdcard; $cmdStr; __pulse_ec=\$?; echo ''; echo '__PULSE_CWD__:'\$(pwd); exit \$__pulse_ec"
    val rawRes = try {
        privilege!!.execute(JSONObject().put("op", "shell").put("command", wrappedCmd))
    } catch (rpcErr: Throwable) {
        val timedOutRpc = rpcErr.message?.contains("超时") == true ||
            rpcErr.message?.contains("timeout", ignoreCase = true) == true
        if (privilege?.getPrivilegeMode() != "root" || timedOutRpc) throw rpcErr
        val proc = ProcessBuilder("su", "-c", wrappedCmd).redirectErrorStream(true).start()
        val outBuf = StringBuilder()
        val readerThread = Thread {
            runCatching {
                proc.inputStream.bufferedReader().use { br ->
                    val buf = CharArray(4096)
                    while (true) {
                        val n = br.read(buf)
                        if (n <= 0) break
                        if (outBuf.length < 32768) {
                            outBuf.append(buf, 0, n.coerceAtMost(32768 - outBuf.length))
                        }
                    }
                }
            }
        }.apply { isDaemon = true; name = "pulse-su-drain"; start() }
        val finished = proc.waitFor(20, java.util.concurrent.TimeUnit.SECONDS)
        if (!finished) proc.destroyForcibly()
        runCatching { readerThread.join(1000L) }
        val ec = if (finished) proc.exitValue() else 124
        JSONObject()
            .put("uid", 0)
            .put("ok", finished && ec == 0)
            .put("exit_code", ec)
            .put("timeout", !finished)
            .put("output", outBuf.toString())
            .put("direct_su_fallback", true)
    }
    var rawOut = rawRes.optString("output", "")
    val markerIdx = rawOut.lastIndexOf("__PULSE_CWD__:")
    if (markerIdx >= 0) {
        val newCwd = rawOut.substring(markerIdx + "__PULSE_CWD__:".length).trim().lineSequence().firstOrNull()?.trim().orEmpty()
        if (newCwd.startsWith("/")) shellCwd = newCwd
        rawOut = rawOut.substring(0, markerIdx).trimEnd()
    }
    if (rawOut.length > 16000) {
        rawOut = rawOut.take(12000) + "\n...[输出过长已截断，原长 ${rawOut.length} 字符]...\n" + rawOut.takeLast(3000)
    }
    val timedOut = rawRes.optBoolean("timeout", false)
    val exitCode = rawRes.optInt("exit_code", if (timedOut) 124 else 0)
    val ok = !timedOut && exitCode == 0 && rawRes.optBoolean("ok", true)
    val outJson = JSONObject()
        .put("ok", ok)
        .put("uid", rawRes.optInt("uid", 0))
        .put("exit_code", exitCode)
        .put("timeout", timedOut)
        .put("output", rawOut)
        .put("cwd", shellCwd)
    if (timedOut) {
        outJson.put("partial_output", rawOut)
    }
    return outJson
}

private fun cleanDumpsysExtraValue(rawVal: String): String {
    var s = rawVal.trim()
    if (s.isEmpty() || s == "null") return ""
    // Strip Android 10-15 dumpsys type wrappers:
    //   String (微信)
    //   String [length=4] (微信)
    //   SpannableString (验证码 123456)
    //   SpannableString [length=12] (验证码 123456)
    //   CharSequence (...)
    val parenMatch = Regex("""^[A-Za-z0-9_.$]+(?:\s*\[[^\]]*\])?\s*\((.*)\)$""").find(s)
    if (parenMatch != null) {
        s = parenMatch.groupValues[1].trim()
    } else {
        s = s.removePrefix("String (").removeSuffix(")").trim()
    }
    if (s == "null" || s == "REDACTED") return ""
    return s
}

internal fun RootPhoneHarness.executeNotificationsAction(base: Context, input: JSONObject): JSONObject {
    val filterQ = input.optString(
        "filter_app",
        input.optString("filter_package", input.optString("app", input.optString("query", input.optString("package", ""))))
    ).trim()
    val limit = input.optInt("limit", 25).coerceIn(1, 80)
    fun hasPrivilegedShell(): Boolean = privilege?.isPrivilegedReady() == true || runCatching {
        val p = privilege?.probe()
        p?.optBoolean("privileged", p.optBoolean("root")) == true
    }.getOrDefault(false)

    val privReady = hasPrivilegedShell()
    if (!AgentNotificationListener.isConnected() && privReady) {
        runCatching {
            val comp = "${base.packageName}/${base.packageName}.AgentNotificationListener"
            privilege!!.execute(
                JSONObject()
                    .put("op", "shell")
                    .put("command", "cmd notification allow_listener $comp >/dev/null 2>&1")
            )
            if (Build.VERSION.SDK_INT >= 24) {
                runCatching {
                    android.service.notification.NotificationListenerService.requestRebind(
                        android.content.ComponentName(base, AgentNotificationListener::class.java)
                    )
                }
            }
            for (step in 0 until 9) {
                if (AgentNotificationListener.isConnected()) break
                Thread.sleep(100)
            }
        }
    }

    var items = AgentNotificationListener.getRecentNotifications(base, filterQ, limit)
    var sourceTag = if (AgentNotificationListener.isConnected()) "listener" else "none"
    var dumpsysErr = ""

    if (items.length() == 0 && privReady) {
        runCatching {
            // Focus on active Notification List section (avoid head -n 300 truncating before active records!)
            val dumpRes = privilege!!.execute(
                JSONObject()
                    .put("op", "shell")
                    .put(
                        "command",
                        "dumpsys notification --noredact 2>&1 | awk '/Notification List:/,/Notification History:|Zen Mode:|Snoozed notifications:/' | grep -E 'NotificationRecord\\(|pkg=|tickerText=|android\\.title=|android\\.text=|android\\.bigText=|android\\.subText=|postTime=|when=' | head -n 900"
                    )
            )
            var rawOut = dumpRes.optString("output", dumpRes.optString("stdout", ""))
            if (!rawOut.contains("NotificationRecord(")) {
                val fallbackRes = privilege!!.execute(
                    JSONObject()
                        .put("op", "shell")
                        .put(
                            "command",
                            "dumpsys notification --noredact 2>&1 | grep -E 'NotificationRecord\\(|pkg=|tickerText=|android\\.title=|android\\.text=|android\\.bigText=|android\\.subText=|postTime=' | tail -n 600"
                        )
                )
                rawOut = fallbackRes.optString("output", fallbackRes.optString("stdout", ""))
            }
            val parsed = JSONArray()
            var curPkg = ""
            var curTitle = ""
            var curText = ""
            var curTs = 0L
            val seenKeys = HashSet<String>()
            fun flushRecord() {
                if (curPkg.isNotBlank() && curPkg != base.packageName && curPkg != "android" && curPkg != "com.android.systemui" && (curTitle.isNotBlank() || curText.isNotBlank())) {
                    val appLabel = AgentNotificationListener.resolveAppName(base, curPkg)
                    val hay = "$appLabel $curPkg $curTitle $curText".lowercase()
                    val dedup = "$curPkg|$curTitle|$curText"
                    if ((filterQ.isBlank() || hay.contains(filterQ.lowercase())) && seenKeys.add(dedup)) {
                        parsed.put(
                            JSONObject()
                                .put("package", curPkg)
                                .put("app", appLabel)
                                .put("title", curTitle)
                                .put("text", curText)
                                .put("timestamp_ms", if (curTs > 0L) curTs else System.currentTimeMillis())
                                .put("active", true)
                        )
                    }
                }
                curTitle = ""
                curText = ""
                curTs = 0L
            }
            for (line in rawOut.lineSequence()) {
                val s = line.trim()
                when {
                    s.contains("NotificationRecord(") -> {
                        flushRecord()
                        val m = Regex("""pkg=([^\s:}]+)""").find(s)
                        curPkg = m?.groupValues?.get(1)?.trim() ?: ""
                    }
                    s.contains("postTime=") -> {
                        val pm = Regex("""postTime=(\d+)""").find(s)
                        curTs = pm?.groupValues?.get(1)?.toLongOrNull() ?: curTs
                    }
                    s.startsWith("android.title=") -> {
                        curTitle = cleanDumpsysExtraValue(s.substringAfter("android.title="))
                    }
                    s.startsWith("android.bigText=") -> {
                        val bt = cleanDumpsysExtraValue(s.substringAfter("android.bigText="))
                        if (bt.isNotBlank()) curText = bt
                    }
                    s.startsWith("android.text=") && curText.isBlank() -> {
                        val tx = cleanDumpsysExtraValue(s.substringAfter("android.text="))
                        if (tx.isNotBlank()) curText = tx
                    }
                    s.startsWith("android.subText=") && curText.isBlank() -> {
                        val st = cleanDumpsysExtraValue(s.substringAfter("android.subText="))
                        if (st.isNotBlank()) curText = st
                    }
                    s.startsWith("tickerText=") && curText.isBlank() -> {
                        val tk = cleanDumpsysExtraValue(s.substringAfter("tickerText="))
                        if (tk.isNotBlank()) curText = tk
                    }
                }
                if (parsed.length() >= limit) break
            }
            flushRecord()
            if (parsed.length() > 0) {
                items = parsed
                sourceTag = "dumpsys"
            }
        }.onFailure { err ->
            dumpsysErr = err.message ?: err.javaClass.simpleName
        }
    }

    val listenerConnected = AgentNotificationListener.isConnected()
    if (!listenerConnected && items.length() == 0 && !privReady) {
        return JSONObject()
            .put("ok", false)
            .put("op", "notifications")
            .put("listener_connected", false)
            .put("count", 0)
            .put("notifications", items)
            .put("error", "通知监听服务未开启且未连接 Shizuku/Root 特权：请在手机系统设置 → 通知使用权 中开启「DSB 通知状态监听」")
    }

    return JSONObject()
        .put("ok", true)
        .put("op", "notifications")
        .put("source", sourceTag)
        .put("listener_connected", listenerConnected)
        .put("privileged", privReady)
        .put("count", items.length())
        .put("notifications", items)
        .apply { if (dumpsysErr.isNotBlank()) put("dumpsys_warning", dumpsysErr) }
}

internal fun RootPhoneHarness.executeReadOtpAction(base: Context, input: JSONObject): JSONObject {
    val filterQ = input.optString("query", input.optString("app", "")).trim()
    val limit = input.optInt("limit", 15).coerceIn(1, 40)
    val merged = mutableListOf<JSONObject>()
    val notifRes = executeNotificationsAction(base, JSONObject().put("query", filterQ).put("limit", 50))
    val notifArr = notifRes.optJSONArray("notifications") ?: JSONArray()
    val noiseRe = Regex("""nekobox|matsuri|v2ray|clash|flclash|sing-?box|shadowsocks|vpn|proxy|网速|已连接|正在运行""", RegexOption.IGNORE_CASE)
    for (i in 0 until notifArr.length()) {
        val it = notifArr.optJSONObject(i) ?: continue
        val hay = "${it.optString("package")} ${it.optString("app")} ${it.optString("title")} ${it.optString("text")}"
        if (filterQ.isBlank() && noiseRe.containsMatchIn(hay) && !hay.contains("验证码") && !hay.contains("code", ignoreCase = true)) {
            continue
        }
        merged += JSONObject(it.toString()).put("channel", "notification")
    }
    val privReady = privilege?.isPrivilegedReady() == true || runCatching {
        val p = privilege?.probe()
        p?.optBoolean("privileged", p.optBoolean("root")) == true
    }.getOrDefault(false)

    var smsGranted = base.checkSelfPermission(android.Manifest.permission.READ_SMS) == android.content.pm.PackageManager.PERMISSION_GRANTED
    if (!smsGranted && privReady) {
        runCatching {
            val pkg = base.packageName
            privilege!!.execute(
                JSONObject()
                    .put("op", "shell")
                    .put("command", "pm grant $pkg android.permission.READ_SMS >/dev/null 2>&1; appops set $pkg READ_SMS allow >/dev/null 2>&1")
            )
            smsGranted = base.checkSelfPermission(android.Manifest.permission.READ_SMS) == android.content.pm.PackageManager.PERMISSION_GRANTED
        }
    }

    var smsCount = 0
    var smsError = ""
    var smsSource = "none"

    // 1. Primary path: Native ContentResolver (supports multi-line bodies, works with Shizuku/ADB pm grant + Root + manual grant)
    if (smsGranted) {
        runCatching {
            val uri = android.provider.Telephony.Sms.Inbox.CONTENT_URI
            val proj = arrayOf(
                android.provider.Telephony.Sms.ADDRESS,
                android.provider.Telephony.Sms.BODY,
                android.provider.Telephony.Sms.DATE
            )
            base.contentResolver.query(uri, proj, null, null, "${android.provider.Telephony.Sms.DATE} DESC")?.use { cursor ->
                val idxAddr = cursor.getColumnIndex(android.provider.Telephony.Sms.ADDRESS)
                val idxBody = cursor.getColumnIndex(android.provider.Telephony.Sms.BODY)
                val idxDate = cursor.getColumnIndex(android.provider.Telephony.Sms.DATE)
                var scanned = 0
                while (cursor.moveToNext() && scanned < 40) {
                    scanned++
                    val addr = if (idxAddr >= 0) cursor.getString(idxAddr).orEmpty().trim() else ""
                    val body = if (idxBody >= 0) cursor.getString(idxBody).orEmpty().trim() else ""
                    val dateVal = if (idxDate >= 0) cursor.getLong(idxDate) else 0L
                    if (body.isBlank()) continue
                    if (filterQ.isBlank() || "$addr $body".lowercase().contains(filterQ.lowercase())) {
                        merged += JSONObject()
                            .put("channel", "sms")
                            .put("sender", addr)
                            .put("title", addr)
                            .put("text", body)
                            .put("timestamp_ms", dateVal)
                        smsCount++
                    }
                }
                smsSource = "content_resolver"
            }
        }.onFailure { err ->
            smsError = err.message ?: err.javaClass.simpleName
        }
    }

    // 2. Fallback path: Privileged shell `content query` with multi-line Row chunk parsing (fixes newline/comma truncation)
    if (smsCount == 0 && privReady) {
        runCatching {
            val smsRes = privilege!!.execute(
                JSONObject()
                    .put("op", "shell")
                    .put("command", "content query --uri content://sms/inbox --projection address:body:date --sort \"date DESC\" 2>&1 | head -n 120")
            )
            val smsOut = smsRes.optString("output", smsRes.optString("stdout", ""))
            if (smsOut.contains("Permission Denial", ignoreCase = true) || smsOut.contains("Error while accessing provider", ignoreCase = true)) {
                if (smsError.isBlank()) smsError = smsOut.lineSequence().firstOrNull { it.isNotBlank() }?.take(140) ?: "Shell 无权访问短信数据库"
            } else {
                val chunks = smsOut.split(Regex("""(?m)^Row:\s*\d+\s+""")).drop(1)
                for (chunk in chunks) {
                    val c = chunk.trim()
                    if (c.isEmpty()) continue
                    val dateVal = Regex("""(?:^|,\s*)date=(\d+)""").find(c)?.groupValues?.get(1)?.toLongOrNull() ?: 0L
                    val addr = Regex("""address=([^,]*),\s*body=""").find(c)?.groupValues?.get(1)?.trim().orEmpty()
                    val body = Regex("""body=([\s\S]*?),\s*date=\d+""").find(c)?.groupValues?.get(1)?.trim().orEmpty()
                    if (body.isBlank()) continue
                    if (filterQ.isBlank() || "$addr $body".lowercase().contains(filterQ.lowercase())) {
                        merged += JSONObject()
                            .put("channel", "sms")
                            .put("sender", addr)
                            .put("title", addr)
                            .put("text", body)
                            .put("timestamp_ms", dateVal)
                        smsCount++
                    }
                }
                if (smsCount > 0) smsSource = "shell_content_query"
            }
        }.onFailure { err ->
            if (smsError.isBlank()) smsError = err.message ?: err.javaClass.simpleName
        }
    }

    merged.sortByDescending { it.optLong("timestamp_ms", 0L) }
    val keywordRe = Regex("""(?:验证码|校验码|动态码|确认码|提取码|安全码|code|otp|verification|passcode|pin)[^A-Za-z0-9\n]{0,18}([0-9]{4,8}|[A-Za-z0-9]{4,8})""", RegexOption.IGNORE_CASE)
    val revRe = Regex("""\b([0-9]{4,8})\b[^.\n]{0,16}(?:为你的|是你的|验证码|校验码|动态码|code|otp|verification)""", RegexOption.IGNORE_CASE)
    val candidates = JSONArray()
    val topItems = JSONArray()
    var bestCode = ""
    for (item in merged.take(limit)) {
        topItems.put(item)
        val combined = listOf(item.optString("title"), item.optString("text")).filter { it.isNotBlank() }.joinToString(" ")
        val m1 = keywordRe.find(combined) ?: revRe.find(combined)
        val code = m1?.groupValues?.getOrNull(1)?.trim().orEmpty()
        if (code.isNotBlank() && code.any { it.isDigit() }) {
            if (bestCode.isEmpty()) bestCode = code
            candidates.put(
                JSONObject()
                    .put("code", code)
                    .put("channel", item.optString("channel"))
                    .put("source", item.optString("app").ifBlank { item.optString("sender").ifBlank { item.optString("title") } })
                    .put("timestamp_ms", item.optLong("timestamp_ms", 0L))
                    .put("snippet", combined.take(180))
            )
        }
    }

    val bothBlocked = !smsGranted && !notifRes.optBoolean("listener_connected", false) && topItems.length() == 0
    return JSONObject()
        .put("ok", !bothBlocked)
        .put("op", "read_otp")
        .put("found", bestCode.isNotBlank())
        .put("code", bestCode)
        .put("sms_permission_granted", smsGranted)
        .put("sms_source", smsSource)
        .put("sms_count", smsCount)
        .put("notification_count", notifArr.length())
        .put("listener_connected", notifRes.optBoolean("listener_connected", false))
        .put("candidates", candidates)
        .put("items", topItems)
        .apply {
            if (smsError.isNotBlank()) put("sms_error", smsError)
            if (bothBlocked) {
                put("error", "未获得短信权限(READ_SMS)且通知监听未连接：请授权短信权限或连接 Shizuku/Root 后重试")
            }
        }
}

internal fun RootPhoneHarness.queryLauncherApps(base: Context, forceRefresh: Boolean = false): List<PhoneApp> {
    val now = SystemClock.elapsedRealtime()
    if (!forceRefresh && cachedLauncherApps.isNotEmpty() && now - appsCachedAtElapsed < APPS_CACHE_TTL_MS) {
        return cachedLauncherApps
    }
    val launcher = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
    val baseList = base.packageManager.queryIntentActivities(launcher, 0)
        .filter { it.activityInfo.packageName != base.packageName }
        .map { PhoneApp(it.loadLabel(base.packageManager).toString(), it.activityInfo.packageName + "/" + it.activityInfo.name) }
        .toMutableList()
    val knownPkgs = baseList.map { it.component.substringBefore("/") }.toMutableSet()
    val privReady = privilege?.isPrivilegedReady() == true
    if (privReady) {
        runCatching {
            val pmRes = privilege!!.execute(JSONObject().put("op", "shell").put("command", "cmd package list packages -3 2>/dev/null"))
            val thirdPartyPkgs = pmRes.optString("output", "").lineSequence()
                .map { it.trim().removePrefix("package:") }
                .filter { it.isNotBlank() && it != base.packageName && it !in knownPkgs }
                .take(80)
                .toList()
            if (thirdPartyPkgs.isNotEmpty()) {
                val joined = thirdPartyPkgs.joinToString(" ") { "'" + it.replace("'", "'\\''") + "'" }
                val resCmd = "for p in $joined; do r=\$(cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER \"\$p\" 2>/dev/null | tail -n 1); case \"\$r\" in */*) echo \"\$p|\$r\" ;; esac; done"
                val resolveRes = privilege!!.execute(JSONObject().put("op", "shell").put("command", resCmd))
                val pm = base.packageManager
                for (line in resolveRes.optString("output", "").lineSequence()) {
                    val s = line.trim()
                    val sep = s.indexOf('|')
                    if (sep <= 0) continue
                    val pkg = s.substring(0, sep).trim()
                    val comp = s.substring(sep + 1).trim()
                    if (!comp.contains("/") || pkg in knownPkgs) continue
                    val label = runCatching {
                        val ai = pm.getApplicationInfo(pkg, 0)
                        pm.getApplicationLabel(ai).toString().ifBlank { pkg }
                    }.getOrDefault(pkg)
                    baseList += PhoneApp(label, comp)
                    knownPkgs.add(pkg)
                }
            }
        }
    }
    val fresh = baseList
        .distinctBy { it.component }
        .sortedBy { it.name }
    cachedLauncherApps = fresh
    appsCachedAtElapsed = now
    return fresh
}

internal fun RootPhoneHarness.resolveLaunchComponent(base: Context, query: String): String {
    if (query.isBlank()) throw Exception("请提供要打开的应用名或组件名")
    val qTrim = query.trim()
    resolvedComponentCache[qTrim]?.let { return it }
    if (qTrim.contains("/") && qTrim.matches(Regex("[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+"))) {
        resolvedComponentCache[qTrim] = qTrim
        return qTrim
    }
    val apps = state.value.apps.ifEmpty { queryLauncherApps(base).also { list -> state.update { it.copy(apps = list) } } }
    val qLower = qTrim.lowercase()
    val stripped = qLower
        .replace(Regex("^(oppo|vivo|小米|miui|hyperos|华为|huawei|荣耀|honor|一加|oneplus|realme|真我|红米|redmi|三星|samsung|系统|手机|自带)\\s*"), "")
        .replace(Regex("(app|客户端|应用)$"), "")
        .trim()
        .ifBlank { qLower }

    val aliasPackages = mapOf(
        "微信" to "com.tencent.mm", "wechat" to "com.tencent.mm",
        "b站" to "tv.danmaku.bili", "bilibili" to "tv.danmaku.bili", "哔哩哔哩" to "tv.danmaku.bili",
        "qq" to "com.tencent.mobileqq",
        "网易云" to "com.netease.cloudmusic", "网易云音乐" to "com.netease.cloudmusic",
        "qq音乐" to "com.tencent.qqmusic",
        "健康" to "com.heytap.health", "运动健康" to "com.heytap.health",
        "淘宝" to "com.taobao.taobao", "支付宝" to "com.eg.android.AlipayGphone",
        "高德" to "com.autonavi.minimap", "高德地图" to "com.autonavi.minimap",
        "美团" to "com.sankuai.meituan", "小红书" to "com.xingin.xhs",
        "抖音" to "com.ss.android.ugc.aweme", "知乎" to "com.zhihu.android"
    )
    val aliasPkg = aliasPackages[qLower] ?: aliasPackages[stripped]
    if (aliasPkg != null) {
        apps.firstOrNull { it.component.substringBefore("/").equals(aliasPkg, ignoreCase = true) }?.let {
            resolvedComponentCache[qTrim] = it.component
            return it.component
        }
    }

    apps.firstOrNull {
        val pkg = it.component.substringBefore("/").lowercase()
        val nm = it.name.lowercase()
        pkg == qLower || nm == qLower || nm == stripped
    }?.let {
        resolvedComponentCache[qTrim] = it.component
        return it.component
    }
    apps.firstOrNull {
        val nm = it.name.lowercase()
        val comp = it.component.lowercase()
        nm.contains(qLower) || nm.contains(stripped) || comp.contains(qLower)
    }?.let {
        resolvedComponentCache[qTrim] = it.component
        return it.component
    }
    base.packageManager.getLaunchIntentForPackage(qTrim)?.component?.let {
        val comp = it.packageName + "/" + it.className
        resolvedComponentCache[qTrim] = comp
        return comp
    }
    val sampleApps = apps.take(25).joinToString("、") { it.name }
    throw Exception("未在手机上找到应用「$qTrim」；已安装的部分应用有：$sampleApps（也可调用 op=\"apps\" 搜索）")
}
