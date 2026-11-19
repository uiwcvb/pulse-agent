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
        val allCaps = runCatching {
            cm?.allNetworks?.mapNotNull { cm.getNetworkCapabilities(it) }.orEmpty()
        }.getOrDefault(emptyList())
        val hasWifi = activeCaps?.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI) == true ||
            allCaps.any { it.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI) && it.hasCapability(android.net.NetworkCapabilities.NET_CAPABILITY_INTERNET) }
        val hasCellular = activeCaps?.hasTransport(android.net.NetworkCapabilities.TRANSPORT_CELLULAR) == true ||
            allCaps.any { it.hasTransport(android.net.NetworkCapabilities.TRANSPORT_CELLULAR) && it.hasCapability(android.net.NetworkCapabilities.NET_CAPABILITY_INTERNET) }
        val hasVpn = activeCaps?.hasTransport(android.net.NetworkCapabilities.TRANSPORT_VPN) == true ||
            allCaps.any { it.hasTransport(android.net.NetworkCapabilities.TRANSPORT_VPN) }
        val netType = when {
            activeCaps == null && allCaps.isEmpty() -> "none"
            hasWifi -> "wifi"
            hasCellular -> "cellular"
            else -> "other"
        }
        out.put("network", JSONObject()
            .put("connected", activeCaps != null || allCaps.isNotEmpty())
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

private fun sandboxShellCommandToDisplay(rawCmd: String, displayId: Int): String {
    if (displayId <= 0) return rawCmd
    var s = rawCmd
    if (Regex("""\binput\b""").containsMatchIn(s) && !Regex("""\binput\b[^;|&\n]*?\s-d\s+\d+""").containsMatchIn(s)) {
        s = Regex("""\binput\s+""").replace(s, "input -d $displayId ")
    }
    if (Regex("""\bam\s+start\b""").containsMatchIn(s) && !Regex("""\bam\s+start\b[^;|&\n]*?\s--display\s+\d+""").containsMatchIn(s)) {
        s = Regex("""\bam\s+start\s+""").replace(s, "am start --display $displayId ")
    }
    if (Regex("""\bcmd\s+activity\s+start\b""").containsMatchIn(s) && !Regex("""\bcmd\s+activity\s+start\b[^;|&\n]*?\s--display\s+\d+""").containsMatchIn(s)) {
        s = Regex("""\bcmd\s+activity\s+start\s+""").replace(s, "cmd activity start --display $displayId ")
    }
    if (Regex("""\bscreencap\b""").containsMatchIn(s) && !Regex("""\bscreencap\b[^;|&\n]*?\s-d\s+\d+""").containsMatchIn(s)) {
        s = Regex("""\bscreencap\s+""").replace(s, "screencap -d $displayId ")
    }
    return s
}

internal fun RootPhoneHarness.executeShellAction(base: Context, input: JSONObject): JSONObject {
    var cmdStr = input.optString("command", input.optString("cmd", input.optString("text", ""))).trim()
    if (cmdStr.isEmpty()) throw Exception("请提供要执行的 shell command")
    val waitingForeground = state.value.phase == "waiting_user"
    if (Regex("""\bcmd\s+uimode\b|\bservice\s+call\s+uimode\b|\bsettings\s+put\s+(?:system|secure|global)\s+(?:ui_night_mode|screen_brightness|screen_off_timeout|font_scale|user_rotation|accelerometer_rotation|default_input_method|enabled_input_methods)\b""", RegexOption.IGNORE_CASE).containsMatchIn(cmdStr)) {
        throw Exception("安全沙箱已拦截全局系统设置修改指令（严禁修改主屏深浅色 Night mode、亮度、字体缩放或输入法等系统全局设置）")
    }
    if (!waitingForeground && Regex("""\bmonkey\b""", RegexOption.IGNORE_CASE).containsMatchIn(cmdStr)) {
        throw Exception("安全沙箱已拦截 monkey 指令（monkey 会直接操作主屏幕 Display 0），请改用 phone_workspace(op=\"open\") 或 am start")
    }
    if (!waitingForeground && Regex("""(?:--display|-d)\s+0\b""", RegexOption.IGNORE_CASE).containsMatchIn(cmdStr)) {
        throw Exception("安全沙箱已拦截指向主屏幕 (Display 0) 的指令，严禁在未接管状态下操作或启动应用到 Display 0")
    }
    var dispObj = runCatching { JSONObject(state.value.display) }.getOrNull()
    var currentDisp = if (dispObj?.optBoolean("created", false) == true) dispObj.optInt("displayId", -1) else -1
    val touchesDisplayUi = Regex(
        """\b(input|screencap|uiautomator)\b|\bam\s+(start|display)\b|\bcmd\s+activity\s+start|\bwm\s+(size|density|overscan|user-rotation)\b""",
        RegexOption.IGNORE_CASE
    ).containsMatchIn(cmdStr)
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
        if (Regex("""\bwm\s+(size|density|overscan|user-rotation)\b""", RegexOption.IGNORE_CASE).containsMatchIn(cmdStr) &&
            !Regex("""-d\s+$currentDisp\b""").containsMatchIn(cmdStr)
        ) {
            throw Exception("安全沙箱已拦截未绑定虚拟屏 (-d $currentDisp) 的 wm 显示参数修改指令，禁止修改主屏幕分辨率/DPI")
        }
        cmdStr = sandboxShellCommandToDisplay(cmdStr, currentDisp)
    }
    val hasPriv = privilege!!.isPrivilegedReady() || runCatching {
        val p = privilege!!.probe()
        p.optBoolean("privileged", p.optBoolean("root"))
    }.getOrDefault(false)
    if (!hasPriv) {
        throw Exception("尚未取得 Root 或无线 ADB 特权授权，无法执行特权 shell")
    }
    val safeCwd = "'" + shellCwd.replace("'", "'\\''") + "'"
    val wrappedCmd = "cd $safeCwd 2>/dev/null || cd /sdcard; $cmdStr; __pulse_ec=\$?; echo ''; echo '__PULSE_CWD__:'\$(pwd); exit \$__pulse_ec"
    val rawRes = try {
        privilege!!.execute(JSONObject().put("op", "shell").put("command", wrappedCmd))
    } catch (rpcErr: Throwable) {
        if (privilege?.getPrivilegeMode() == "wireless_adb") throw rpcErr
        val proc = ProcessBuilder("su", "-c", wrappedCmd).redirectErrorStream(true).start()
        val finished = proc.waitFor(20, java.util.concurrent.TimeUnit.SECONDS)
        if (!finished) proc.destroyForcibly()
        val outTxt = runCatching { proc.inputStream.bufferedReader().readText() }.getOrDefault("")
        val ec = if (finished) proc.exitValue() else 124
        JSONObject()
            .put("uid", 0)
            .put("ok", finished && ec == 0)
            .put("exit_code", ec)
            .put("timeout", !finished)
            .put("output", outTxt)
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

internal fun RootPhoneHarness.executeNotificationsAction(base: Context, input: JSONObject): JSONObject {
    val filterQ = input.optString("app", input.optString("query", input.optString("package", ""))).trim()
    val limit = input.optInt("limit", 25).coerceIn(1, 80)
    fun hasPrivilegedShell(): Boolean = privilege!!.isPrivilegedReady() || runCatching {
        val p = privilege!!.probe()
        p.optBoolean("privileged", p.optBoolean("root"))
    }.getOrDefault(false)
    if (!AgentNotificationListener.isConnected()) {
        runCatching {
            if (hasPrivilegedShell()) {
                privilege!!.execute(
                    JSONObject()
                        .put("op", "shell")
                        .put("command", "cmd notification allow_listener ${base.packageName}/${base.packageName}.AgentNotificationListener")
                )
                Thread.sleep(180)
            }
        }
    }
    var items = AgentNotificationListener.getRecentNotifications(base, filterQ, limit)
    if (items.length() == 0) {
        runCatching {
            if (hasPrivilegedShell()) {
                val dumpRes = privilege!!.execute(
                    JSONObject()
                        .put("op", "shell")
                        .put("command", "dumpsys notification --noredact | grep -E 'NotificationRecord\\(|pkg=|android\\.title=|android\\.text=|android\\.bigText=|postTime=' | head -n 300")
                )
                val rawOut = dumpRes.optString("output", dumpRes.optString("stdout", ""))
                val parsed = JSONArray()
                var curPkg = ""
                var curTitle = ""
                var curText = ""
                var curTs = 0L
                fun flushRecord() {
                    if (curPkg.isNotBlank() && curPkg != base.packageName && curPkg != "android" && curPkg != "com.android.systemui" && (curTitle.isNotBlank() || curText.isNotBlank())) {
                        val appLabel = AgentNotificationListener.resolveAppName(base, curPkg)
                        val hay = "$appLabel $curPkg $curTitle $curText".lowercase()
                        if (filterQ.isBlank() || hay.contains(filterQ.lowercase())) {
                            parsed.put(
                                JSONObject()
                                    .put("package", curPkg)
                                    .put("app", appLabel)
                                    .put("title", curTitle)
                                    .put("text", curText)
                                    .put("timestamp_ms", curTs)
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
                    if (s.contains("NotificationRecord(")) {
                        flushRecord()
                        val m = Regex("pkg=([^\\s:]+)").find(s)
                        curPkg = m?.groupValues?.get(1) ?: ""
                    } else if (s.startsWith("postTime=") || s.contains("postTime=")) {
                        val pm = Regex("postTime=(\\d+)").find(s)
                        curTs = pm?.groupValues?.get(1)?.toLongOrNull() ?: curTs
                    } else if (s.startsWith("android.title=")) {
                        curTitle = s.substringAfter("android.title=").removePrefix("String (").removeSuffix(")").trim()
                    } else if (s.startsWith("android.bigText=") && s != "android.bigText=null") {
                        val bt = s.substringAfter("android.bigText=").removePrefix("String (").removeSuffix(")").trim()
                        if (bt.isNotBlank() && bt != "null") curText = bt
                    } else if (s.startsWith("android.text=") && curText.isBlank()) {
                        val tx = s.substringAfter("android.text=").removePrefix("String (").removeSuffix(")").trim()
                        if (tx != "null") curText = tx
                    }
                    if (parsed.length() >= limit) break
                }
                flushRecord()
                if (parsed.length() > 0) items = parsed
            }
        }
    }
    return JSONObject()
        .put("ok", true)
        .put("listener_connected", AgentNotificationListener.isConnected())
        .put("count", items.length())
        .put("notifications", items)
}

internal fun RootPhoneHarness.executeReadOtpAction(base: Context, input: JSONObject): JSONObject {
    val filterQ = input.optString("query", input.optString("app", "")).trim()
    val limit = input.optInt("limit", 15).coerceIn(1, 40)
    val merged = mutableListOf<JSONObject>()
    val notifRes = executeNotificationsAction(base, JSONObject().put("query", filterQ).put("limit", 30))
    val notifArr = notifRes.optJSONArray("notifications") ?: JSONArray()
    for (i in 0 until notifArr.length()) {
        val it = notifArr.optJSONObject(i) ?: continue
        merged += JSONObject(it.toString()).put("channel", "notification")
    }
    val privReady = privilege?.isPrivilegedReady() == true || runCatching {
        val p = privilege?.probe()
        p?.optBoolean("privileged", p.optBoolean("root")) == true
    }.getOrDefault(false)
    if (privReady) {
        runCatching {
            val smsRes = privilege!!.execute(
                JSONObject()
                    .put("op", "shell")
                    .put("command", "content query --uri content://sms/inbox --projection address:body:date --sort \"date DESC\" 2>/dev/null | head -n 30")
            )
            val smsOut = smsRes.optString("output", "")
            for (line in smsOut.lineSequence()) {
                val trimmed = line.trim()
                if (!trimmed.startsWith("Row:")) continue
                val dateVal = Regex(",\\s*date=(\\d+)").find(trimmed)?.groupValues?.get(1)?.toLongOrNull() ?: 0L
                val addr = Regex("address=([^,]*),\\s*body=").find(trimmed)?.groupValues?.get(1)?.trim().orEmpty()
                val body = Regex(",\\s*body=(.*),\\s*date=\\d+").find(trimmed)?.groupValues?.get(1)?.trim().orEmpty()
                if (body.isBlank()) continue
                if (filterQ.isBlank() || "$addr $body".lowercase().contains(filterQ.lowercase())) {
                    merged += JSONObject()
                        .put("channel", "sms")
                        .put("sender", addr)
                        .put("title", addr)
                        .put("text", body)
                        .put("timestamp_ms", dateVal)
                }
            }
        }
    }
    merged.sortByDescending { it.optLong("timestamp_ms", 0L) }
    val keywordRe = Regex("""(?:验证码|校验码|动态码|确认码|提取码|code|otp|verification|passcode|pin)[^A-Za-z0-9\n]{0,18}([0-9]{4,8}|[A-Za-z0-9]{4,8})""", RegexOption.IGNORE_CASE)
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
    return JSONObject()
        .put("ok", true)
        .put("op", "read_otp")
        .put("found", bestCode.isNotBlank())
        .put("code", bestCode)
        .put("candidates", candidates)
        .put("items", topItems)
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
