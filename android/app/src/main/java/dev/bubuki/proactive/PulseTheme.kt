package dev.bubuki.proactive

import android.content.Context
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.enableEdgeToEdge
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.*
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.hapticfeedback.HapticFeedback
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalHapticFeedback

data class PulseColors(
    val fill: Color,
    val hair: Color,
    val ink: Color,
    val accent: Color,
    val codeBg: Color = fill,
    val codeHeader: Color = hair.copy(alpha = 0.28f),
    val isDark: Boolean = false
) { val ink2: Color get() = ink.copy(alpha = .60f) }
val LightColors = PulseColors(Color(0xFFEAEAEF), Color(0x1A000000), Color(0xFF000000), Color(0xFF007AFF), Color(0xFFF5F6F8), Color(0xFFECEEF2), false)
internal val DarkColors = PulseColors(Color(0xFF242426), Color(0x26FFFFFF), Color(0xFFFFFFFF), Color(0xFF0A84FF), Color(0xFF111216), Color(0xFF181A20), true)
internal val LocalPulseColors = staticCompositionLocalOf { DarkColors }
internal val LocalReduceMotion = staticCompositionLocalOf { false }
internal val QuietHaptics: HapticFeedback = object : HapticFeedback { override fun performHapticFeedback(hapticFeedbackType: HapticFeedbackType) {} }

fun animationsOff(context: Context): Boolean = Settings.Global.getFloat(context.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) == 0f
@Composable fun appearanceDark(): Boolean {
    val settings by UiPreferences.changes(LocalContext.current).collectAsState()
    return when (settings.appearance) { "light" -> false; "dark" -> true; else -> isSystemInDarkTheme() }
}
fun ComponentActivity.systemBarsFor(dark: Boolean) {
    val transparent = android.graphics.Color.TRANSPARENT
    enableEdgeToEdge(statusBarStyle = if (dark) SystemBarStyle.dark(transparent) else SystemBarStyle.light(transparent, transparent),
        navigationBarStyle = if (dark) SystemBarStyle.dark(transparent) else SystemBarStyle.light(transparent, transparent))
}
object PulseTheme {
    val colors: PulseColors @Composable @ReadOnlyComposable get() = LocalPulseColors.current
    @Composable operator fun invoke(dark: Boolean, reduceMotion: Boolean = false, content: @Composable () -> Unit) {
        val context = LocalContext.current
        val settings by UiPreferences.changes(context).collectAsState()
        val feedback = LocalHapticFeedback.current
        val powerSaveActive = LocalPowerSaveMode.current
        val effectiveReduceMotion = reduceMotion || animationsOff(context) || powerSaveActive
        val targetPalette = if (dark) DarkColors else LightColors
        val palette = if (effectiveReduceMotion) {
            targetPalette
        } else {
            val spec = remember { tween<Color>(durationMillis = 220, easing = FastOutSlowInEasing) }
            val fill by animateColorAsState(targetPalette.fill, spec, label = "pt_fill")
            val hair by animateColorAsState(targetPalette.hair, spec, label = "pt_hair")
            val ink by animateColorAsState(targetPalette.ink, spec, label = "pt_ink")
            val accent by animateColorAsState(targetPalette.accent, spec, label = "pt_accent")
            val codeBg by animateColorAsState(targetPalette.codeBg, spec, label = "pt_codeBg")
            val codeHeader by animateColorAsState(targetPalette.codeHeader, spec, label = "pt_codeHeader")
            remember(fill, hair, ink, accent, codeBg, codeHeader, dark) {
                PulseColors(fill, hair, ink, accent, codeBg, codeHeader, dark)
            }
        }
        val targetBg = if (dark) Color(0xFF000000) else Color(0xFFF5F5F7)
        val bg = if (effectiveReduceMotion) targetBg else animateColorAsState(targetBg, tween(220, easing = FastOutSlowInEasing), label = "pt_bg").value
        val scheme = if (dark) {
            darkColorScheme(
                primary = palette.accent,
                background = bg,
                surface = palette.fill,
                surfaceVariant = palette.fill,
                onBackground = palette.ink,
                onSurface = palette.ink,
                outline = palette.hair
            )
        } else {
            lightColorScheme(
                primary = palette.accent,
                background = bg,
                surface = palette.fill,
                surfaceVariant = palette.fill,
                onBackground = palette.ink,
                onSurface = palette.ink,
                outline = palette.hair
            )
        }
        CompositionLocalProvider(
            LocalPulseColors provides palette,
            LocalReduceMotion provides effectiveReduceMotion,
            LocalHapticFeedback provides if (settings.haptics) feedback else QuietHaptics
        ) {
            MaterialTheme(colorScheme = scheme, content = content)
        }
    }
}
