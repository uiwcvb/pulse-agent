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


@Composable
internal fun TelegramActionChip(icon: String, label: String, color: Color, onClick: () -> Unit) {
    val haptics = LocalHapticFeedback.current
    Row(
        Modifier.iosPressScale(0.93f) {
            haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
            onClick()
        },
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp)
    ) {
        PulseIcon(icon, color, Modifier.size(13.5.dp))
        Text(label, color = color, fontSize = 12.sp, fontWeight = FontWeight.Medium)
    }
}

@Composable
internal fun PulseStickerThumb(model: NativePulseModel, stickerPath: String, size: Dp = 56.dp) {
    var bmp by remember(stickerPath) { mutableStateOf<ImageBitmap?>(null) }
    LaunchedEffect(stickerPath) {
        model.loadStickerBitmap(stickerPath) { loaded -> bmp = loaded }
    }
    Box(Modifier.size(size), contentAlignment = Alignment.Center) {
        bmp?.let { image ->
            Image(
                bitmap = image,
                contentDescription = "贴纸预览",
                modifier = Modifier.fillMaxSize().clip(RoundedCornerShape(8.dp)),
                contentScale = ContentScale.Fit
            )
        }
    }
}

@Composable
internal fun PulseStickerImage(model: NativePulseModel, stickerPath: String) {
    val c = LocalPulsePalette.current
    var bmp by remember(stickerPath) { mutableStateOf<ImageBitmap?>(null) }
    var showLightbox by remember { mutableStateOf(false) }
    val isSticker = stickerPath.contains("/stickers/")
    LaunchedEffect(stickerPath) {
        model.loadStickerBitmap(stickerPath) { loaded -> bmp = loaded }
    }
    val popScale by animateFloatAsState(
        targetValue = if (bmp != null) 1f else 0.82f,
        animationSpec = spring(dampingRatio = 0.68f, stiffness = 420f),
        label = "sticker_pop"
    )
    bmp?.let { image ->
        if (isSticker) {
            Image(
                bitmap = image,
                contentDescription = "表情贴纸",
                modifier = Modifier
                    .size(136.dp)
                    .graphicsLayer {
                        scaleX = popScale
                        scaleY = popScale
                    }
                    .clip(RoundedCornerShape(14.dp)),
                contentScale = ContentScale.Fit
            )
        } else {
            Image(
                bitmap = image,
                contentDescription = "AI返回图片",
                modifier = Modifier
                    .widthIn(min = 120.dp, max = 248.dp)
                    .heightIn(min = 80.dp, max = 268.dp)
                    .graphicsLayer {
                        scaleX = popScale
                        scaleY = popScale
                    }
                    .clip(RoundedCornerShape(16.dp))
                    .border(0.6.dp, c.line.copy(alpha = 0.6f), RoundedCornerShape(16.dp))
                    .clickable { showLightbox = true },
                contentScale = ContentScale.Fit
            )
            if (showLightbox) {
                PulseMediaLightboxDialog(model = model, rawPath = stickerPath, bitmap = image, onDismiss = { showLightbox = false })
            }
        }
    }
}

@Composable
internal fun PulseMediaLightboxDialog(
    model: NativePulseModel,
    rawPath: String,
    bitmap: ImageBitmap,
    onDismiss: () -> Unit
) {
    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        var lbShown by remember { mutableStateOf(false) }
        LaunchedEffect(Unit) { lbShown = true }
        val reduceLb = LocalReduceMotion.current
        val lbAlpha by animateFloatAsState(if (lbShown || reduceLb) 1f else 0f, tween(180), label = "lb_alpha")
        val lbScale by animateFloatAsState(
            if (lbShown || reduceLb) 1f else 0.92f,
            spring(dampingRatio = 0.80f, stiffness = 360f),
            label = "lb_scale"
        )
        Box(
            Modifier.fillMaxSize()
                .background(Color.Black.copy(alpha = 0.92f * lbAlpha))
                .clickable { onDismiss() },
            contentAlignment = Alignment.Center
        ) {
            Image(
                bitmap = bitmap,
                contentDescription = "全屏大图预览",
                modifier = Modifier.fillMaxWidth(0.95f)
                    .fillMaxHeight(0.84f)
                    .graphicsLayer {
                        alpha = lbAlpha
                        scaleX = lbScale
                        scaleY = lbScale
                    },
                contentScale = ContentScale.Fit
            )
            Row(
                Modifier.align(Alignment.BottomCenter).padding(bottom = 32.dp),
                horizontalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Box(
                    Modifier.clip(RoundedCornerShape(20.dp))
                        .background(Color.White.copy(alpha = 0.18f))
                        .iosPressScale(0.95f) {
                            val rawUrl = resolveWorkspaceMediaUrl(rawPath, model.ui.serverUrl)
                            val token = model.currentDeviceToken()
                            val full = if (rawUrl.startsWith(model.ui.serverUrl.trimEnd('/')) && token.isNotBlank() && !rawUrl.contains("token=")) {
                                val sep = if (rawUrl.contains('?')) '&' else '?'
                                "$rawUrl${sep}token=${android.net.Uri.encode(token)}"
                            } else rawUrl
                            onDismiss()
                            if (full.isNotBlank()) model.openLink(full)
                        }
                        .padding(horizontal = 16.dp, vertical = 9.dp)
                ) {
                    Text("在浏览器打开", color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.Medium)
                }
                Box(
                    Modifier.clip(RoundedCornerShape(20.dp))
                        .background(Color.White.copy(alpha = 0.25f))
                        .clickable { onDismiss() }
                        .padding(horizontal = 18.dp, vertical = 9.dp)
                ) {
                    Text("关闭", color = Color.White, fontSize = 13.sp, fontWeight = FontWeight.SemiBold)
                }
            }
        }
    }
}

@Composable
internal fun PulseUserMediaGrid(model: NativePulseModel, paths: List<String>, alignEnd: Boolean = true) {
    val distinct = remember(paths) { paths.filter { it.isNotBlank() }.distinct() }
    if (distinct.isEmpty()) return
    val c = LocalPulsePalette.current
    if (distinct.size == 1) {
        val single = distinct.first()
        var bmp by remember(single) { mutableStateOf<ImageBitmap?>(null) }
        var showLb by remember { mutableStateOf(false) }
        val reduceMotion = LocalReduceMotion.current
        val imgAlpha by animateFloatAsState(
            targetValue = if (bmp != null) 1f else 0f,
            animationSpec = if (reduceMotion) snap() else tween(160),
            label = "single_img_fade"
        )
        LaunchedEffect(single) {
            model.loadStickerBitmap(single) { loaded -> bmp = loaded }
        }
        Box(
            Modifier
                .widthIn(min = 128.dp, max = 232.dp)
                .heightIn(min = 96.dp, max = 248.dp)
                .clip(RoundedCornerShape(16.dp))
                .background(c.surface)
                .border(0.7.dp, c.line.copy(alpha = 0.55f), RoundedCornerShape(16.dp))
                .iosPressScale(0.97f, enabled = bmp != null) { showLb = true },
            contentAlignment = Alignment.Center
        ) {
            if (bmp != null) {
                Image(
                    bitmap = bmp!!,
                    contentDescription = single.substringAfterLast('/'),
                    modifier = Modifier.clip(RoundedCornerShape(16.dp)).graphicsLayer { alpha = imgAlpha },
                    contentScale = ContentScale.Fit
                )
            } else {
                Row(
                    Modifier.padding(horizontal = 14.dp, vertical = 18.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp, color = c.accent)
                    Text("图片加载中", color = c.muted, fontSize = 12.sp)
                }
            }
        }
        if (showLb && bmp != null) {
            PulseMediaLightboxDialog(model = model, rawPath = single, bitmap = bmp!!, onDismiss = { showLb = false })
        }
        return
    }
    Column(
        verticalArrangement = Arrangement.spacedBy(6.dp),
        horizontalAlignment = if (alignEnd) Alignment.End else Alignment.Start
    ) {
        distinct.chunked(2).forEach { rowPaths ->
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                rowPaths.forEach { imgPath ->
                    var bmp by remember(imgPath) { mutableStateOf<ImageBitmap?>(null) }
                    var showLb by remember { mutableStateOf(false) }
                    val reduceMotion = LocalReduceMotion.current
                    val gridAlpha by animateFloatAsState(
                        targetValue = if (bmp != null) 1f else 0f,
                        animationSpec = if (reduceMotion) snap() else tween(160),
                        label = "grid_img_fade"
                    )
                    LaunchedEffect(imgPath) {
                        model.loadStickerBitmap(imgPath) { loaded -> bmp = loaded }
                    }
                    Box(
                        Modifier
                            .size(114.dp)
                            .clip(RoundedCornerShape(14.dp))
                            .background(c.surface)
                            .border(0.7.dp, c.line.copy(alpha = 0.55f), RoundedCornerShape(14.dp))
                            .iosPressScale(0.96f, enabled = bmp != null) { showLb = true },
                        contentAlignment = Alignment.Center
                    ) {
                        if (bmp != null) {
                            Image(
                                bitmap = bmp!!,
                                contentDescription = imgPath.substringAfterLast('/'),
                                modifier = Modifier.fillMaxSize().graphicsLayer { alpha = gridAlpha },
                                contentScale = ContentScale.Crop
                            )
                        } else {
                            CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp, color = c.accent)
                        }
                    }
                    if (showLb && bmp != null) {
                        PulseMediaLightboxDialog(model = model, rawPath = imgPath, bitmap = bmp!!, onDismiss = { showLb = false })
                    }
                }
            }
        }
    }
}

private fun formatFileSizeLabel(bytes: Long): String {
    if (bytes <= 0L) return ""
    return when {
        bytes < 1024L -> "${bytes} B"
        bytes < 1024L * 1024L -> String.format(java.util.Locale.US, "%.1f KB", bytes / 1024.0)
        else -> String.format(java.util.Locale.US, "%.1f MB", bytes / (1024.0 * 1024.0))
    }
}

@Composable
internal fun PulseFileAttachmentCard(
    model: NativePulseModel,
    path: String,
    displayName: String = "",
    fileSize: Long = 0L
) {
    val c = LocalPulsePalette.current
    val s = model.ui
    val fileName = displayName.ifBlank { path.substringAfterLast('/') }.ifBlank { "未命名文件" }
    val ext = fileName.substringAfterLast('.', "").uppercase(java.util.Locale.ROOT).take(5)
    var downloading by remember(path) { mutableStateOf(false) }

    val badgeBg = remember(ext, c.accent) {
        when (ext) {
            "PDF" -> Color(0xFFEF4444)
            "DOC", "DOCX", "RTF" -> Color(0xFF2563EB)
            "XLS", "XLSX", "CSV" -> Color(0xFF10B981)
            "PPT", "PPTX" -> Color(0xFFF97316)
            "ZIP", "RAR", "7Z", "TAR", "GZ" -> Color(0xFFF59E0B)
            "MD", "TXT", "JSON", "PY", "KT", "JS", "TS" -> Color(0xFF6366F1)
            else -> c.accent
        }
    }
    val typeLabel = when (ext) {
        "PDF" -> "PDF 文档"
        "DOC", "DOCX" -> "Word 文档"
        "XLS", "XLSX", "CSV" -> "表格文件"
        "PPT", "PPTX" -> "演示文稿"
        "ZIP", "RAR", "7Z", "TAR", "GZ" -> "压缩包"
        else -> if (ext.isNotBlank()) "$ext 文件" else "工作区文件"
    }
    val sizeStr = formatFileSizeLabel(fileSize)
    val subText = if (downloading) {
        "正在下载并准备打开…"
    } else if (sizeStr.isNotEmpty()) {
        "$typeLabel · $sizeStr · 点击打开"
    } else {
        "$typeLabel · 点击打开或保存"
    }

    Row(
        Modifier
            .widthIn(min = 210.dp, max = 278.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(if (s.dark) c.surface else Color.White)
            .border(0.8.dp, if (s.dark) Color(0x28FFFFFF) else Color(0x1E000000), RoundedCornerShape(14.dp))
            .iosPressScale(0.97f, enabled = !downloading) {
                model.downloadAndOpenWorkspaceFile(path, fileName) { busy -> downloading = busy }
            }
            .padding(horizontal = 11.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        Box(
            Modifier
                .size(42.dp)
                .clip(RoundedCornerShape(11.dp))
                .background(badgeBg.copy(alpha = 0.14f))
                .border(0.8.dp, badgeBg.copy(alpha = 0.28f), RoundedCornerShape(11.dp)),
            contentAlignment = Alignment.Center
        ) {
            if (downloading) {
                CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp, color = badgeBg)
            } else if (ext.isNotBlank()) {
                Text(
                    ext,
                    color = badgeBg,
                    fontSize = if (ext.length >= 4) 10.sp else 11.5.sp,
                    fontWeight = FontWeight.ExtraBold,
                    maxLines = 1
                )
            } else {
                PulseIcon("file", badgeBg, Modifier.size(20.dp))
            }
        }
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(
                fileName,
                color = c.ink,
                fontSize = 13.5.sp,
                fontWeight = FontWeight.SemiBold,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis
            )
            Text(
                subText,
                color = if (downloading) c.accent else c.muted,
                fontSize = 11.sp,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )
        }
        Box(
            Modifier
                .size(26.dp)
                .clip(CircleShape)
                .background(c.line.copy(alpha = 0.25f)),
            contentAlignment = Alignment.Center
        ) {
            PulseIcon("next", c.muted, Modifier.size(13.dp))
        }
    }
}
