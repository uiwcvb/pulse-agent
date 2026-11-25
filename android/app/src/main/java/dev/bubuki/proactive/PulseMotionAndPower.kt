package dev.bubuki.proactive

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.os.PowerManager
import androidx.compose.animation.ContentTransform
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner

/**
 * [Module] PulseMotionAndPower
 * [Responsibility]
 * App-wide fluid motion helpers (`rememberAnimatedPulsePalette`, `appSubPageTransitionSpec`),
 * lifecycle-aware ticker gating (`rememberLifecycleStarted`), system & manual battery-saver
 * coordination (`PowerSaveController`), and the extracted Settings Appearance + Power-Save card.
 */

internal val LocalPowerSaveMode = compositionLocalOf { false }

@Composable
internal fun rememberReducedMotion(): Boolean = LocalReduceMotion.current || LocalPowerSaveMode.current

internal object PowerSaveController {
    @JvmStatic
    fun isSystemPowerSave(context: Context): Boolean = runCatching {
        context.applicationContext.getSystemService(PowerManager::class.java)?.isPowerSaveMode == true
    }.getOrDefault(false)

    @JvmStatic
    fun isPowerSaveActive(context: Context): Boolean =
        UiPreferences.readPowerSave(context) || isSystemPowerSave(context)

    @JvmStatic
    fun setManualPowerSave(context: Context, enabled: Boolean) {
        UiPreferences.writePowerSave(context, enabled)
    }
}

@Composable
internal fun rememberPowerSaveActive(manualFlag: Boolean): Boolean {
    val context = LocalContext.current
    val prefsState by UiPreferences.changes(context).collectAsState()
    var sysSave by remember(context) { mutableStateOf(PowerSaveController.isSystemPowerSave(context)) }
    DisposableEffect(context) {
        val appCtx = context.applicationContext
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(c: Context?, intent: Intent?) {
                sysSave = PowerSaveController.isSystemPowerSave(appCtx)
            }
        }
        val filter = IntentFilter(PowerManager.ACTION_POWER_SAVE_MODE_CHANGED)
        runCatching {
            if (Build.VERSION.SDK_INT >= 33) {
                appCtx.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
            } else {
                appCtx.registerReceiver(receiver, filter)
            }
        }
        onDispose {
            runCatching { appCtx.unregisterReceiver(receiver) }
        }
    }
    return manualFlag || prefsState.powerSave || sysSave
}

@Composable
internal fun rememberLifecycleStarted(): Boolean {
    val owner = LocalLifecycleOwner.current
    var started by remember(owner) {
        mutableStateOf(owner.lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED))
    }
    DisposableEffect(owner) {
        val observer = LifecycleEventObserver { _, _ ->
            started = owner.lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)
        }
        owner.lifecycle.addObserver(observer)
        onDispose { owner.lifecycle.removeObserver(observer) }
    }
    return started
}

@Composable
internal fun rememberAnimatedPulsePalette(dark: Boolean, reduceMotion: Boolean): PulsePalette {
    val target = remember(dark) { pulsePalette(dark) }
    if (reduceMotion) return target
    val spec = remember { tween<Color>(durationMillis = 220, easing = FastOutSlowInEasing) }
    val bg by animateColorAsState(target.bg, spec, label = "pal_bg")
    val chatBg by animateColorAsState(target.chatBg, spec, label = "pal_chatBg")
    val surface by animateColorAsState(target.surface, spec, label = "pal_surface")
    val elevated by animateColorAsState(target.elevated, spec, label = "pal_elevated")
    val pinnedRowBg by animateColorAsState(target.pinnedRowBg, spec, label = "pal_pinned")
    val userBubble by animateColorAsState(target.userBubble, spec, label = "pal_userBubble")
    val aiBubble by animateColorAsState(target.aiBubble, spec, label = "pal_aiBubble")
    val fill by animateColorAsState(target.fill, spec, label = "pal_fill")
    val line by animateColorAsState(target.line, spec, label = "pal_line")
    val ink by animateColorAsState(target.ink, spec, label = "pal_ink")
    val muted by animateColorAsState(target.muted, spec, label = "pal_muted")
    val faint by animateColorAsState(target.faint, spec, label = "pal_faint")
    val accent by animateColorAsState(target.accent, spec, label = "pal_accent")
    val green by animateColorAsState(target.green, spec, label = "pal_green")
    val amber by animateColorAsState(target.amber, spec, label = "pal_amber")
    val warn by animateColorAsState(target.warn, spec, label = "pal_warn")
    val codeBg by animateColorAsState(target.codeBg, spec, label = "pal_codeBg")
    val codeHeader by animateColorAsState(target.codeHeader, spec, label = "pal_codeHeader")
    return remember(bg, chatBg, surface, elevated, pinnedRowBg, userBubble, aiBubble, fill, line, ink, muted, faint, accent, green, amber, warn, codeBg, codeHeader, target) {
        target.copy(
            bg = bg,
            chatBg = chatBg,
            surface = surface,
            elevated = elevated,
            pinnedRowBg = pinnedRowBg,
            userBubble = userBubble,
            aiBubble = aiBubble,
            fill = fill,
            line = line,
            ink = ink,
            muted = muted,
            faint = faint,
            accent = accent,
            green = green,
            amber = amber,
            warn = warn,
            codeBg = codeBg,
            codeHeader = codeHeader
        )
    }
}

internal fun appSubPageTransitionSpec(goingDeeper: Boolean, reduceMotion: Boolean): ContentTransform {
    if (reduceMotion) {
        return fadeIn(tween(110)) togetherWith fadeOut(tween(90))
    }
    val enterSlide = slideInHorizontally(iosFluidSpring(false)) { w ->
        if (goingDeeper) w / 4 else -w / 5
    }
    val exitSlide = slideOutHorizontally(iosGentleSpring(false)) { w ->
        if (goingDeeper) -w / 6 else w / 4
    }
    return (enterSlide + fadeIn(tween(170))) togetherWith (exitSlide + fadeOut(tween(110)))
}

@Composable
internal fun SettingsAppearanceSubPage(model: NativePulseModel) {
    val s = model.ui
    val c = LocalPulsePalette.current
    val powerSaveActive = LocalPowerSaveMode.current
    val sysPowerSave = PowerSaveController.isSystemPowerSave(LocalContext.current) || (powerSaveActive && !s.powerSave)
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(16.dp))
            .background(c.surface)
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Text("外观、动效与省电", color = c.ink, fontSize = 15.5.sp, fontWeight = FontWeight.SemiBold)
        Row(
            Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(11.dp))
                .background(c.fill)
                .padding(3.dp),
            horizontalArrangement = Arrangement.spacedBy(4.dp)
        ) {
            listOf("dark" to "深色模式", "light" to "浅色模式").forEach { (key, label) ->
                val active = (key == "dark" && s.appearance != "light") || (key == "light" && s.appearance == "light")
                Text(
                    label,
                    Modifier
                        .weight(1f)
                        .clip(RoundedCornerShape(8.dp))
                        .background(if (active) c.surface else Color.Transparent)
                        .iosPressScale(0.96f) { model.updateSettingsField(appearance = key) }
                        .padding(vertical = 9.dp),
                    color = if (active) c.ink else c.muted,
                    fontSize = 13.sp,
                    fontWeight = if (active) FontWeight.SemiBold else FontWeight.Normal,
                    textAlign = TextAlign.Center
                )
            }
        }
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("流体弹簧过渡动效", color = c.ink, fontSize = 14.5.sp)
                Text("页面推入、主题渐变、气泡与虚拟屏连续变形", color = c.muted, fontSize = 11.5.sp)
            }
            Switch(checked = s.motion, onCheckedChange = { model.updateSettingsField(motion = it) })
        }
        HorizontalDivider(color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("智能省电模式", color = c.ink, fontSize = 14.5.sp)
                Text(
                    if (sysPowerSave) "系统省电已开启：已自动降频预览并暂停非必要休眠唤醒"
                    else "挂起/息屏自动停帧采样，拉长后台心跳间隔并降低虚拟屏帧率",
                    color = c.muted,
                    fontSize = 11.5.sp
                )
            }
            Switch(
                checked = s.powerSave,
                onCheckedChange = { model.togglePowerSave(it) },
                modifier = Modifier.semantics { contentDescription = "智能省电模式开关" }
            )
        }
        HorizontalDivider(color = c.line.copy(alpha = 0.55f), thickness = 0.5.dp)
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("触感震动反馈", color = c.ink, fontSize = 14.5.sp)
                Text("左滑引用、右滑置顶、切换状态时提供物理震动", color = c.muted, fontSize = 11.5.sp)
            }
            Switch(checked = s.haptics, onCheckedChange = { model.updateSettingsField(haptics = it) })
        }
    }
}
