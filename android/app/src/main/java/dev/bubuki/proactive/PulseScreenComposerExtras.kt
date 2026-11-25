package dev.bubuki.proactive

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.util.Locale
import kotlin.math.roundToInt

/**
 * [Module] PulseScreenComposerExtras
 * [Responsibility]
 * Extracted composer attachment picker tiles, upload progress strip, and file badge formatting
 * keeping [PulseScreenComposer] modular and focused.
 */

private fun formatCompactBytes(bytes: Long): String {
    if (bytes <= 0L) return ""
    return when {
        bytes < 1024L -> "${bytes} B"
        bytes < 1024L * 1024L -> "${(bytes / 1024.0).roundToInt()} KB"
        else -> String.format(Locale.US, "%.1f MB", bytes / (1024.0 * 1024.0))
    }
}

private fun extBadgeStyle(name: String): Pair<String, Color> {
    val ext = name.substringAfterLast('.', "").uppercase(Locale.ROOT).take(4)
    val label = ext.ifBlank { "FILE" }
    val color = when (ext) {
        "PDF" -> Color(0xFFFF453A)
        "ZIP", "RAR", "7Z", "GZ", "TAR" -> Color(0xFFFF9F0A)
        "PY", "KT", "JS", "TS", "MJS", "JAVA", "JSON", "YML", "YAML", "MD" -> Color(0xFF5E5CE6)
        "XLS", "XLSX", "CSV" -> Color(0xFF30D158)
        "DOC", "DOCX", "TXT", "RTF" -> Color(0xFF0A84FF)
        else -> Color(0xFF8E8E93)
    }
    return label to color
}

@Composable
internal fun ModernAttachSourceButton(
    icon: String,
    title: String,
    subtitle: String,
    tint: Color,
    modifier: Modifier = Modifier,
    onClick: () -> Unit
) {
    val c = LocalPulsePalette.current
    Row(
        modifier
            .clip(RoundedCornerShape(14.dp))
            .background(c.fill.copy(alpha = 0.88f))
            .border(0.6.dp, c.line.copy(alpha = 0.65f), RoundedCornerShape(14.dp))
            .iosPressScale(0.96f, onClick = onClick)
            .padding(horizontal = 11.dp, vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(9.dp)
    ) {
        Box(
            Modifier.size(32.dp)
                .clip(RoundedCornerShape(9.dp))
                .background(tint.copy(alpha = 0.14f)),
            contentAlignment = Alignment.Center
        ) {
            PulseIcon(icon, tint, Modifier.size(17.dp))
        }
        Column(Modifier.weight(1f)) {
            Text(title, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, color = c.ink, maxLines = 1)
            Text(subtitle, fontSize = 10.5.sp, color = c.muted, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
    }
}

@Composable
internal fun ModernAttachmentStrip(
    attachments: List<PulseAttachment>,
    pendingUploads: List<PulsePendingUpload>,
    attachmentThumbs: Map<String, ImageBitmap>,
    onPreviewThumb: (ImageBitmap) -> Unit,
    onRemoveAttachment: (PulseAttachment) -> Unit,
    onRetryPending: (String) -> Unit,
    onDismissPending: (String) -> Unit,
    onClearAll: () -> Unit
) {
    val c = LocalPulsePalette.current
    val totalCount = attachments.size + pendingUploads.size
    Column(Modifier.fillMaxWidth().padding(bottom = 8.dp)) {
        LazyRow(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            items(pendingUploads, key = { "p_${it.id}" }) { item ->
                if (item.isImage) {
                    AttachmentImageTile(
                        name = item.name,
                        thumb = item.thumb,
                        uploading = item.error.isEmpty(),
                        progress = item.progress,
                        error = item.error,
                        onClick = {
                            if (item.error.isNotEmpty()) onRetryPending(item.id)
                            else item.thumb?.let(onPreviewThumb)
                        },
                        onRemove = { onDismissPending(item.id) }
                    )
                } else {
                    AttachmentFileTile(
                        name = item.name,
                        bytes = item.bytes,
                        uploading = item.error.isEmpty(),
                        progress = item.progress,
                        error = item.error,
                        onClick = { if (item.error.isNotEmpty()) onRetryPending(item.id) },
                        onRemove = { onDismissPending(item.id) }
                    )
                }
            }
            items(attachments, key = { "a_${it.source}_${it.path}" }) { att ->
                val isImg = CloudFiles.isImageFilename(att.name) || attachmentThumbs.containsKey(att.path)
                val thumb = attachmentThumbs[att.path]
                if (isImg) {
                    AttachmentImageTile(
                        name = att.name,
                        thumb = thumb,
                        uploading = false,
                        progress = 1f,
                        error = "",
                        onClick = { thumb?.let(onPreviewThumb) },
                        onRemove = { onRemoveAttachment(att) }
                    )
                } else {
                    AttachmentFileTile(
                        name = att.name,
                        bytes = att.bytes,
                        uploading = false,
                        progress = 1f,
                        error = "",
                        onClick = {},
                        onRemove = { onRemoveAttachment(att) }
                    )
                }
            }
            if (totalCount >= 2 && pendingUploads.none { it.error.isEmpty() }) {
                item(key = "clear_all_chip") {
                    Column(
                        Modifier.height(64.dp)
                            .clip(RoundedCornerShape(14.dp))
                            .background(c.fill.copy(alpha = 0.70f))
                            .border(0.6.dp, c.line.copy(alpha = 0.6f), RoundedCornerShape(14.dp))
                            .iosPressScale(0.95f, onClick = onClearAll)
                            .padding(horizontal = 12.dp),
                        verticalArrangement = Arrangement.Center,
                        horizontalAlignment = Alignment.CenterHorizontally
                    ) {
                        PulseIcon("broom", c.muted, Modifier.size(16.dp))
                        Text("全清", Modifier.padding(top = 3.dp), fontSize = 11.sp, color = c.muted, fontWeight = FontWeight.Medium)
                    }
                }
            }
        }
    }
}

@Composable
internal fun AttachmentImageTile(
    name: String,
    thumb: ImageBitmap?,
    uploading: Boolean,
    progress: Float,
    error: String,
    onClick: () -> Unit,
    onRemove: () -> Unit
) {
    val c = LocalPulsePalette.current
    val reduceMotion = rememberReducedMotion()
    val smoothProgress by animateFloatAsState(
        targetValue = progress.coerceIn(0f, 1f),
        animationSpec = if (reduceMotion) snap() else tween(160),
        label = "img_upload_progress"
    )
    val borderColor = if (error.isNotEmpty()) c.warn.copy(alpha = 0.85f) else c.line.copy(alpha = 0.75f)
    Box(
        Modifier.size(64.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(c.fill)
            .border(0.8.dp, borderColor, RoundedCornerShape(14.dp))
            .clickable(onClick = onClick)
    ) {
        if (thumb != null) {
            Image(
                bitmap = thumb,
                contentDescription = name,
                modifier = Modifier.fillMaxSize(),
                contentScale = ContentScale.Crop
            )
        } else {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                PulseIcon("image", c.accent, Modifier.size(22.dp))
            }
        }
        if (uploading) {
            Box(
                Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.45f)),
                contentAlignment = Alignment.Center
            ) {
                CircularProgressIndicator(
                    progress = { smoothProgress },
                    modifier = Modifier.size(26.dp),
                    color = Color.White,
                    trackColor = Color.White.copy(alpha = 0.25f),
                    strokeWidth = 2.4.dp
                )
                Text(
                    "${(smoothProgress * 100).roundToInt()}%",
                    color = Color.White,
                    fontSize = 9.sp,
                    fontWeight = FontWeight.Bold,
                    style = TextStyle(fontFeatureSettings = "tnum")
                )
            }
        } else if (error.isNotEmpty()) {
            Box(
                Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.56f)),
                contentAlignment = Alignment.Center
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    PulseIcon("refresh", Color.White, Modifier.size(16.dp))
                    Text("重试", color = Color.White, fontSize = 9.5.sp, fontWeight = FontWeight.SemiBold)
                }
            }
        }
        Box(
            Modifier.align(Alignment.TopEnd)
                .size(36.dp)
                .iosPressScale(0.90f, onClick = onRemove)
                .padding(3.dp),
            contentAlignment = Alignment.TopEnd
        ) {
            Box(
                Modifier.size(18.dp)
                    .clip(CircleShape)
                    .background(Color.Black.copy(alpha = 0.62f)),
                contentAlignment = Alignment.Center
            ) {
                PulseIcon("close", Color.White, Modifier.size(10.dp))
            }
        }
    }
}

@Composable
internal fun AttachmentFileTile(
    name: String,
    bytes: Long,
    uploading: Boolean,
    progress: Float,
    error: String,
    onClick: () -> Unit,
    onRemove: () -> Unit
) {
    val c = LocalPulsePalette.current
    val reduceMotion = rememberReducedMotion()
    val (extLabel, extColor) = remember(name) { extBadgeStyle(name) }
    val smoothProgress by animateFloatAsState(
        targetValue = progress.coerceIn(0f, 1f),
        animationSpec = if (reduceMotion) snap() else tween(160),
        label = "file_upload_progress"
    )
    val sizeTxt = remember(bytes) { formatCompactBytes(bytes) }
    val subTxt = when {
        error.isNotEmpty() -> "上传失败 · 点击重试"
        uploading -> if (sizeTxt.isNotEmpty()) "${(smoothProgress * 100).roundToInt()}% · $sizeTxt" else "正在上传 ${(smoothProgress * 100).roundToInt()}%"
        sizeTxt.isNotEmpty() -> "$sizeTxt · 已就绪"
        else -> "已就绪"
    }
    Box(
        Modifier.widthIn(min = 152.dp, max = 204.dp)
            .height(64.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(if (error.isNotEmpty()) c.warn.copy(alpha = 0.08f) else c.fill.copy(alpha = 0.92f))
            .border(0.8.dp, if (error.isNotEmpty()) c.warn.copy(alpha = 0.65f) else c.line.copy(alpha = 0.72f), RoundedCornerShape(14.dp))
            .clickable(onClick = onClick)
    ) {
        Row(
            Modifier.fillMaxSize().padding(start = 9.dp, end = 24.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(9.dp)
        ) {
            Box(
                Modifier.size(36.dp)
                    .clip(RoundedCornerShape(10.dp))
                    .background(extColor.copy(alpha = 0.16f)),
                contentAlignment = Alignment.Center
            ) {
                if (error.isNotEmpty()) {
                    PulseIcon("refresh", c.warn, Modifier.size(16.dp))
                } else {
                    Text(extLabel, color = extColor, fontSize = 10.sp, fontWeight = FontWeight.Bold, maxLines = 1)
                }
            }
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.Center) {
                Text(name, color = c.ink, fontSize = 12.5.sp, fontWeight = FontWeight.Medium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(
                    subTxt,
                    color = if (error.isNotEmpty()) c.warn else c.muted,
                    fontSize = 10.5.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    style = TextStyle(fontFeatureSettings = "tnum")
                )
            }
        }
        if (uploading) {
            Box(
                Modifier.align(Alignment.BottomStart)
                    .fillMaxWidth()
                    .height(2.5.dp)
                    .background(c.line.copy(alpha = 0.35f))
            ) {
                Box(
                    Modifier.fillMaxHeight()
                        .fillMaxWidth(smoothProgress)
                        .background(c.accent)
                )
            }
        }
        Box(
            Modifier.align(Alignment.TopEnd)
                .size(36.dp)
                .iosPressScale(0.90f, onClick = onRemove)
                .padding(4.dp),
            contentAlignment = Alignment.TopEnd
        ) {
            Box(
                Modifier.size(18.dp)
                    .clip(CircleShape)
                    .background(c.ink.copy(alpha = 0.12f)),
                contentAlignment = Alignment.Center
            ) {
                PulseIcon("close", c.muted, Modifier.size(10.dp))
            }
        }
    }
}
