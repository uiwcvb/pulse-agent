package dev.bubuki.proactive

import android.app.Instrumentation
import android.app.UiAutomation
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Shader
import android.os.ParcelFileDescriptor
import android.os.SystemClock
import android.util.Base64
import android.view.InputDevice
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.nio.charset.StandardCharsets

@RunWith(AndroidJUnit4::class)
class VirtualDisplayV401AcceptanceTest {
    private val ins: Instrumentation = InstrumentationRegistry.getInstrumentation()
    private val uiAuto: UiAutomation = ins.uiAutomation

    private fun shell(cmd: String): String {
        val pfd: ParcelFileDescriptor = uiAuto.executeShellCommand(cmd)
        return pfd.use { fd ->
            FileInputStream(fd.fileDescriptor).use { input ->
                String(Io.read(input, 65536), StandardCharsets.UTF_8)
            }
        }
    }

    private fun makeSamplePngBase64(w: Int = 540, h: Int = 1170): String {
        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bmp)
        val bgPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            shader = LinearGradient(
                0f, 0f, 0f, h.toFloat(),
                0xFF141824.toInt(), 0xFF1F273B.toInt(),
                Shader.TileMode.CLAMP
            )
        }
        canvas.drawRect(0f, 0f, w.toFloat(), h.toFloat(), bgPaint)
        val cardPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0x22FFFFFF }
        canvas.drawRoundRect(RectF(36f, 110f, w - 36f, 340f), 28f, 28f, cardPaint)
        canvas.drawRoundRect(RectF(36f, 370f, w - 36f, 640f), 28f, 28f, cardPaint)
        val accentPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = 0xFF0A84FF.toInt() }
        canvas.drawRoundRect(RectF(64f, 148f, 220f, 196f), 18f, 18f, accentPaint)
        val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = 0xF2FFFFFF.toInt()
            textSize = 30f
        }
        canvas.drawText("虚拟屏应用画面", 64f, 275f, textPaint)
        val out = ByteArrayOutputStream()
        bmp.compress(Bitmap.CompressFormat.PNG, 92, out)
        bmp.recycle()
        return Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
    }

    private fun saveShot(fileName: String) {
        ins.waitForIdleSync()
        SystemClock.sleep(450)
        val bmp = uiAuto.takeScreenshot()
        assertNotNull("takeScreenshot returned null for $fileName", bmp)
        val outDir = ins.targetContext.getExternalFilesDir(null)
        assertNotNull("externalFilesDir is null", outDir)
        outDir!!.mkdirs()
        val file = File(outDir, fileName)
        FileOutputStream(file).use { fos ->
            bmp!!.compress(Bitmap.CompressFormat.PNG, 100, fos)
        }
        bmp!!.recycle()
    }

    private fun tapScreen(x: Float, y: Float) {
        val downTime = SystemClock.uptimeMillis()
        val down = MotionEvent.obtain(downTime, downTime, MotionEvent.ACTION_DOWN, x, y, 0)
        down.source = InputDevice.SOURCE_TOUCHSCREEN
        assertTrue("ACTION_DOWN failed", uiAuto.injectInputEvent(down, true))
        SystemClock.sleep(60)
        val up = MotionEvent.obtain(downTime, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, x, y, 0)
        up.source = InputDevice.SOURCE_TOUCHSCREEN
        assertTrue("ACTION_UP failed", uiAuto.injectInputEvent(up, true))
        down.recycle()
        up.recycle()
        ins.waitForIdleSync()
    }

    private fun waitUntil(timeoutMs: Long, cond: () -> Boolean): Boolean {
        val deadline = SystemClock.uptimeMillis() + timeoutMs
        while (SystemClock.uptimeMillis() < deadline) {
            if (cond()) return true
            SystemClock.sleep(100)
        }
        return cond()
    }

    private fun findOverlayViewByDesc(root: View?, desc: String): View? {
        if (root == null) return null
        if (root.contentDescription?.toString() == desc && root.visibility == View.VISIBLE) return root
        if (root is ViewGroup) {
            for (i in 0 until root.childCount) {
                val found = findOverlayViewByDesc(root.getChildAt(i), desc)
                if (found != null) return found
            }
        }
        return null
    }

    private fun tapOverlayView(desc: String) {
        var cx = -1f
        var cy = -1f
        ins.runOnMainSync {
            val target = findOverlayViewByDesc(FloatingVirtualDisplay.rootView, desc)
            assertNotNull("Overlay view '$desc' not found", target)
            val loc = IntArray(2)
            target!!.getLocationOnScreen(loc)
            cx = loc[0] + target.width / 2f
            cy = loc[1] + target.height / 2f
        }
        assertTrue("Invalid tap coordinates for '$desc': ($cx, $cy)", cx >= 0f && cy >= 0f)
        tapScreen(cx, cy)
    }

    private fun tapCurrentOverlayCenter() {
        var tapX = 0f
        var tapY = 0f
        ins.runOnMainSync {
            val lp = FloatingVirtualDisplay.params!!
            tapX = lp.x + lp.width / 2f
            tapY = lp.y + lp.height * 0.55f
        }
        tapScreen(tapX, tapY)
    }

    @Test
    fun verifyInAppAndSystemOverlayVirtualDisplay() {
        val ctx = ins.targetContext
        val pkg = ctx.packageName
        shell("pm grant $pkg android.permission.POST_NOTIFICATIONS")
        shell("appops set $pkg SYSTEM_ALERT_WINDOW ignore")

        val monitor = ins.addMonitor(NativePulseActivity::class.java.name, null, false)
        val launch = Intent(ctx, NativePulseActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        ctx.startActivity(launch)
        val activity = ins.waitForMonitorWithTimeout(monitor, 12_000) as? NativePulseActivity
        ins.removeMonitor(monitor)
        assertNotNull("NativePulseActivity did not launch", activity)
        assertTrue("Window never gained focus", waitUntil(8_000) { activity!!.hasWindowFocus() })

        val samplePngB64 = makeSamplePngBase64()
        val portraitState = RootPhoneState(
            root = true,
            privilegeMode = "root",
            uid = 0,
            display = """{"created":true,"displayId":2,"width":1080,"height":2340}""",
            image = samplePngB64,
            imageInfo = """{"width":540,"height":1170}""",
            phase = "running"
        )

        val initialPowerSave = UiPreferences.readPowerSave(ctx)
        val dm = ctx.resources.displayMetrics
        val screenWidthPx = minOf(dm.widthPixels, dm.heightPixels)
        try {
            // Sub-test 0: Non-hijack, TaskInfo windowConfiguration reflection & preview Markdown cleanup unit checks
            val unfilteredPull = DisplayInputHelper.pullTaskFromMainToVirtualDisplay(999, -1, "")
            assertTrue("Unfiltered pullTaskFromMainToVirtualDisplay must return -1, got $unfilteredPull", unfilteredPull == -1)
            val cleanedMd = stripMarkdownForPreview("**粗体** `code` [链接](https://example.com) # 标题")
            assertTrue("Unexpected cleaned Markdown preview: $cleanedMd", cleanedMd == "粗体 code 链接 标题")
            val rtf = android.app.ActivityManager.RecentTaskInfo()
            val curWinMode = DisplayTaskContainment.getTaskWindowingMode(rtf)
            assertTrue("getTaskWindowingMode should handle RecentTaskInfo safely ($curWinMode)", curWinMode >= 0)
            shell("settings put global hidden_api_policy 1")
            val syntheticFreeform = android.app.ActivityManager.RunningTaskInfo()
            val cfgObj = syntheticFreeform.javaClass.getField("configuration").get(syntheticFreeform)!!
            val winCfgObj = cfgObj.javaClass.getField("windowConfiguration").get(cfgObj)!!
            val setWinMode = winCfgObj.javaClass.getMethod("setWindowingMode", Int::class.javaPrimitiveType)
            setWinMode.invoke(winCfgObj, 1)
            assertFalse(
                "Fullscreen task (windowingMode=1) must NOT be detected as floating",
                DisplayTaskContainment.isFloatingOrMultiWindowTask(syntheticFreeform)
            )
            setWinMode.invoke(winCfgObj, 5)
            assertTrue(
                "Freeform task (windowingMode=5) must be detected as floating",
                DisplayTaskContainment.isFloatingOrMultiWindowTask(syntheticFreeform)
            )

            // Sub-test A: In-App PiP fallback when overlay permission is denied
            FloatingVirtualDisplay.hide()
            FloatingVirtualDisplay.resetUserDismissed()
            RootPhoneHarness.state.value = portraitState
            SystemClock.sleep(1_200)
            saveShot("verify_v403_vd_inapp_pip.png")

            // Sub-test B: Pearl-White Mini Overlay
            shell("appops set $pkg SYSTEM_ALERT_WINDOW allow")
            SystemClock.sleep(400)
            FloatingVirtualDisplay.hide()
            SystemClock.sleep(300)
            FloatingVirtualDisplay.show(activity!!, forceExpand = false)

            assertTrue(
                "System overlay did not become visible in collapsed mini mode",
                waitUntil(5_000) {
                    FloatingVirtualDisplay.overlayVisible.value &&
                        FloatingVirtualDisplay.rootView != null &&
                        !FloatingVirtualDisplay.expanded &&
                        FloatingVirtualDisplay.isAnyPreviewShowing()
                }
            )
            val miniParams = FloatingVirtualDisplay.params
            assertNotNull("Overlay LayoutParams null", miniParams)
            assertTrue("Overlay width <= 0", miniParams!!.width > 0)
            assertTrue("Overlay height <= 0", miniParams.height > 0)
            SystemClock.sleep(600)
            saveShot("verify_v403_vd_overlay_mini.png")

            // Sub-test C: Tap mini preview -> expanded pearl-white floating frame (NEVER fullscreen even during waiting_user)
            val waitingState = portraitState.copy(phase = "waiting_user")
            RootPhoneHarness.state.value = waitingState
            tapCurrentOverlayCenter()
            assertTrue(
                "Collapsed mini overlay did not expand on tap",
                waitUntil(3_000) { FloatingVirtualDisplay.expanded }
            )
            assertFalse("fullStage must stay false to avoid fullscreen takeover", FloatingVirtualDisplay.fullStage)
            val expandedParams = FloatingVirtualDisplay.params!!
            assertTrue(
                "Expanded overlay must stay floating (< 78% of screen width=${screenWidthPx}), got ${expandedParams.width}",
                expandedParams.width < (screenWidthPx * 0.78f).toInt()
            )
            RootPhoneHarness.state.value = portraitState
            SystemClock.sleep(250)
            ins.runOnMainSync {
                assertNotNull("Missing 应用 button", findOverlayViewByDesc(FloatingVirtualDisplay.rootView, "虚拟屏应用抽屉"))
                assertNotNull("Missing 尺寸切换 button", findOverlayViewByDesc(FloatingVirtualDisplay.rootView, "切换虚拟屏尺寸"))
                assertNotNull("Missing 挂起 button", findOverlayViewByDesc(FloatingVirtualDisplay.rootView, "挂起虚拟屏"))
                assertNotNull("Missing 收起 button", findOverlayViewByDesc(FloatingVirtualDisplay.rootView, "收起虚拟屏"))
                assertNotNull("Missing 关闭 button", findOverlayViewByDesc(FloatingVirtualDisplay.rootView, "关闭虚拟屏"))
            }
            SystemClock.sleep(500)
            saveShot("verify_v403_vd_overlay_expanded.png")

            // Sub-test C2: Open app drawer & cycle size tier inside floating overlay
            tapOverlayView("虚拟屏应用抽屉")
            SystemClock.sleep(500)
            saveShot("verify_v403_vd_app_drawer.png")
            tapOverlayView("虚拟屏应用抽屉")
            SystemClock.sleep(350)
            val tierBefore = FloatingVirtualDisplay.currentTier()
            tapOverlayView("切换虚拟屏尺寸")
            assertTrue(
                "Size tier did not cycle on tap",
                waitUntil(2_500) { FloatingVirtualDisplay.currentTier() != tierBefore }
            )
            val largeOrNextWidth = FloatingVirtualDisplay.params!!.width
            assertTrue(
                "Cycled overlay width must remain floating (< 78% of screen width=${screenWidthPx}), got $largeOrNextWidth",
                largeOrNextWidth < (screenWidthPx * 0.78f).toInt()
            )

            // Sub-test D: Real tap on "收起" -> collapses back to mini pearl-white PiP
            tapOverlayView("收起虚拟屏")
            assertTrue(
                "Expanded overlay did not collapse on 收起 tap",
                waitUntil(3_000) {
                    !FloatingVirtualDisplay.expanded &&
                        !FloatingVirtualDisplay.minimizedToPill &&
                        FloatingVirtualDisplay.isAnyPreviewShowing()
                }
            )
            SystemClock.sleep(650)
            saveShot("verify_v403_vd_collapsed.png")

            // Sub-test E: Expand again -> real tap on "挂起" -> docks into pearl-white edge pill & stops preview sampling
            tapCurrentOverlayCenter()
            assertTrue(
                "Collapsed overlay did not re-expand on tap",
                waitUntil(3_000) { FloatingVirtualDisplay.expanded }
            )
            SystemClock.sleep(450)
            tapOverlayView("挂起虚拟屏")
            assertTrue(
                "Overlay did not dock to edge pill on 挂起 tap",
                waitUntil(3_000) {
                    FloatingVirtualDisplay.minimizedToPill && !FloatingVirtualDisplay.isAnyPreviewShowing()
                }
            )
            SystemClock.sleep(600)
            saveShot("verify_v403_vd_docked_pill.png")

            // Sub-test F: Real tap on docked pill ("恢复虚拟屏") restores active preview
            tapOverlayView("恢复虚拟屏")
            assertTrue(
                "Docked pill did not restore preview on tap",
                waitUntil(3_000) {
                    !FloatingVirtualDisplay.minimizedToPill && FloatingVirtualDisplay.isAnyPreviewShowing()
                }
            )

            // Landscape aspect change check while active
            val landscapeState = portraitState.copy(imageInfo = """{"width":1200,"height":540}""")
            RootPhoneHarness.state.value = landscapeState
            FloatingVirtualDisplay.onWorkspaceStateChanged(activity, landscapeState)
            SystemClock.sleep(250)
            RootPhoneHarness.state.value = portraitState
            FloatingVirtualDisplay.onWorkspaceStateChanged(activity, portraitState)
            SystemClock.sleep(200)

            // Sub-test G: PowerSaveController state verification
            PowerSaveController.setManualPowerSave(ctx, true)
            assertTrue("PowerSaveController did not report active", PowerSaveController.isPowerSaveActive(ctx))
            PowerSaveController.setManualPowerSave(ctx, initialPowerSave)

            FloatingVirtualDisplay.hide()
            RootPhoneHarness.state.value = RootPhoneState()
            assertTrue(
                "Overlay remained visible after hide()",
                waitUntil(3_000) { !FloatingVirtualDisplay.overlayVisible.value && !FloatingVirtualDisplay.isAnyPreviewShowing() }
            )
            assertFalse(FloatingVirtualDisplay.overlayVisible.value)
        } finally {
            shell("settings delete global hidden_api_policy")
            PowerSaveController.setManualPowerSave(ctx, initialPowerSave)
            FloatingVirtualDisplay.hide()
            RootPhoneHarness.state.value = RootPhoneState()
            shell("appops set $pkg SYSTEM_ALERT_WINDOW allow")
        }
    }
}
