package dev.bubuki.proactive

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.LruCache
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Request
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap

/**
 * [Module] PulseMediaCache
 * [Responsibility]
 * Byte-sized in-memory LRU cache (24 MiB) backed by a 40 MiB SHA-256 disk cache
 * under `cacheDir/pulse-media` using [SecureTransport.HTTP] and in-flight
 * request deduplication.
 */
internal object PulseMediaCache {
    private const val MEM_CACHE_BYTES = 24 * 1024 * 1024
    private const val DISK_CACHE_BYTES = 40L * 1024L * 1024L
    private const val MAX_RESPONSE_BYTES = 12 * 1024 * 1024

    private data class CachedEntry(val image: ImageBitmap, val bytes: Int)

    private val memLock = Any()
    private val memCache = object : LruCache<String, CachedEntry>(MEM_CACHE_BYTES) {
        override fun sizeOf(key: String, value: CachedEntry): Int = value.bytes.coerceAtLeast(1)
    }
    private val inFlight = ConcurrentHashMap<String, CompletableDeferred<ImageBitmap?>>()
    private val diskTrimLock = Any()
    private val fetchScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    fun getMemoryCached(fullUrl: String): ImageBitmap? {
        if (fullUrl.isBlank()) return null
        return synchronized(memLock) { memCache.get(fullUrl)?.image }
    }

    suspend fun load(
        context: Context,
        fullUrl: String,
        token: String = "",
        maxDimPx: Int = 1600
    ): ImageBitmap? {
        if (fullUrl.isBlank()) return null
        getMemoryCached(fullUrl)?.let { return it }

        val existing = inFlight[fullUrl]
        if (existing != null) {
            return existing.await()
        }

        val deferred = CompletableDeferred<ImageBitmap?>()
        val prev = inFlight.putIfAbsent(fullUrl, deferred)
        if (prev != null) {
            return prev.await()
        }

        val appCtx = context.applicationContext
        fetchScope.launch {
            var decoded: ImageBitmap? = null
            try {
                decoded = loadFromDisk(appCtx, fullUrl, maxDimPx)
                    ?: fetchAndStore(appCtx, fullUrl, token, maxDimPx)
            } catch (_: Throwable) {
                decoded = null
            } finally {
                deferred.complete(decoded)
                inFlight.remove(fullUrl, deferred)
            }
        }
        return deferred.await()
    }

    private suspend fun loadFromDisk(context: Context, fullUrl: String, maxDimPx: Int): ImageBitmap? =
        withContext(Dispatchers.IO) {
            val file = diskFileFor(context, fullUrl)
            if (!file.exists() || !file.isFile || file.length() <= 0L) return@withContext null
            val raw = runCatching { file.readBytes() }.getOrNull() ?: return@withContext null
            val bmp = decodeSampled(raw, maxDimPx)
            if (bmp != null) {
                file.setLastModified(System.currentTimeMillis())
                val img = bmp.asImageBitmap()
                putMemory(fullUrl, img, bmp.allocationByteCount)
                img
            } else {
                file.delete()
                null
            }
        }

    private fun fetchAndStore(
        context: Context,
        fullUrl: String,
        token: String,
        maxDimPx: Int
    ): ImageBitmap? {
        val reqBuilder = Request.Builder().url(fullUrl)
        if (token.isNotBlank()) {
            reqBuilder.header("Authorization", "Bearer $token")
        }
        SecureTransport.HTTP.newCall(reqBuilder.build()).execute().use { resp ->
            if (!resp.isSuccessful) return null
            val body = resp.body ?: return null
            val cl = body.contentLength()
            if (cl > MAX_RESPONSE_BYTES) return null
            val rawBytes = body.byteStream().use { input ->
                val bos = ByteArrayOutputStream()
                val buf = ByteArray(16 * 1024)
                var total = 0
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    total += n
                    if (total > MAX_RESPONSE_BYTES) return null
                    bos.write(buf, 0, n)
                }
                bos.toByteArray()
            }
            if (rawBytes.isEmpty()) return null
            val bmp = decodeSampled(rawBytes, maxDimPx) ?: return null
            val img = bmp.asImageBitmap()
            putMemory(fullUrl, img, bmp.allocationByteCount)
            writeDisk(context, fullUrl, rawBytes)
            return img
        }
    }

    private fun decodeSampled(rawBytes: ByteArray, maxDimPx: Int): Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(rawBytes, 0, rawBytes.size, bounds)
        val maxDim = maxOf(bounds.outWidth, bounds.outHeight)
        if (maxDim <= 0) return null
        var sample = 1
        while (maxDim / sample > maxDimPx) sample *= 2
        val decodeOpts = BitmapFactory.Options().apply { inSampleSize = sample }
        return BitmapFactory.decodeByteArray(rawBytes, 0, rawBytes.size, decodeOpts)
    }

    private fun putMemory(key: String, image: ImageBitmap, bytes: Int) {
        synchronized(memLock) {
            memCache.put(key, CachedEntry(image, bytes.coerceAtLeast(1)))
        }
    }

    private fun diskDir(context: Context): File = File(context.cacheDir, "pulse-media")

    private fun diskFileFor(context: Context, fullUrl: String): File {
        val digest = MessageDigest.getInstance("SHA-256").digest(fullUrl.toByteArray(Charsets.UTF_8))
        val name = digest.joinToString("") { "%02x".format(it.toInt() and 0xFF) }
        return File(diskDir(context), name)
    }

    private fun writeDisk(context: Context, fullUrl: String, bytes: ByteArray) {
        if (bytes.size > MAX_RESPONSE_BYTES) return
        synchronized(diskTrimLock) {
            runCatching {
                val dir = diskDir(context)
                if (!dir.exists()) dir.mkdirs()
                val target = diskFileFor(context, fullUrl)
                val tmp = File(dir, target.name + ".tmp")
                tmp.writeBytes(bytes)
                if (target.exists()) target.delete()
                tmp.renameTo(target)
                trimDiskCacheLocked(dir)
            }
        }
    }

    private fun trimDiskCacheLocked(dir: File) {
        val files = dir.listFiles()?.filter { it.isFile && !it.name.endsWith(".tmp") } ?: return
        var total = files.sumOf { it.length() }
        if (total <= DISK_CACHE_BYTES) return
        val sorted = files.sortedBy { it.lastModified() }
        for (f in sorted) {
            if (total <= DISK_CACHE_BYTES) break
            val len = f.length()
            if (f.delete()) total -= len
        }
    }
}
