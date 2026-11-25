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
        Modifier.heightIn(min = 40.dp)
            .clip(RoundedCornerShape(8.dp))
            .iosPressScale(0.93f) {
                haptics.performHapticFeedback(HapticFeedbackType.TextHandleMove)
                onClick()
            }
            .padding(horizontal = 8.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp)
    ) {
        PulseIcon(icon, color, Modifier.size(13.5.dp))
        Text(label, color = color, fontSize = 12.sp, fontWeight = FontWeight.Medium)
    }
}

@Composable
internal fun PulseStickerThumb(model: NativePulseModel, stickerPath: String, size: Dp = 56.dp) {
    val c = LocalPulsePalette.current
    val fullUrl = remember(stickerPath, model.ui.serverUrl) { resolveWorkspaceMediaUrl(stickerPath, model.ui.serverUrl) }
    var bmp by remember(fullUrl) { mutableStateOf(PulseMediaCache.getMemoryCached(fullUrl)) }
    var failed by remember(fullUrl) { mutableStateOf(false) }
    var retryNonce by remember(fullUrl) { mutableIntStateOf(0) }
    LaunchedEffect(fullUrl, retryNonce) {
        if (bmp != null || fullUrl.isBlank()) return@LaunchedEffect
        failed = false
        val loaded = model.loadMediaBitmap(stickerPath)
        if (loaded != null) bmp = loaded else failed = true
    }
    Box(
        Modifier.size(size)
            .clip(RoundedCornerShape(8.dp))
            .then(if (failed && bmp == null) Modifier.clickable { retryNonce++ } else Modifier),
        contentAlignment = Alignment.Center
    ) {
        when {
            bmp != null -> Image(
                bitmap = bmp!!,
                contentDescription = "贴纸预览",
                modifier = Modifier.fillMaxSize().clip(RoundedCornerShape(8.dp)),
                contentScale = ContentScale.Fit
            )
            failed -> PulseIcon("refresh", c.muted, Modifier.size(18.dp))
            else -> Box(Modifier.fillMaxSize().background(c.fill.copy(alpha = 0.4f)))
        }
    }
}

@Composable
internal fun PulseStickerImage(model: NativePulseModel, stickerPath: String) {
    val c = LocalPulsePalette.current
    val fullUrl = remember(stickerPath, model.ui.serverUrl) { resolveWorkspaceMediaUrl(stickerPath, model.ui.serverUrl) }
    var bmp by remember(fullUrl) { mutableStateOf(PulseMediaCache.getMemoryCached(fullUrl)) }
    var failed by remember(fullUrl) { mutableStateOf(false) }
    var retryNonce by remember(fullUrl) { mutableIntStateOf(0) }
    var showLightbox by remember { mutableStateOf(false) }
    val isSticker = stickerPath.contains("/stickers/")
    val reduceMotion = rememberReducedMotion()
    LaunchedEffect(fullUrl, retryNonce) {
        if (bmp != null || fullUrl.isBlank()) return@LaunchedEffect
        failed = false
        val loaded = model.loadMediaBitmap(stickerPath)
        if (loaded != null) bmp = loaded else failed = true
    }
    val popScale by animateFloatAsState(
        targetValue = if (bmp != null || reduceMotion) 1f else 0.92f,
        animationSpec = if (reduceMotion) snap() else spring(dampingRatio = 0.78f, stiffness = 420f),
        label = "sticker_pop"
    )
    if (isSticker) {
        Box(
            Modifier.size(136.dp)
                .clip(RoundedCornerShape(14.dp))
                .then(if (failed && bmp == null) Modifier.background(c.surface).clickable { retryNonce++ } else Modifier),
            contentAlignment = Alignment.Center
        ) {
            when {
                bmp != null -> Image(
                    bitmap = bmp!!,
                    contentDescription = "表情贴纸",
                    modifier = Modifier
                        .fillMaxSize()
                        .graphicsLayer {
                            scaleX = popScale
                            scaleY = popScale
                        },
                    contentScale = ContentScale.Fit
                )
                failed -> Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    PulseIcon("refresh", c.muted, Modifier.size(22.dp))
                    Text("点击重试", color = c.muted, fontSize = 11.sp)
                }
                else -> CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp, color = c.accent)
            }
        }
    } else {
        Box(
            Modifier
                .widthIn(min = 120.dp, max = 248.dp)
                .heightIn(min = 112.dp, max = 268.dp)
                .clip(RoundedCornerShape(16.dp))
                .then(if (bmp == null) Modifier.size(width = 196.dp, height = 148.dp).background(c.surface) else Modifier)
                .border(0.6.dp, c.line.copy(alpha = 0.6f), RoundedCornerShape(16.dp))
                .clickable(enabled = bmp != null || failed) {
                    if (bmp != null) showLightbox = true else retryNonce++
                },
            contentAlignment = Alignment.Center
        ) {
            when {
                bmp != null -> Image(
                    bitmap = bmp!!,
                    contentDescription = "AI返回图片",
                    modifier = Modifier
                        .fillMaxWidth()
                        .graphicsLayer {
                            scaleX = popScale
                            scaleY = popScale
                        },
                    contentScale = ContentScale.Fit
                )
                failed -> Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    PulseIcon("refresh", c.muted, Modifier.size(22.dp))
                    Text("图片加载失败 · 点击重试", color = c.muted, fontSize = 11.5.sp)
                }
                else -> CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp, color = c.accent)
            }
        }
        if (showLightbox && bmp != null) {
            PulseMediaLightboxDialog(model = model, rawPath = stickerPath, bitmap = bmp!!, onDismiss = { showLightbox = false })
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
        properties = DialogProperties(usePlatformDefaultWidth = false, decorFitsSystemWindows = true)
    ) {
        var lbShown by remember { mutableStateOf(false) }
        var zoomScale by remember { mutableFloatStateOf(1f) }
        var panOffset by remember { mutableStateOf(androidx.compose.ui.geometry.Offset.Zero) }
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
                .background(Color.Black.copy(alpha = 0.94f * lbAlpha))
                .pointerInput(Unit) {
                    detectTapGestures(
                        onTap = { onDismiss() },
                        onDoubleTap = {
                            if (zoomScale > 1.05f) {
                                zoomScale = 1f
                                panOffset = androidx.compose.ui.geometry.Offset.Zero
                            } else {
                                zoomScale = 2.5f
                            }
                        }
                    )
                },
            contentAlignment = Alignment.Center
        ) {
            Image(
                bitmap = bitmap,
                contentDescription = "全屏大图预览",
                modifier = Modifier.fillMaxSize()
                    .pointerInput(Unit) {
                        detectTransformGestures { _, pan, zoom, _ ->
                            val nextScale = (zoomScale * zoom).coerceIn(1f, 4f)
                            zoomScale = nextScale
                            panOffset = if (nextScale > 1.02f) {
                                androidx.compose.ui.geometry.Offset(
                                    (panOffset.x + pan.x).coerceIn(-size.width * 0.8f, size.width * 0.8f),
                                    (panOffset.y + pan.y).coerceIn(-size.height * 0.8f, size.height * 0.8f)
                                )
                            } else {
                                androidx.compose.ui.geometry.Offset.Zero
                            }
                        }
                    }
                    .graphicsLayer {
                        alpha = lbAlpha
                        scaleX = lbScale * zoomScale
                        scaleY = lbScale * zoomScale
                        translationX = panOffset.x
                        translationY = panOffset.y
                    },
                contentScale = ContentScale.Fit
            )
            Box(
                Modifier.align(Alignment.TopEnd)
                    .statusBarsPadding()
                    .padding(top = 20.dp, end = 16.dp)
                    .size(42.dp)
                    .clip(CircleShape)
                    .background(Color.White.copy(alpha = 0.2f))
                    .iosPressScale(0.92f) { onDismiss() },
                contentAlignment = Alignment.Center
            ) {
                PulseIcon("close", Color.White, Modifier.size(18.dp))
            }
            Row(
                Modifier.align(Alignment.BottomCenter)
                    .navigationBarsPadding()
                    .padding(start = 16.dp, end = 16.dp, bottom = 44.dp)
                    .clip(RoundedCornerShape(26.dp))
                    .background(Color(0xCC1C1C1E))
                    .border(0.6.dp, Color.White.copy(alpha = 0.16f), RoundedCornerShape(26.dp))
                    .clickable(
                        interactionSource = remember { MutableInteractionSource() },
                        indication = null,
                        onClick = {}
                    )
                    .padding(horizontal = 8.dp, vertical = 6.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Box(
                    Modifier.heightIn(min = 42.dp)
                        .clip(RoundedCornerShape(20.dp))
                        .background(Color.White.copy(alpha = 0.14f))
                        .iosPressScale(0.95f) { model.saveBitmapToGallery(bitmap, rawPath) }
                        .padding(horizontal = 16.dp, vertical = 10.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Text("保存相册", color = Color.White, fontSize = 13.5.sp, fontWeight = FontWeight.Medium)
                }
                Box(
                    Modifier.heightIn(min = 42.dp)
                        .clip(RoundedCornerShape(20.dp))
                        .background(Color.White.copy(alpha = 0.14f))
                        .iosPressScale(0.95f) { model.shareBitmapImage(bitmap, rawPath) }
                        .padding(horizontal = 16.dp, vertical = 10.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Text("分享", color = Color.White, fontSize = 13.5.sp, fontWeight = FontWeight.Medium)
                }
                Box(
                    Modifier.heightIn(min = 42.dp)
                        .clip(RoundedCornerShape(20.dp))
                        .background(Color.White.copy(alpha = 0.24f))
                        .iosPressScale(0.95f) { onDismiss() }
                        .padding(horizontal = 18.dp, vertical = 10.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Text("关闭", color = Color.White, fontSize = 13.5.sp, fontWeight = FontWeight.SemiBold)
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
        val singleUrl = remember(single, model.ui.serverUrl) { resolveWorkspaceMediaUrl(single, model.ui.serverUrl) }
        var bmp by remember(singleUrl) { mutableStateOf(PulseMediaCache.getMemoryCached(singleUrl)) }
        var failed by remember(singleUrl) { mutableStateOf(false) }
        var retryNonce by remember(singleUrl) { mutableIntStateOf(0) }
        var showLb by remember { mutableStateOf(false) }
        val reduceMotion = LocalReduceMotion.current
        val imgAlpha by animateFloatAsState(
            targetValue = if (bmp != null) 1f else 0f,
            animationSpec = if (reduceMotion) snap() else tween(160),
            label = "single_img_fade"
        )
        LaunchedEffect(singleUrl, retryNonce) {
            if (bmp != null || singleUrl.isBlank()) return@LaunchedEffect
            failed = false
            val loaded = model.loadMediaBitmap(single)
            if (loaded != null) bmp = loaded else failed = true
        }
        Box(
            Modifier
                .widthIn(min = 128.dp, max = 232.dp)
                .heightIn(min = 112.dp, max = 248.dp)
                .then(if (bmp == null) Modifier.size(width = 196.dp, height = 148.dp) else Modifier)
                .clip(RoundedCornerShape(16.dp))
                .background(c.surface)
                .border(0.7.dp, c.line.copy(alpha = 0.55f), RoundedCornerShape(16.dp))
                .iosPressScale(0.97f, enabled = bmp != null || failed) {
                    if (bmp != null) showLb = true else retryNonce++
                },
            contentAlignment = Alignment.Center
        ) {
            if (bmp != null) {
                Image(
                    bitmap = bmp!!,
                    contentDescription = single.substringAfterLast('/'),
                    modifier = Modifier.clip(RoundedCornerShape(16.dp)).graphicsLayer { alpha = imgAlpha },
                    contentScale = ContentScale.Fit
                )
            } else if (failed) {
                Column(
                    Modifier.padding(horizontal = 14.dp, vertical = 18.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    PulseIcon("refresh", c.muted, Modifier.size(20.dp))
                    Text("图片加载失败 · 点击重试", color = c.muted, fontSize = 11.5.sp)
                }
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
    val rowChunks = remember(distinct) { distinct.chunked(2) }
    Column(
        verticalArrangement = Arrangement.spacedBy(6.dp),
        horizontalAlignment = if (alignEnd) Alignment.End else Alignment.Start
    ) {
        rowChunks.forEach { rowPaths ->
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                rowPaths.forEach { imgPath ->
                    key(imgPath) {
                        val gridUrl = remember(imgPath, model.ui.serverUrl) { resolveWorkspaceMediaUrl(imgPath, model.ui.serverUrl) }
                        var bmp by remember(gridUrl) { mutableStateOf(PulseMediaCache.getMemoryCached(gridUrl)) }
                        var failed by remember(gridUrl) { mutableStateOf(false) }
                        var retryNonce by remember(gridUrl) { mutableIntStateOf(0) }
                        var showLb by remember { mutableStateOf(false) }
                        val reduceMotion = LocalReduceMotion.current
                        val gridAlpha by animateFloatAsState(
                            targetValue = if (bmp != null) 1f else 0f,
                            animationSpec = if (reduceMotion) snap() else tween(160),
                            label = "grid_img_fade"
                        )
                        LaunchedEffect(gridUrl, retryNonce) {
                            if (bmp != null || gridUrl.isBlank()) return@LaunchedEffect
                            failed = false
                            val loaded = model.loadMediaBitmap(imgPath)
                            if (loaded != null) bmp = loaded else failed = true
                        }
                        Box(
                            Modifier
                                .size(114.dp)
                                .clip(RoundedCornerShape(14.dp))
                                .background(c.surface)
                                .border(0.7.dp, c.line.copy(alpha = 0.55f), RoundedCornerShape(14.dp))
                                .iosPressScale(0.96f, enabled = bmp != null || failed) {
                                    if (bmp != null) showLb = true else retryNonce++
                                },
                            contentAlignment = Alignment.Center
                        ) {
                            when {
                                bmp != null -> Image(
                                    bitmap = bmp!!,
                                    contentDescription = imgPath.substringAfterLast('/'),
                                    modifier = Modifier.fillMaxSize().graphicsLayer { alpha = gridAlpha },
                                    contentScale = ContentScale.Crop
                                )
                                failed -> Column(
                                    horizontalAlignment = Alignment.CenterHorizontally,
                                    verticalArrangement = Arrangement.spacedBy(4.dp)
                                ) {
                                    PulseIcon("refresh", c.muted, Modifier.size(18.dp))
                                    Text("点击重试", color = c.muted, fontSize = 10.5.sp)
                                }
                                else -> CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp, color = c.accent)
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
            .clip(RoundedCornerShape(16.dp))
            .background(c.surface)
            .border(0.5.dp, c.line.copy(alpha = 0.72f), RoundedCornerShape(16.dp))
            .iosPressScale(0.97f, enabled = !downloading) {
                model.downloadAndOpenWorkspaceFile(path, fileName) { busy -> downloading = busy }
            }
            .padding(8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        Box(
            Modifier
                .size(42.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(badgeBg.copy(alpha = 0.14f))
                .border(0.5.dp, badgeBg.copy(alpha = 0.28f), RoundedCornerShape(8.dp)),
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
                overflow = TextOverflow.Ellipsis,
                style = TextStyle(fontFeatureSettings = "tnum")
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
