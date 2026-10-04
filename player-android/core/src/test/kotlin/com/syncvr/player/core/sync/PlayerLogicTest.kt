package com.syncvr.player.core.sync

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class PlayerLogicTest {
    @Test fun rateIsClamped() {
        assertEquals(1.0, PlayerMath.clampRate(1.0))
        assertEquals(0.5, PlayerMath.clampRate(0.0))
        assertEquals(2.0, PlayerMath.clampRate(9.0))
        assertEquals(1.0, PlayerMath.clampRate(Double.NaN))
        assertEquals(1.003, PlayerMath.clampRate(1.003))
    }

    @Test fun timeConversions() {
        assertEquals(1235L, PlayerMath.secondsToMs(1.2346))
        assertEquals(0L, PlayerMath.secondsToMs(-3.0))
        assertEquals(0L, PlayerMath.secondsToMs(Double.NaN))
        assertEquals(2.5, PlayerMath.msToSeconds(2500))
        assertEquals(0.0, PlayerMath.durationSeconds(Long.MIN_VALUE + 1))
        assertEquals(60.0, PlayerMath.durationSeconds(60_000))
    }

    @Test fun seekTrackerCompletesAndTimesOut() {
        val t = SeekTracker(timeout = 4.0)
        assertFalse(t.isSeeking(0.0))
        t.begin(10.0)
        assertTrue(t.isSeeking(12.0))
        t.complete()
        assertFalse(t.isSeeking(12.1))
        t.begin(20.0)
        assertTrue(t.isSeeking(23.9))
        assertFalse(t.isSeeking(24.1))
        assertFalse(t.isSeeking(20.5)) // timeout latched
    }

    @Test fun resolveRejectsTraversalAndMissing() {
        val files = listOf("a.mp4", "b.mkv")
        assertEquals("a.mp4", VideoFiles.resolve("a.mp4", files))
        assertNull(VideoFiles.resolve("c.mp4", files))
        assertNull(VideoFiles.resolve("../a.mp4", files))
        assertNull(VideoFiles.resolve("x/a.mp4", files))
        assertNull(VideoFiles.resolve("..", files))
        assertNull(VideoFiles.resolve(null, files))
        assertNull(VideoFiles.resolve("", files))
    }
}
