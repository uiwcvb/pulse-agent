package dev.bubuki.proactive

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class VirtualDisplayRegressionTest {

    @Test
    fun edgeDrag_docksToSideBubbleOnlyPastEdgeThresholdOrFastFling() {
        val screenW = 1080
        val winW = 400
        val allowPx = 80

        // Dragged safely near center or normal corner margin (24px) -> snap back (0)
        assertEquals(0, PipSpringPhysics.edgeDockSideOnDragUp(200, winW, screenW, 0f, allowPx))
        assertEquals(0, PipSpringPhysics.edgeDockSideOnDragUp(24, winW, screenW, -200f, allowPx))
        assertEquals(0, PipSpringPhysics.edgeDockSideOnDragUp(656, winW, screenW, 200f, allowPx))

        // Dragged past left/right edge threshold -> dock into clinging bubble (-1 / +1)
        assertEquals(-1, PipSpringPhysics.edgeDockSideOnDragUp(-40, winW, screenW, 0f, allowPx))
        assertEquals(-1, PipSpringPhysics.edgeDockSideOnDragUp(8, winW, screenW, 0f, allowPx))
        assertEquals(1, PipSpringPhysics.edgeDockSideOnDragUp(720, winW, screenW, 0f, allowPx))
        assertEquals(1, PipSpringPhysics.edgeDockSideOnDragUp(672, winW, screenW, 0f, allowPx))

        // High horizontal fling velocity near edge -> dock (-1 / +1)
        assertEquals(-1, PipSpringPhysics.edgeDockSideOnDragUp(30, winW, screenW, -1200f, allowPx))
        assertEquals(1, PipSpringPhysics.edgeDockSideOnDragUp(650, winW, screenW, 1200f, allowPx))
    }

    @Test
    fun resizeShrink_belowMinimumWidthTriggersSidePillSuspend() {
        assertTrue(PipSpringPhysics.shouldDockPillOnResizeUp(0.25f))
        assertTrue(PipSpringPhysics.shouldDockPillOnResizeUp(0.379f))
        assertFalse(PipSpringPhysics.shouldDockPillOnResizeUp(0.38f))
        assertFalse(PipSpringPhysics.shouldDockPillOnResizeUp(0.56f))
    }

    @Test
    fun virtualDisplay_neverAutoClosesOnChatPauseOrIdleWithoutExplicitDestroy() {
        val liveJson = """{"created":true,"display_active":true,"displayId":42}"""
        val idleWithoutDestroy = """{"created":false,"display_active":false,"displayId":-1}"""
        val explicitDestroyJson = """{"created":false,"display_active":false,"explicit_destroy":true,"displayId":-1}"""

        assertFalse(shouldAutoCloseVirtualDisplay("stopping", liveJson))
        assertFalse(shouldAutoCloseVirtualDisplay("stopped", liveJson))
        assertFalse(shouldAutoCloseVirtualDisplay("idle", liveJson))
        assertFalse(shouldAutoCloseVirtualDisplay("idle", idleWithoutDestroy))
        assertFalse(shouldAutoCloseVirtualDisplay("stopping", idleWithoutDestroy))
        assertTrue(shouldAutoCloseVirtualDisplay("stopped", explicitDestroyJson))
        assertTrue(shouldAutoCloseVirtualDisplay("idle", explicitDestroyJson))
    }

    @Test
    fun messageDedup_neverCollapsesDistinctServerMessageIds() {
        val m1 = PulseMessage("srv-101", "companion", "assistant", "收到。", "chat", "dsh", 1000.0, "", "[]", orderSeq = 10)
        val m2 = PulseMessage("srv-102", "companion", "assistant", "收到。", "chat", "dsh", 1005.0, "", "[]", orderSeq = 11)
        val distinct = deduplicateAssistantList(listOf(m1, m2))
        assertEquals(2, distinct.size)
        assertEquals("srv-101", distinct[0].id)
        assertEquals("srv-102", distinct[1].id)

        val localStopped = PulseMessage("stopped-123", "companion", "assistant", "进行中…（已停止）", "chat", "small", 1000.0, "", "[]", orderSeq = 0)
        val serverStopped = PulseMessage("srv-103", "companion", "assistant", "进行中…（已停止）", "chat", "dsh", 1001.0, "", "[]", orderSeq = 12)
        val mergedStop = deduplicateAssistantList(listOf(localStopped, serverStopped))
        assertEquals(1, mergedStop.size)
        assertEquals("srv-103", mergedStop[0].id)
    }

    @Test
    fun springPhysics_convergesToTargetCorner() {
        val (cx, cy) = PipSpringPhysics.cornerPosition(
            PipSpringPhysics.Corner.BottomEnd,
            screenW = 1080,
            screenH = 2400,
            pipW = 300,
            pipH = 600,
            marginX = 24,
            topMargin = 100,
            bottomMargin = 160
        )
        assertEquals(1080 - 300 - 24, cx)
        assertEquals(2400 - 600 - 160, cy)

        val endX = PipSpringPhysics.stepCriticalSpring(100f, cx.toFloat(), 500f, 0.42f)
        assertEquals(cx.toFloat(), endX, 12f)
    }

    @Test
    fun rootPhoneState_parsesCreatedAndDestroyedDisplayContracts() {
        val destroyedJson =
            """{"created":false,"display_active":false,"allocated":false,"released":true,"destroyed":true,"explicit_destroy":true,"displayId":-1,"display_id":-1}"""
        val deadState = RootPhoneState(display = destroyedJson, phase = "idle")
        assertFalse(deadState.displayCreated)
        assertEquals(-1, deadState.displayId)

        val liveState = RootPhoneState(display = """{"created":true,"displayId":42}""", phase = "idle")
        assertTrue(liveState.displayCreated)
        assertEquals(42, liveState.displayId)
    }

    @Test
    fun displayFrameCodec_nullVirtualDisplayBuildsCleanDestroyedState() {
        val st = DisplayFrameCodec.buildStateJson(
            null,
            1080,
            2400,
            420,
            true,
            "compact",
            -1,
            "",
            false
        )
        assertFalse(st.getBoolean("created"))
        assertFalse(st.getBoolean("display_active"))
        assertFalse(st.getBoolean("allocated"))
        assertEquals(-1, st.getInt("displayId"))
        assertEquals(-1, st.getInt("display_id"))
        assertFalse(st.getBoolean("hasPromotedTask"))
    }

    @Test
    fun backgroundIdleVirtualDisplay_staysAliveWhileActivityIsStopped() {
        val st = RootPhoneState(
            phase = "idle",
            display = """{"created":true,"display_active":true,"displayId":77}"""
        )
        assertTrue(st.displayCreated)
        assertFalse(st.active)
        assertFalse(shouldAutoCloseVirtualDisplay(st.phase, st.display))
    }

    @Test
    fun displayFlags_preservesTrustedOwnFocusAcrossOemFallbackTiers() {
        val flagTrusted = 1 shl 10
        val flagOwnDisplayGroup = 1 shl 11
        val flagAlwaysUnlocked = 1 shl 12
        val flagOwnFocus = 1 shl 14
        val flagStealTopFocusDisabled = 1 shl 16

        // Tier 3: trusted=true, ownDisplayGroup=false, alwaysUnlocked=false, secure=true
        val tier3 = DisplayInputHelper.displayFlags(34, true, false, false, true)
        assertTrue((tier3 and flagTrusted) != 0)
        assertTrue((tier3 and flagOwnFocus) != 0)
        assertTrue((tier3 and flagStealTopFocusDisabled) != 0)
        assertEquals(0, tier3 and flagOwnDisplayGroup)
        assertEquals(0, tier3 and flagAlwaysUnlocked)

        // UID resolution handles root (0) and shell (2000) cleanly without Android framework
        assertEquals("root", DisplayInputHelper.resolveCallerPackage(0))
        assertEquals("com.android.shell", DisplayInputHelper.resolveCallerPackage(2000))
    }

    @Test
    fun threeStatePipThresholds_separateMiniSuspendFromEdgeBall() {
        // Bottom-right shrink below 0.38 -> visible compact mini suspend window (never hide off-screen); >= 0.38 -> stay active
        assertTrue(PipSpringPhysics.shouldDockPillOnResizeUp(0.35f))
        assertFalse(PipSpringPhysics.shouldDockPillOnResizeUp(0.44f))

        // Top grabber drag to edges -> -1 (left ball), +1 (right ball), 0 (center corner snap)
        assertEquals(-1, PipSpringPhysics.edgeDockSideOnDragUp(-30, 360, 1080, 0f, 60))
        assertEquals(1, PipSpringPhysics.edgeDockSideOnDragUp(760, 360, 1080, 0f, 60))
        assertEquals(0, PipSpringPhysics.edgeDockSideOnDragUp(320, 360, 1080, 0f, 60))
    }

    @Test
    fun singleSourceOverlayStateMachine_enforcesMutualExclusion() {
        var st = OverlayUiState()
        assertFalse(st.expanded)
        assertFalse(st.dockedAsEdgeBubble)

        // Expanded + App Picker drawer
        st = reduceOverlayTransition(st, OverlayMode.EXPANDED, OverlayDrawer.APP_PICKER, userClosed = false)
        assertTrue(st.expanded && st.pickingApp && !st.inputBarOpen && !st.dockedAsEdgeBubble)

        // Shrinking to MINI automatically clears any open drawer
        st = reduceOverlayTransition(st, OverlayMode.MINI, OverlayDrawer.APP_PICKER, userClosed = false)
        assertEquals(OverlayMode.MINI, st.mode)
        assertFalse(st.expanded || st.pickingApp || st.inputBarOpen || st.dockedAsEdgeBubble)

        // Docking to edge bubble stays mutually exclusive with expanded/picking
        st = reduceOverlayTransition(st, OverlayMode.EDGE_BUBBLE, OverlayDrawer.INPUT_BAR, userClosed = false)
        assertTrue(st.dockedAsEdgeBubble && !st.expanded && !st.inputBarOpen)

        // Initial connection health snapshot is accessible off-service
        val h = AgentForegroundService.currentHealth()
        assertEquals(0L, h.wsGeneration)
        assertEquals("STOPPED", h.phase)
    }

    @Test
    fun pulseMessage_preservesInterleavedTimelineJson() {
        val obj = org.json.JSONObject("""{
            "id": "m-tl-1",
            "bot_id": "companion",
            "role": "assistant",
            "text": "先打开看看。\n\n搞定了。",
            "type": "chat",
            "source": "phone",
            "created_at": 1700000000.0,
            "order_seq": 5,
            "timeline": [
                {"type": "text", "text": "先打开看看。"},
                {"type": "tool", "tool": "phone", "status": "done", "duration_ms": 210},
                {"type": "text", "text": "搞定了。"}
            ]
        }""")
        val parsed = PulseMessage.from(obj)
        val tlArr = org.json.JSONArray(parsed.timeline)
        assertEquals(3, tlArr.length())
        assertEquals("text", tlArr.getJSONObject(0).getString("type"))
        assertEquals("tool", tlArr.getJSONObject(1).getString("type"))
        assertEquals("phone", tlArr.getJSONObject(1).getString("tool"))
        assertEquals("text", tlArr.getJSONObject(2).getString("type"))

        val legacy = PulseMessage.from(org.json.JSONObject("""{"id": "m-legacy", "text": "hi"}"""))
        assertEquals("[]", legacy.timeline)
    }
}
