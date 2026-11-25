package dev.bubuki.proactive

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Color as AndroidColor
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.text.InputType
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * [Module] FloatingVirtualDisplayChrome
 * [Responsibility] Pearl-white bezel tokens and overlaid translucent floating controls
 *                  ("挂起", "收起", "关闭", top drag grabber, docked edge pill) shared by both
 *                  the WindowManager overlay (`FloatingVirtualDisplay`) and the Compose in-app
 *                  PiP fallback (`InAppFloatingVirtualDisplay`), ensuring zero opaque black bars.
 */

internal const val PEARL_WHITE_FILL_INT: Int = 0xF7FAFAFC.toInt()
internal const val PEARL_EDGE_CONTOUR_IDLE_INT: Int = 0x2B000000
internal const val PEARL_INNER_HAIRLINE_INT: Int = 0x1A000000

internal fun pearlOuterStrokeColorInt(phase: String): Int = when (phase) {
    "running" -> 0xCC34C759.toInt()
    "waiting_user" -> 0xEEFF9F0A.toInt()
    else -> PEARL_EDGE_CONTOUR_IDLE_INT
}

internal fun pearlStatusDotColorInt(phase: String): Int = when (phase) {
    "running" -> 0xFF34C759.toInt()
    "waiting_user" -> 0xFFFF9F0A.toInt()
    else -> 0xFF007AFF.toInt()
}

@SuppressLint("ClickableViewAccessibility")
internal fun createPearlEdgeBubbleView(
    context: Context,
    state: RootPhoneState,
    previewBitmap: Bitmap?,
    dockLeft: Boolean,
    dp: (Float) -> Int,
    onRestore: () -> Unit,
    onClose: (() -> Unit)? = null
): FrameLayout {
    val root = FrameLayout(context).apply {
        clipChildren = false
        clipToPadding = false
    }
    val bubble = FrameLayout(context).apply {
        val radius = dp(24f).toFloat()
        background = GradientDrawable().apply {
            setColor(0xEBF2F5FA.toInt())
            cornerRadius = radius
            setStroke(dp(1.2f).coerceAtLeast(2), pearlOuterStrokeColorInt(state.phase))
        }
        elevation = dp(14f).toFloat()
        clipToOutline = true
        outlineProvider = object : android.view.ViewOutlineProvider() {
            override fun getOutline(view: View, outline: android.graphics.Outline) {
                outline.setOval(0, 0, view.width, view.height)
            }
        }
        contentDescription = "点击展开虚拟屏挂起球"
    }
    if (previewBitmap != null && !previewBitmap.isRecycled) {
        val thumb = ImageView(context).apply {
            scaleType = ImageView.ScaleType.CENTER_CROP
            alpha = 0.88f
            setImageBitmap(previewBitmap)
        }
        bubble.addView(thumb, FrameLayout.LayoutParams(-1, -1))
        val tint = View(context).apply {
            setBackgroundColor(0x240C1018)
        }
        bubble.addView(tint, FrameLayout.LayoutParams(-1, -1))
    }
    val statusDot = View(context).apply {
        background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(pearlStatusDotColorInt(state.phase))
            setStroke(dp(1.2f).coerceAtLeast(1), 0xE6FFFFFF.toInt())
        }
    }
    bubble.addView(
        statusDot,
        FrameLayout.LayoutParams(dp(10f), dp(10f), Gravity.TOP or (if (dockLeft) Gravity.END else Gravity.START)).apply {
            topMargin = dp(4f)
            if (dockLeft) rightMargin = dp(4f) else leftMargin = dp(4f)
        }
    )
    if (previewBitmap == null || previewBitmap.isRecycled) {
        val glyph = TextView(context).apply {
            text = if (state.phase == "waiting_user") "!" else "屏"
            textSize = 14f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(0xFF1C1C1E.toInt())
            gravity = Gravity.CENTER
        }
        bubble.addView(glyph, FrameLayout.LayoutParams(-1, -1, Gravity.CENTER))
    }
    root.addView(bubble, FrameLayout.LayoutParams(dp(48f), dp(48f), Gravity.BOTTOM or (if (dockLeft) Gravity.START else Gravity.END)))
    if (onClose != null) {
        val closeBtn = PipVectorIconButton(context, PipVectorIconButton.IconKind.POWER_CLOSE, isDanger = true) {
            onClose()
        }.apply { contentDescription = "关闭虚拟屏" }
        root.addView(
            closeBtn,
            FrameLayout.LayoutParams(dp(18f), dp(18f), Gravity.TOP or (if (dockLeft) Gravity.END else Gravity.START))
        )
    }
    FloatingVirtualDisplay.attachEdgeBubbleDragAndTap(context, bubble, onTap = onRestore)
    return root
}

@SuppressLint("ClickableViewAccessibility")
internal fun createTopGrabberHandle(
    context: Context,
    dp: (Float) -> Int
): FrameLayout {
    val hitZone = FrameLayout(context).apply {
        contentDescription = "拖动虚拟屏位置（拖至左右边缘变为气泡）"
    }
    val bar = View(context).apply {
        background = GradientDrawable().apply {
            setColor(0xE8FFFFFF.toInt())
            cornerRadius = dp(2.5f).toFloat()
            setStroke(1, 0x40000000)
        }
        elevation = dp(2.5f).toFloat()
    }
    hitZone.addView(
        bar,
        FrameLayout.LayoutParams(dp(36f), dp(4f).coerceAtLeast(3), Gravity.TOP or Gravity.CENTER_HORIZONTAL).apply {
            topMargin = dp(3f)
        }
    )
    return hitZone
}

internal fun createExpandedToolCapsule(
    context: Context,
    sigma: Float,
    dp: (Float) -> Int,
    onToggleApps: () -> Unit
): LinearLayout {
    val capsule = LinearLayout(context).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        val pH = dp(4f * sigma).coerceIn(dp(3f), dp(6f))
        val pV = dp(2.5f * sigma).coerceIn(dp(2f), dp(4.5f))
        setPadding(pH, pV, pH, pV)
        background = GradientDrawable().apply {
            setColor(0xC218181E.toInt())
            cornerRadius = dp(15f).toFloat()
            setStroke(dp(0.75f).coerceAtLeast(1), 0x42FFFFFF)
        }
        elevation = dp(4f).toFloat()
    }
    val padH = dp(7f * sigma).coerceIn(dp(5f), dp(9.5f))
    val padV = dp(3f * sigma).coerceIn(dp(2f), dp(4.5f))
    val textSp = (10f * sigma).coerceIn(9f, 11.5f)
    val appsBtn = TextView(context).apply {
        text = "应用"
        contentDescription = "虚拟屏应用抽屉"
        textSize = textSp
        setTextColor(0xF5FFFFFF.toInt())
        typeface = Typeface.DEFAULT_BOLD
        setPadding(padH, padV, padH, padV)
        background = GradientDrawable().apply {
            setColor(0x28FFFFFF)
            cornerRadius = dp(10f).toFloat()
        }
        setOnClickListener { onToggleApps() }
    }
    capsule.addView(appsBtn, LinearLayout.LayoutParams(-2, -2))
    return capsule
}

internal fun createExpandedControlCapsule(
    context: Context,
    sigma: Float,
    dp: (Float) -> Int,
    onPromoteFullscreen: () -> Unit,
    onClose: () -> Unit
): LinearLayout {
    val capsule = LinearLayout(context).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        val pH = dp(4f * sigma).coerceIn(dp(3f), dp(6f))
        val pV = dp(2.5f * sigma).coerceIn(dp(2f), dp(4.5f))
        setPadding(pH, pV, pH, pV)
        background = GradientDrawable().apply {
            setColor(0xC218181E.toInt())
            cornerRadius = dp(15f).toFloat()
            setStroke(dp(0.75f).coerceAtLeast(1), 0x42FFFFFF)
        }
        elevation = dp(4f).toFloat()
    }
    val fullBtn = TextView(context).apply {
        text = "全屏"
        contentDescription = "切到全屏继续"
        textSize = (10f * sigma).coerceIn(9f, 11.5f)
        setTextColor(0xF5FFFFFF.toInt())
        typeface = Typeface.DEFAULT_BOLD
        val padH = dp(6.5f * sigma).coerceIn(dp(4.5f), dp(9f))
        val padV = dp(3f * sigma).coerceIn(dp(2f), dp(4.5f))
        setPadding(padH, padV, padH, padV)
        background = GradientDrawable().apply {
            setColor(0x28FFFFFF)
            cornerRadius = dp(10f).toFloat()
        }
        setOnClickListener { onPromoteFullscreen() }
    }
    val space = dp(3.5f * sigma).coerceIn(dp(2.5f), dp(5f))
    val closeSize = dp(20.5f * sigma).coerceIn(dp(18f), dp(24f))
    val closeBtn = PipVectorIconButton(context, PipVectorIconButton.IconKind.POWER_CLOSE, isDanger = true) {
        onClose()
    }.apply { contentDescription = "关闭虚拟屏" }

    capsule.addView(fullBtn, LinearLayout.LayoutParams(-2, -2).apply { rightMargin = space })
    capsule.addView(closeBtn, LinearLayout.LayoutParams(closeSize, closeSize))
    return capsule
}

internal fun createInlineImeInputBar(
    context: Context,
    sigma: Float,
    dp: (Float) -> Int,
    onSend: (String) -> Unit,
    onClose: () -> Unit
): Pair<LinearLayout, EditText> {
    val bar = LinearLayout(context).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        setPadding(dp(8f * sigma), dp(5f * sigma), dp(6f * sigma), dp(5f * sigma))
        background = GradientDrawable().apply {
            setColor(0xE8181A22.toInt())
            cornerRadius = dp(14f).toFloat()
            setStroke(dp(0.8f).coerceAtLeast(1), 0x55FFFFFF)
        }
        elevation = dp(6f).toFloat()
    }
    val edit = EditText(context).apply {
        hint = "输入中文/文本发送到虚拟屏…"
        setHintTextColor(0x88FFFFFF.toInt())
        setTextColor(AndroidColor.WHITE)
        textSize = (11.5f * sigma).coerceIn(11f, 14f)
        background = null
        isSingleLine = true
        inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_AUTO_CORRECT
        imeOptions = EditorInfo.IME_ACTION_SEND
        setPadding(dp(4f), dp(3f), dp(6f), dp(3f))
    }
    fun commitInput() {
        val txt = edit.text?.toString().orEmpty()
        if (txt.isNotEmpty()) {
            edit.setText("")
            onSend(txt)
        }
    }
    edit.setOnEditorActionListener { _, actionId, event ->
        val enterUp = event != null && event.keyCode == KeyEvent.KEYCODE_ENTER && event.action == KeyEvent.ACTION_UP
        if (actionId == EditorInfo.IME_ACTION_SEND || actionId == EditorInfo.IME_ACTION_DONE || enterUp) {
            commitInput()
            true
        } else false
    }
    val sendBtn = TextView(context).apply {
        text = "发送"
        textSize = (10.5f * sigma).coerceIn(10f, 12.5f)
        typeface = Typeface.DEFAULT_BOLD
        setTextColor(AndroidColor.WHITE)
        setPadding(dp(9f), dp(5f), dp(9f), dp(5f))
        background = GradientDrawable().apply {
            setColor(0xFF0A84FF.toInt())
            cornerRadius = dp(9f).toFloat()
        }
        setOnClickListener { commitInput() }
    }
    val closeBtn = TextView(context).apply {
        text = "✕"
        textSize = (11f * sigma).coerceIn(10f, 13f)
        setTextColor(0xCCFFFFFF.toInt())
        setPadding(dp(7f), dp(4f), dp(7f), dp(4f))
        setOnClickListener {
            val imm = context.getSystemService(InputMethodManager::class.java)
            imm?.hideSoftInputFromWindow(edit.windowToken, 0)
            onClose()
        }
    }
    bar.addView(edit, LinearLayout.LayoutParams(0, -2, 1f))
    bar.addView(sendBtn, LinearLayout.LayoutParams(-2, -2).apply { leftMargin = dp(4f) })
    bar.addView(closeBtn, LinearLayout.LayoutParams(-2, -2).apply { leftMargin = dp(2f) })
    return bar to edit
}

@Composable
internal fun PearlEdgeBubbleComposable(
    state: RootPhoneState,
    previewBitmap: androidx.compose.ui.graphics.ImageBitmap?,
    onRestore: () -> Unit,
    onClose: () -> Unit,
    modifier: Modifier = Modifier
) {
    val borderCol = Color(pearlOuterStrokeColorInt(state.phase))
    val dotCol = Color(pearlStatusDotColorInt(state.phase))
    Box(modifier.size(56.dp)) {
        Box(
            Modifier
                .align(Alignment.BottomStart)
                .size(48.dp)
                .shadow(12.dp, CircleShape)
                .clip(CircleShape)
                .background(Color(PEARL_WHITE_FILL_INT))
                .border(1.2.dp, borderCol, CircleShape)
                .iosPressScale(0.93f) { onRestore() }
                .semantics { contentDescription = "展开虚拟屏挂起球" },
            contentAlignment = Alignment.Center
        ) {
            if (previewBitmap != null) {
                Image(
                    bitmap = previewBitmap,
                    contentDescription = "虚拟屏缩略图",
                    modifier = Modifier.fillMaxSize(),
                    contentScale = androidx.compose.ui.layout.ContentScale.Crop,
                    alpha = 0.88f
                )
            } else {
                Text(
                    if (state.phase == "waiting_user") "!" else "屏",
                    color = Color(0xFF1C1C1E),
                    fontSize = 14.sp,
                    fontWeight = FontWeight.Bold
                )
            }
            Box(
                Modifier
                    .align(Alignment.TopStart)
                    .padding(4.dp)
                    .size(9.dp)
                    .clip(CircleShape)
                    .background(dotCol)
                    .border(1.dp, Color(0xE6FFFFFF), CircleShape)
            )
        }
        ComposePipCircleButton(
            modifier = Modifier
                .align(Alignment.TopEnd),
            isDanger = true,
            contentLabel = "关闭虚拟屏",
            onClick = onClose
        ) {
            val strokeW = 1.3.dp.toPx()
            val c = Color(0xFFFF7A72)
            drawLine(c, Offset(size.width * 0.32f, size.height * 0.32f), Offset(size.width * 0.68f, size.height * 0.68f), strokeW, StrokeCap.Round)
            drawLine(c, Offset(size.width * 0.68f, size.height * 0.32f), Offset(size.width * 0.32f, size.height * 0.68f), strokeW, StrokeCap.Round)
        }
    }
}

@Composable
internal fun BoxScope.ExpandedFloatingChromeComposable(
    state: RootPhoneState,
    inputBarOpen: Boolean,
    onToggleApps: () -> Unit,
    onPromoteFullscreen: () -> Unit,
    onClose: () -> Unit,
    onToggleInput: () -> Unit,
    onBackOrResume: () -> Unit
) {
    Box(
        Modifier
            .align(Alignment.TopCenter)
            .padding(top = 4.dp)
            .width(36.dp)
            .height(4.dp)
            .clip(CircleShape)
            .background(Color(0xD8FFFFFF))
            .border(0.5.dp, Color(0x38000000), CircleShape)
    )
    Row(
        Modifier
            .align(Alignment.TopStart)
            .padding(start = 6.dp, top = 7.dp)
            .clip(RoundedCornerShape(15.dp))
            .background(Color(0xC218181E))
            .border(0.5.dp, Color(0x42FFFFFF), RoundedCornerShape(15.dp))
            .padding(horizontal = 4.dp, vertical = 2.5.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(
            "应用",
            color = Color(0xF5FFFFFF),
            fontSize = 9.5.sp,
            fontWeight = FontWeight.Bold,
            modifier = Modifier
                .clip(RoundedCornerShape(10.dp))
                .background(Color(0x28FFFFFF))
                .clickable(onClick = onToggleApps)
                .semantics { contentDescription = "虚拟屏应用抽屉" }
                .padding(horizontal = 8.dp, vertical = 4.dp)
        )
    }
    Row(
        Modifier
            .align(Alignment.TopEnd)
            .padding(end = 6.dp, top = 7.dp)
            .clip(RoundedCornerShape(15.dp))
            .background(Color(0xC218181E))
            .border(0.5.dp, Color(0x42FFFFFF), RoundedCornerShape(15.dp))
            .padding(horizontal = 4.dp, vertical = 2.5.dp),
        horizontalArrangement = Arrangement.spacedBy(3.5.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(
            "全屏",
            color = Color(0xF5FFFFFF),
            fontSize = 9.5.sp,
            fontWeight = FontWeight.Bold,
            modifier = Modifier
                .clip(RoundedCornerShape(10.dp))
                .background(Color(0x28FFFFFF))
                .clickable(onClick = onPromoteFullscreen)
                .semantics { contentDescription = "全屏虚拟屏" }
                .padding(horizontal = 7.dp, vertical = 4.dp)
        )
        ComposePipCircleButton(
            modifier = Modifier.size(28.dp),
            isDanger = true,
            contentLabel = "关闭虚拟屏",
            onClick = onClose
        ) {
            val strokeW = 1.4.dp.toPx()
            val c = Color(0xFFFF7A72)
            drawLine(c, Offset(size.width * 0.32f, size.height * 0.32f), Offset(size.width * 0.68f, size.height * 0.68f), strokeW, StrokeCap.Round)
            drawLine(c, Offset(size.width * 0.68f, size.height * 0.32f), Offset(size.width * 0.32f, size.height * 0.68f), strokeW, StrokeCap.Round)
        }
    }
    if (!inputBarOpen) {
        Row(
            Modifier
                .align(Alignment.BottomStart)
                .fillMaxWidth()
                .padding(start = 8.dp, end = 34.dp, bottom = 7.dp)
                .horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Box(
                Modifier
                    .clip(RoundedCornerShape(12.dp))
                    .background(if (state.phase == "waiting_user") Color(0xFFFF9F0A) else Color(0xBF18181E))
                    .border(0.75.dp, Color(0x38FFFFFF), RoundedCornerShape(12.dp))
                    .clickable(onClick = onBackOrResume)
                    .padding(horizontal = 10.dp, vertical = 4.dp)
            ) {
                Text(
                    if (state.phase == "waiting_user") "我处理好了，继续" else "‹ 返回",
                    color = if (state.phase == "waiting_user") Color.Black else Color(0xF2FFFFFF),
                    fontSize = 10.sp,
                    fontWeight = FontWeight.Bold
                )
            }
            Box(
                Modifier
                    .clip(RoundedCornerShape(12.dp))
                    .background(Color(0xBF18181E))
                    .border(0.75.dp, Color(0x38FFFFFF), RoundedCornerShape(12.dp))
                    .clickable(onClick = onToggleInput)
                    .padding(horizontal = 9.dp, vertical = 4.dp)
            ) {
                Text(
                    "⌨ 输入",
                    color = Color(0xF2FFFFFF),
                    fontSize = 10.sp,
                    fontWeight = FontWeight.Bold
                )
            }
        }
    }
}

@Composable
internal fun BoxScope.InAppInlineImeInputBar(
    onSend: (String) -> Unit,
    onClose: () -> Unit
) {
    var draft by remember { mutableStateOf("") }
    val focusRequester = remember { FocusRequester() }
    LaunchedEffect(Unit) { focusRequester.requestFocus() }
    fun commit() {
        val txt = draft
        if (txt.isNotEmpty()) {
            draft = ""
            onSend(txt)
        }
    }
    Row(
        Modifier
            .align(Alignment.BottomCenter)
            .fillMaxWidth()
            .padding(horizontal = 7.dp, vertical = 7.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(Color(0xE8181A22))
            .border(0.8.dp, Color(0x55FFFFFF), RoundedCornerShape(14.dp))
            .padding(horizontal = 9.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp)
    ) {
        BasicTextField(
            value = draft,
            onValueChange = { draft = it },
            singleLine = true,
            textStyle = TextStyle(color = Color.White, fontSize = 11.5.sp),
            cursorBrush = SolidColor(Color(0xFF0A84FF)),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
            keyboardActions = KeyboardActions(onSend = { commit() }),
            modifier = Modifier.weight(1f).focusRequester(focusRequester),
            decorationBox = { inner ->
                if (draft.isEmpty()) {
                    Text("输入中文/文本发送到虚拟屏…", color = Color(0x88FFFFFF), fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                inner()
            }
        )
        Text(
            "发送",
            color = Color.White,
            fontSize = 10.5.sp,
            fontWeight = FontWeight.Bold,
            modifier = Modifier
                .clip(RoundedCornerShape(9.dp))
                .background(Color(0xFF0A84FF))
                .clickable(onClick = { commit() })
                .padding(horizontal = 8.dp, vertical = 4.dp)
        )
        Text(
            "✕",
            color = Color(0xCCFFFFFF),
            fontSize = 11.sp,
            modifier = Modifier
                .clip(CircleShape)
                .clickable(onClick = onClose)
                .padding(horizontal = 5.dp, vertical = 3.dp)
        )
    }
}

@Composable
internal fun InAppVirtualAppDrawerOverlay(
    apps: List<PhoneApp>,
    onLaunchApp: (String) -> Unit,
    onClose: () -> Unit
) {
    Column(
        Modifier
            .fillMaxSize()
            .background(Color(0xF5111824))
            .padding(start = 8.dp, end = 8.dp, top = 36.dp, bottom = 8.dp)
    ) {
        Row(Modifier.fillMaxWidth().padding(bottom = 6.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("虚拟屏应用 (${apps.size})", color = Color(0xFFF8FAFC), fontSize = 11.5.sp, fontWeight = FontWeight.Bold, style = TextStyle(fontFeatureSettings = "tnum"))
            Spacer(Modifier.weight(1f))
            Text(
                "返回画面 ✕",
                color = Color(0xFF93C5FD),
                fontSize = 10.sp,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier
                    .clip(RoundedCornerShape(8.dp))
                    .background(Color(0x260A84FF))
                    .clickable(onClick = onClose)
                    .padding(horizontal = 7.dp, vertical = 3.dp)
            )
        }
        if (apps.isEmpty()) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text("正在加载应用列表…", color = Color(0xFF94A3B8), fontSize = 11.sp)
            }
        } else {
            LazyColumn(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                items(apps.take(40)) { app ->
                    Row(
                        Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(8.dp))
                            .background(Color(0x18FFFFFF))
                            .clickable { onLaunchApp(app.component) }
                            .padding(horizontal = 8.dp, vertical = 6.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            app.name,
                            color = Color(0xFFF1F5F9),
                            fontSize = 11.sp,
                            fontWeight = FontWeight.Medium,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis
                        )
                    }
                }
            }
        }
    }
}
