package dev.bubuki.proactive

import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.net.URI
import java.util.concurrent.TimeUnit

/**
 * [Module] PulseApkUpdater
 * [Responsibility]
 * 客户端极速零 SHA-256 应用内 OTA 更新器：主备节点探测与规范化路径解析、
 * `Accept-Encoding: identity` 带进度流式下载、大小+ZIP头+包名三重校验、
 * 2 秒快速 Root 判定，以及 Android 未知来源授权跳转与 `onResume` 自动续装闭环。
 */

private val APK_UPDATE_HTTP: OkHttpClient by lazy {
    SecureTransport.HTTP.newBuilder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()
}

private fun buildUpdateGetRequest(url: String): Request {
    val busted = if (url.contains("?")) "$url&t=${System.currentTimeMillis()}" else "$url?t=${System.currentTimeMillis()}"
    return Request.Builder().url(busted).header("Accept-Encoding", "identity").header("Cache-Control", "no-cache").get().build()
}

private fun normalizeApkDownloadUrl(base: String, rawDownloadUrl: String): String {
    val cleanBase = base.trim().trimEnd('/')
    val raw = rawDownloadUrl.trim()
    if (raw.startsWith("http://") || raw.startsWith("https://")) return raw
    val rel = (if (raw.isBlank()) "/download/proactive-agent.apk" else raw).let { if (it.startsWith("/")) it else "/$it" }
    val uri = runCatching { URI(cleanBase) }.getOrNull()
    val basePath = (uri?.rawPath ?: "").trimEnd('/')
    return if (uri != null && !uri.rawAuthority.isNullOrBlank() && basePath.isNotEmpty() &&
        (rel == basePath || rel.startsWith("$basePath/") || rel.startsWith("$basePath?"))
    ) "${uri.scheme}://${uri.rawAuthority}$rel" else "$cleanBase$rel"
}

private fun fetchReleaseJson(baseUrl: String): JSONObject {
    APK_UPDATE_HTTP.newCall(buildUpdateGetRequest("${baseUrl.trimEnd('/')}/download/release.json")).execute().use { resp ->
        if (!resp.isSuccessful) throw IllegalStateException("HTTP ${resp.code}")
        val body = resp.body?.string()?.trim().orEmpty()
        if (body.isEmpty() || !body.startsWith("{")) throw IllegalStateException("版本清单格式无效")
        return JSONObject(body)
    }
}

private fun probeReleaseCandidates(configuredBase: String, rememberedBase: String?): Triple<String, JSONObject, Boolean> {
    val primary = configuredBase.trim().ifBlank { SecureTransport.DEFAULT_SERVER }.trimEnd('/')
    val candidates = linkedSetOf<String>().apply {
        if (primary.isNotBlank()) add(primary)
        rememberedBase?.trim()?.trimEnd('/')?.takeIf { it.isNotBlank() }?.let { add(it) }
        SecureTransport.DEFAULT_SERVER.trim().trimEnd('/').takeIf { it.isNotBlank() }?.let { add(it) }
    }
    if (candidates.isEmpty()) throw IllegalStateException("请先在设置中填写服务器地址")
    var lastErr: Throwable? = null
    for (candidate in candidates) {
        try {
            return Triple(candidate, fetchReleaseJson(candidate), candidate != primary)
        } catch (t: Throwable) {
            lastErr = t
        }
    }
    throw (lastErr ?: IllegalStateException("主备节点均无法连接"))
}

internal fun NativePulseModel.checkForUpdates(silent: Boolean = false) {
    if (ui.checkingUpdate) return
    ui = ui.copy(checkingUpdate = true)
    scope.launch {
        try {
            val primaryBase = ui.serverUrl.trim().ifBlank { SecureTransport.DEFAULT_SERVER }.trimEnd('/')
            val (usedBase, releaseObj, usedFallback) = withContext(Dispatchers.IO) {
                probeReleaseCandidates(primaryBase, effectiveUpdateServerUrl)
            }
            val ver = releaseObj.optString("versionName").ifBlank { releaseObj.optString("version_name") }
                .ifBlank { releaseObj.optString("version") }.ifBlank { releaseObj.optString("tag_name") }
                .removePrefix("v").trim()
            val remoteCode = releaseObj.optInt("versionCode", releaseObj.optInt("version_code", releaseObj.optInt("build", 0)))
            val notes = releaseObj.optString("notes").ifBlank { releaseObj.optString("body") }
            val expSize = releaseObj.optLong("apk_size", 0L).coerceAtLeast(0L)
            effectiveUpdateServerUrl = usedBase
            resolvedApkDownloadUrl = normalizeApkDownloadUrl(usedBase, releaseObj.optString("download_url", ""))
            val curVer = BuildConfig.VERSION_NAME
            val curCode = BuildConfig.VERSION_CODE
            val hasNew = (remoteCode > curCode) || (ver.isNotBlank() && ver != curVer && (remoteCode <= 0 || remoteCode >= curCode))
            ui = ui.copy(
                latestReleaseVersion = ver.ifBlank { curVer },
                latestReleaseNotes = notes,
                updateAvailable = hasNew,
                updateChecked = true,
                expectedApkSize = expSize
            )
            if (usedFallback) {
                toast(if (hasNew) "主节点不可用，已切默认节点 · 发现新版本 v$ver (Build $remoteCode)" else "主节点不可用，已切默认节点 · 当前已是最新 v$curVer (Build $curCode)")
            } else if (!silent) {
                toast(if (hasNew) "发现新版本 v$ver (Build $remoteCode)，点击下方按钮即可高速下载安装" else "当前已是最新版本 v$curVer (Build $curCode)")
            }
        } catch (e: Exception) {
            if (!silent) toast("检查更新失败 (${e.message ?: "网络异常"})，请检查网络连接后重试")
        } finally {
            ui = ui.copy(checkingUpdate = false)
        }
    }
}

private fun verifyApkArchive(pm: PackageManager, expectedPkg: String, part: File, expectedSize: Long) {
    val actualSize = part.length()
    if (actualSize < 5_000_000L) throw IllegalStateException("安装包体积过小 (${actualSize} B)")
    if (expectedSize > 0L && actualSize != expectedSize) throw IllegalStateException("安装包大小校验不符 (${actualSize} / $expectedSize B)")
    val magic = ByteArray(4)
    RandomAccessFile(part, "r").use { raf ->
        if (raf.read(magic) < 4 || magic[0] != 0x50.toByte() || magic[1] != 0x4B.toByte() || magic[2] != 0x03.toByte() || magic[3] != 0x04.toByte()) {
            throw IllegalStateException("下载内容非有效 APK 压缩头，可能命中了错误页面")
        }
    }
    val pkgInfo = if (Build.VERSION.SDK_INT >= 33) pm.getPackageArchiveInfo(part.absolutePath, PackageManager.PackageInfoFlags.of(0))
    else { @Suppress("DEPRECATION") pm.getPackageArchiveInfo(part.absolutePath, 0) }
    if (pkgInfo == null || pkgInfo.packageName != expectedPkg) {
        throw IllegalStateException("安装包解析失败或包名不匹配 (${pkgInfo?.packageName ?: "null"})")
    }
}

private suspend fun streamApkToFile(url: String, part: File, expectedSize: Long, onProgress: suspend (Int) -> Unit) {
    part.delete()
    APK_UPDATE_HTTP.newCall(buildUpdateGetRequest(url)).execute().use { resp ->
        if (!resp.isSuccessful) throw IllegalStateException("HTTP ${resp.code}")
        val body = resp.body ?: throw IllegalStateException("服务端响应体为空")
        val total = body.contentLength().takeIf { it > 0L } ?: expectedSize
        body.byteStream().use { input ->
            part.outputStream().use { out ->
                val buf = ByteArray(64 * 1024)
                var downloaded = 0L
                var lastPct = -1
                while (true) {
                    val n = input.read(buf)
                    if (n <= 0) break
                    out.write(buf, 0, n)
                    downloaded += n
                    if (total > 0L) {
                        val pct = ((downloaded * 100L) / total).toInt().coerceIn(0, 99)
                        if (pct != lastPct) { lastPct = pct; onProgress(pct) }
                    }
                }
            }
        }
    }
}

private fun hasWorkingRootSu(): Boolean = runCatching {
    val p = ProcessBuilder("sh", "-c", "command -v su >/dev/null 2>&1 && su -c id").redirectErrorStream(true).start()
    if (!p.waitFor(2, TimeUnit.SECONDS)) { p.destroyForcibly(); return@runCatching false }
    p.exitValue() == 0 && p.inputStream.bufferedReader().readText().contains("uid=0")
}.getOrDefault(false)

private fun tryRootSilentInstall(apkFile: File): Boolean = runCatching {
    if (!hasWorkingRootSu()) return@runCatching false
    val tmp = "/data/local/tmp/proactive-agent-update.apk"
    val p = ProcessBuilder("su", "-c", "cp '${apkFile.absolutePath}' $tmp && chmod 644 $tmp && pm install -r -d $tmp && rm -f $tmp")
        .redirectErrorStream(true).start()
    val finished = p.waitFor(45, TimeUnit.SECONDS)
    if (!finished) { p.destroyForcibly(); return@runCatching false }
    p.exitValue() == 0 && p.inputStream.bufferedReader().readText().contains("Success", ignoreCase = true)
}.getOrDefault(false)

internal fun NativePulseModel.launchApkInstaller(apkFile: File) {
    val uri = FileProvider.getUriForFile(activity, "${activity.packageName}.avatar-files", apkFile)
    val installIntent = Intent(Intent.ACTION_VIEW).apply {
        setDataAndType(uri, "application/vnd.android.package-archive")
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
    }
    toast("安装包校验通过，正在拉起系统安装器…")
    activity.startActivity(installIntent)
}

internal fun NativePulseModel.resumePendingApkInstallIfReady() {
    val path = pendingApkInstallPath ?: return
    pendingApkInstallPath = null
    val file = File(path)
    if (!file.exists()) return
    if (activity.packageManager.canRequestPackageInstalls()) {
        runCatching { launchApkInstaller(file) }.onFailure { toast("拉起安装器失败：${it.message ?: "请重试"}") }
    } else {
        toast("未开启「安装未知应用」权限，开启后再次点击安装即可")
    }
}

internal fun NativePulseModel.downloadLatestApk() {
    dismissMenu()
    if (ui.downloadingApk) { toast("正在下载安装包 (${ui.downloadProgress}%)，请稍候…"); return }
    ui = ui.copy(downloadingApk = true, downloadProgress = 0)
    toast("正在应用内高速下载最新 APK…")
    scope.launch {
        try {
            val apkFile = withContext(Dispatchers.IO) {
                var expSize = ui.expectedApkSize
                var resolvedUrl = resolvedApkDownloadUrl
                if (expSize <= 0L || resolvedUrl.isNullOrBlank()) {
                    runCatching {
                        val (usedBase, relObj, _) = probeReleaseCandidates(ui.serverUrl, effectiveUpdateServerUrl)
                        effectiveUpdateServerUrl = usedBase
                        expSize = relObj.optLong("apk_size", 0L).coerceAtLeast(0L)
                        resolvedUrl = normalizeApkDownloadUrl(usedBase, relObj.optString("download_url", ""))
                        resolvedApkDownloadUrl = resolvedUrl
                        withContext(Dispatchers.Main) { ui = ui.copy(expectedApkSize = expSize, updateChecked = true) }
                    }
                }
                val activeBase = (effectiveUpdateServerUrl ?: ui.serverUrl).trim().ifBlank { SecureTransport.DEFAULT_SERVER }.trimEnd('/')
                val candidates = linkedSetOf<String>().apply {
                    resolvedUrl?.takeIf { it.isNotBlank() }?.let { add(it) }
                    if (activeBase.isNotBlank()) add("$activeBase/download/proactive-agent.apk")
                    val defSrv = SecureTransport.DEFAULT_SERVER.trim().trimEnd('/')
                    if (defSrv.isNotBlank()) add("$defSrv/download/proactive-agent.apk")
                }
                val dir = File(activity.cacheDir, "avatar-capture").apply { mkdirs() }
                val target = File(dir, "proactive-agent-update.apk")
                val part = File(dir, "proactive-agent-update.apk.part")
                var lastErr: Throwable? = null
                var ok = false
                for ((idx, baseUrl) in candidates.withIndex()) {
                    try {
                        streamApkToFile(baseUrl, part, expSize) { pct -> withContext(Dispatchers.Main) { ui = ui.copy(downloadProgress = pct) } }
                        verifyApkArchive(activity.packageManager, activity.packageName, part, if (idx == 0) expSize else 0L)
                        if (target.exists()) target.delete()
                        if (!part.renameTo(target)) { part.copyTo(target, overwrite = true); part.delete() }
                        ok = true; break
                    } catch (t: Throwable) {
                        part.delete(); lastErr = t
                    }
                }
                if (!ok) throw (lastErr ?: IllegalStateException("下载失败"))
                target
            }
            ui = ui.copy(downloadProgress = 100)
            if (withContext(Dispatchers.IO) { tryRootSilentInstall(apkFile) }) {
                toast("Root 静默覆盖安装成功！")
                return@launch
            }
            if (!activity.packageManager.canRequestPackageInstalls()) {
                pendingApkInstallPath = apkFile.absolutePath
                toast("请开启「允许安装未知应用」权限，授权返回后将自动继续安装")
                val permIntent = Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${activity.packageName}"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                runCatching { activity.startActivity(permIntent) }.onFailure { pendingApkInstallPath = null; launchApkInstaller(apkFile) }
                return@launch
            }
            launchApkInstaller(apkFile)
        } catch (e: Exception) {
            toast("下载更新失败：${e.message ?: "网络或安装包校验异常"}，请检查网络后重试")
        } finally {
            ui = ui.copy(downloadingApk = false, downloadProgress = 0)
        }
    }
}
