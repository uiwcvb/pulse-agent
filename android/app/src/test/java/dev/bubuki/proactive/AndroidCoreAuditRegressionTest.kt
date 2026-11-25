package dev.bubuki.proactive

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import dev.bubuki.proactive.markdown.MarkdownModel
import dev.bubuki.proactive.markdown.MdBlock
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.net.InetAddress

class AndroidCoreAuditRegressionTest {

    private fun testMsg(
        id: String,
        created: Double,
        orderSeq: Long = 0L,
        seq: Long = 0L,
    ) = PulseMessage(
        id = id,
        botId = "companion",
        role = if (orderSeq > 0L) "assistant" else "user",
        text = id,
        kind = "chat",
        engine = "dsh",
        created = created,
        decision = "",
        trace = "[]",
        optimistic = orderSeq <= 0L,
        seq = seq,
        orderSeq = orderSeq,
    )

    @Test
    fun sortPulseMessages_isStrictlyTransitiveUnderClockSkewAndMixedOrderSeq() {
        val mixed = mutableListOf<PulseMessage>()
        // Server messages with monotonically increasing orderSeq even when timestamps jitter backwards
        mixed += testMsg("s3", created = 1001.0, orderSeq = 30L)
        mixed += testMsg("s1", created = 1005.0, orderSeq = 10L)
        mixed += testMsg("s2", created = 999.0, orderSeq = 20L)
        // Local/optimistic messages with orderSeq = 0 interleaved chronologically
        mixed += testMsg("loc_early", created = 995.0, orderSeq = 0L, seq = 1L)
        mixed += testMsg("loc_mid", created = 1005.0, orderSeq = 0L, seq = 2L)
        mixed += testMsg("loc_late", created = 1010.0, orderSeq = 0L, seq = 3L)

        val sorted = sortPulseMessages(mixed)
        val serverIds = sorted.filter { it.orderSeq > 0L }.map { it.id }
        assertEquals(listOf("s1", "s2", "s3"), serverIds)
        assertEquals("loc_early", sorted.first().id)
        assertEquals("loc_late", sorted.last().id)

        // Fuzz larger skewed batch to guarantee TimSort never throws IllegalArgumentException
        val skewed = (1..60).map { i ->
            if (i % 3 == 0) testMsg("l$i", created = 2000.0 - i, orderSeq = 0L, seq = i.toLong())
            else testMsg("r$i", created = 2000.0 + ((i * 7) % 13) - 6.0, orderSeq = i.toLong())
        }.reversed()
        val res = sortPulseMessages(skewed)
        assertEquals(60, res.size)
        val resServerSeqs = res.filter { it.orderSeq > 0L }.map { it.orderSeq }
        assertEquals(resServerSeqs.sorted(), resServerSeqs)
    }

    @Test
    fun syncEpochGuard_rejectsLowerPositiveEpochOnly() {
        assertTrue(shouldRejectStaleSyncEpoch(incomingEpoch = 4L, currentEpoch = 5L))
        assertFalse(shouldRejectStaleSyncEpoch(incomingEpoch = 5L, currentEpoch = 5L))
        assertFalse(shouldRejectStaleSyncEpoch(incomingEpoch = 6L, currentEpoch = 5L))
        assertFalse(shouldRejectStaleSyncEpoch(incomingEpoch = 0L, currentEpoch = 5L))
        assertFalse(shouldRejectStaleSyncEpoch(incomingEpoch = 4L, currentEpoch = 0L))
    }

    @Test
    fun markdownModel_handlesSingleLineHighlightRgbaColorAndSvgFences() {
        val hlBlocks = MarkdownModel.parse("前文 ==高亮词== 后文")
        val hlPara = hlBlocks.single() as MdBlock.Para
        assertEquals("前文 高亮词 后文", hlPara.text.text)
        assertTrue(
            hlPara.text.spanStyles.any {
                it.start == 3 && it.end == 6 && it.item.background == Color(0x38F59E0B)
            }
        )

        val multilineNoHl = MarkdownModel.parse("a ==line1\nline2== b")
        val multiPara = multilineNoHl.single() as MdBlock.Para
        assertFalse(multiPara.text.spanStyles.any { it.item.background == Color(0x38F59E0B) })

        val rgbaBlocks = MarkdownModel.parse("""<span style="color:#11223380">tint</span>""")
        val rgbaPara = rgbaBlocks.single() as MdBlock.Para
        assertTrue(
            rgbaPara.text.spanStyles.any { it.item.color.toArgb() == 0x80112233.toInt() }
        )

        val svg1 = MarkdownModel.parse("```image/svg+xml\n<svg/>\n```").single() as MdBlock.Code
        val svg2 = MarkdownModel.parse("```svg+xml\n<svg/>\n```").single() as MdBlock.Code
        assertEquals("svg", svg1.info)
        assertEquals("svg", svg2.info)
    }

    @Test
    fun formatting_stripsResidualTagsAndMapsAllCanonicalTools() {
        val cleaned = stripResidualControlTags(
            "你好<sticker:smile>呀<sticker code=\"wave\"/>！<sticker>ok</sticker>[sticker:cat]</think>\n\n\n\n再见<think>"
        )
        assertEquals("你好呀！\n\n再见", cleaned)

        val canonicalTools = listOf(
            "read_file", "write_file", "edit_file", "search_files",
            "web", "bash", "memory", "blackboard",
            "phone", "pc", "cloud", "reply", "subtask", "admin"
        )
        for (tool in canonicalTools) {
            val label = humanizeToolName(tool, JSONObject())
            assertFalse("Tool $tool fell through to generic fallback: $label", label == "执行 $tool")
        }
    }

    @Test
    fun shellSandbox_quotesSegmentsAndBlocksDisplayZero() {
        // Quoted strings are untouched
        assertEquals("""echo "input tap 10 20"""", sandboxShellCommandToDisplay("""echo "input tap 10 20"""", 7))
        assertEquals("""echo 'am start -n a/.B'""", sandboxShellCommandToDisplay("""echo 'am start -n a/.B'""", 7))

        // Multi-segment chains receive display flags on every matching segment
        val rewritten = sandboxShellCommandToDisplay(
            "FOO=1 input tap 10 20 && am start-activity -n a/.B; cmd activity start -n a/.C | screencap -p /sdcard/a.png",
            7
        )
        assertEquals(
            "FOO=1 input -d 7 tap 10 20 && am start-activity --display 7 -n a/.B; cmd activity start --display 7 -n a/.C | screencap -d 7 -p /sdcard/a.png",
            rewritten
        )

        // Already targeted commands stay unchanged
        assertEquals("input -d 5 keyevent 4", sandboxShellCommandToDisplay("input -d 5 keyevent 4", 7))

        // Echoed dangerous patterns are NOT false-blocked
        validateShellCommandSandbox("""echo "am start --display=0 monkey"""", waitingForeground = false, currentDisp = 7)

        // Unquoted Display-0 and system-wide mutations are blocked
        val forbidden = listOf(
            "am start-activity --display=0 -n a/.B",
            "input -d0 tap 10 20",
            "input -d 0 tap 10 20",
            "monkey -p com.example 1",
            "settings --user 0 put secure ui_night_mode 2",
            "wm size 720x1600",
        )
        for (bad in forbidden) {
            try {
                validateShellCommandSandbox(bad, waitingForeground = false, currentDisp = 7)
                fail("Expected sandbox block for: $bad")
            } catch (_: Exception) {
                // Expected
            }
        }
        // Display-scoped wm modification is permitted
        validateShellCommandSandbox("wm size 720x1600 -d 7", waitingForeground = false, currentDisp = 7)
    }

    @Test
    fun wirelessAdb_acceptsLoopbackAndRejectsRemoteLanAddress() {
        assertTrue(WirelessAdbController.isLocalDeviceAddress(InetAddress.getByName("127.0.0.1")))
        assertTrue(WirelessAdbController.isLocalDeviceAddress(InetAddress.getByName("::1")))
        assertFalse(WirelessAdbController.isLocalDeviceAddress(null))
        assertFalse(WirelessAdbController.isLocalDeviceAddress(InetAddress.getByName("192.0.2.55")))
    }

    @Test
    fun apkUpdater_comparesVersionsSemanticallyWithoutDowngrade() {
        assertTrue(isRemoteVersionNewer("4.0.24", 112, "4.0.25", 113))
        assertFalse(isRemoteVersionNewer("4.0.24", 112, "4.0.24", 112))
        assertFalse(isRemoteVersionNewer("4.0.24", 112, "4.0.23", 111))
        assertTrue(isRemoteVersionNewer("4.0.24", 0, "v4.0.25", 0))
        assertFalse(isRemoteVersionNewer("4.0.24", 0, "v4.0.24", 0))
        assertFalse(isRemoteVersionNewer("4.0.24", 0, "4.0.23", 0))
    }
}
