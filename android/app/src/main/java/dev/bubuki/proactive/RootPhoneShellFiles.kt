package dev.bubuki.proactive

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * [Module] RootPhoneShellFiles
 * [Responsibility] Phone storage inspection & cloud workspace file transfer helpers (`list_files`, `pull_file`, `push_file`)
 *                  supporting both uid 0 (Root) and uid 2000 (Wireless ADB) privilege modes.
 */
internal fun RootPhoneHarness.resolveLatestPhoneFile(): java.io.File? {
    val dirs = listOf(
        java.io.File("/sdcard/Download/DeepSeek Claw"),
        java.io.File("/sdcard/Download/WeiXin"),
        java.io.File("/sdcard/Download"),
        java.io.File("/sdcard/Pictures/Screenshots"),
        java.io.File("/sdcard/DCIM/Screenshots"),
        java.io.File("/sdcard/Android/data/com.tencent.mm/MicroMsg/Download")
    )
    val candidates = mutableListOf<java.io.File>()
    for (d in dirs) {
        if (d.exists() && d.isDirectory) {
            d.listFiles()?.filter { it.isFile && it.length() > 0 && !it.name.startsWith(".") }?.let { candidates.addAll(it) }
        }
    }
    return candidates.maxByOrNull { it.lastModified() }
}

internal fun RootPhoneHarness.executeListFiles(input: JSONObject): JSONObject {
    val dirPath = input.optString("path", "/sdcard/Download").trim().ifBlank { "/sdcard/Download" }
    val limit = input.optInt("limit", 25).coerceIn(1, 100)
    val dir = java.io.File(dirPath)
    if (dir.exists() && !dir.isDirectory) {
        return JSONObject().put("ok", false).put("op", "list_files").put("path", dirPath).put("error", "目标路径不是目录: $dirPath")
    }
    val arr = JSONArray()
    val files = if (dir.exists() && dir.isDirectory) dir.listFiles()?.sortedByDescending { it.lastModified() } else null
    if (files != null) {
        for (f in files.take(limit)) {
            arr.put(
                JSONObject()
                    .put("name", f.name)
                    .put("path", f.absolutePath)
                    .put("is_dir", f.isDirectory)
                    .put("bytes", if (f.isFile) f.length() else 0L)
                    .put("mtime", f.lastModified() / 1000)
            )
        }
        return JSONObject().put("ok", true).put("op", "list_files").put("path", dirPath).put("files", arr)
    }
    if (privilege!!.isPrivilegedReady() || runCatching { val p = privilege!!.probe(); p.optBoolean("privileged", p.optBoolean("root")) }.getOrDefault(false)) {
        val safeP = "'" + dirPath.replace("'", "'\\''") + "'"
        val checkRes = privilege!!.execute(JSONObject().put("op", "shell").put("command", "[ -d $safeP ] && echo DIR_OK || echo DIR_MISSING"))
        if (!checkRes.optString("output", "").contains("DIR_OK")) {
            return JSONObject().put("ok", false).put("op", "list_files").put("path", dirPath).put("error", "目录不存在: $dirPath")
        }
        val lsRes = privilege!!.execute(JSONObject().put("op", "shell").put("command", "ls -lt $safeP | head -n ${limit + 1}"))
        return JSONObject().put("ok", true).put("op", "list_files").put("path", dirPath).put("ls_output", lsRes.optString("output", ""))
    }
    return JSONObject().put("ok", false).put("op", "list_files").put("path", dirPath).put("error", "目录不存在或无法访问: $dirPath")
}

internal fun RootPhoneHarness.executePullFile(base: Context, input: JSONObject): JSONObject {
    val rawPath = input.optString("path", input.optString("target", "latest")).trim()
    val source = input.optString("source", "workspace").trim().lowercase().let { if (it == "computer") "computer" else "workspace" }
    val isLatestAlias = rawPath.isBlank() || rawPath.equals("latest", ignoreCase = true) || rawPath.equals("latest_download", ignoreCase = true)
    var resolvedPhonePath = rawPath
    val targetFile = if (isLatestAlias) {
        resolveLatestPhoneFile()?.also { resolvedPhonePath = it.absolutePath }
    } else {
        java.io.File(rawPath)
    }
    var stagedCopy: java.io.File? = null
    try {
        val uploadFile = if (targetFile != null && targetFile.exists() && targetFile.canRead() && targetFile.length() > 0) {
            targetFile
        } else if (privilege!!.isPrivilegedReady() || runCatching { val p = privilege!!.probe(); p.optBoolean("privileged", p.optBoolean("root")) }.getOrDefault(false)) {
            if (isLatestAlias && (targetFile == null || !targetFile.exists())) {
                val findCmd = "ls -1dt /sdcard/Download/* '/sdcard/Download/DeepSeek Claw/'* /sdcard/Pictures/Screenshots/* /sdcard/DCIM/Screenshots/* 2>/dev/null | while IFS= read -r f; do [ -f \"\$f\" ] && [ -s \"\$f\" ] && echo \"\$f\" && break; done"
                val findRes = privilege!!.execute(JSONObject().put("op", "shell").put("command", findCmd))
                val foundLine = findRes.optString("output", "").lineSequence().map { it.trim() }.firstOrNull { it.startsWith("/") }
                if (!foundLine.isNullOrBlank()) {
                    resolvedPhonePath = foundLine
                }
            }
            if (resolvedPhonePath.isBlank() || resolvedPhonePath.equals("latest", ignoreCase = true) || resolvedPhonePath.equals("latest_download", ignoreCase = true)) {
                throw Exception("手机端 /sdcard/Download 与截图目录中未找到可拉取的最新文件")
            }
            val baseName = resolvedPhonePath.substringAfterLast('/').ifBlank { "pulled_file" }.replace(Regex("[^A-Za-z0-9._-]"), "_")
            val stageDir = if (privilege?.getGrantedUid() == 0) base.cacheDir else (base.externalCacheDir ?: base.cacheDir)
            if (!stageDir.exists()) stageDir.mkdirs()
            val tmp = java.io.File.createTempFile("pull_", "_$baseName", stageDir)
            if (tmp.exists()) tmp.delete()
            stagedCopy = tmp
            val safeSrc = "'" + resolvedPhonePath.replace("'", "'\\''") + "'"
            val safeDst = "'" + tmp.absolutePath.replace("'", "'\\''") + "'"
            val cpRes = privilege!!.execute(JSONObject().put("op", "shell").put("command", "cp $safeSrc $safeDst && chmod 644 $safeDst"))
            if (!cpRes.optBoolean("ok", true) || cpRes.optInt("exit_code", 0) != 0) {
                throw Exception("拉取文件失败 (exit=${cpRes.optInt("exit_code", -1)}): ${cpRes.optString("output", "").take(200)}")
            }
            if (!tmp.exists() || tmp.length() == 0L) throw Exception("手机端未找到或无法读取文件: $resolvedPhonePath")
            tmp
        } else {
            throw Exception("手机端未找到可读取文件: ${resolvedPhonePath.ifBlank { "/sdcard/Download" }}")
        }
        val receipt = CloudFiles.upload(base, android.net.Uri.fromFile(uploadFile), source)
        return JSONObject()
            .put("ok", true)
            .put("op", "pull_file")
            .put("phone_path", targetFile?.absolutePath ?: resolvedPhonePath)
            .put("workspace_path", receipt.optString("path"))
            .put("name", receipt.optString("name", uploadFile.name))
            .put("bytes", receipt.optLong("bytes", uploadFile.length()))
            .put("sha256", receipt.optString("sha256"))
    } finally {
        stagedCopy?.delete()
    }
}

internal fun RootPhoneHarness.executePushFile(base: Context, input: JSONObject): JSONObject {
    val rawPath = input.optString("path", input.optString("target", "")).trim()
    if (rawPath.isEmpty()) throw Exception("push_file 需要提供 path")
    val source = input.optString("source", "workspace").trim().lowercase().let { if (it == "computer") "computer" else "workspace" }
    val receipt = CloudFiles.download(base, source, rawPath)
    val name = receipt.optString("name")
    val phonePath = receipt.optString("path").ifBlank { "/sdcard/Download/DeepSeek Claw/$name" }
    return JSONObject()
        .put("ok", true)
        .put("op", "push_file")
        .put("name", name)
        .put("bytes", receipt.optLong("bytes"))
        .put("uri", receipt.optString("uri"))
        .put("phone_path", phonePath)
}
