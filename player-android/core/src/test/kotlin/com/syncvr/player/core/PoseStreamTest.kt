package com.syncvr.player.core

import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class PoseStreamTest {
    @Test fun inactiveUntilRequested() {
        val s = PoseStream()
        assertFalse(s.active(0.0))
        assertFalse(s.due(0.0))
    }

    @Test fun limitsRate() {
        val s = PoseStream()
        s.request(10.0, 100.0)
        var sent = 0
        var t = 100.0
        while (t < 101.0) { if (s.due(t)) sent++; t += 0.004 }
        assertTrue(sent in 9..11, "sent $sent")
    }

    @Test fun leaseExpiresAfter15sAndRenewalExtends() {
        val s = PoseStream()
        s.request(10.0, 0.0)
        assertTrue(s.active(14.9))
        s.request(10.0, 10.0)
        assertTrue(s.active(24.9))
        assertFalse(s.active(25.0))
        assertFalse(s.due(30.0))
    }

    @Test fun zeroHzStopsAndRestartSendsImmediately() {
        val s = PoseStream()
        s.request(10.0, 0.0)
        s.request(0.0, 1.0)
        assertFalse(s.active(1.0))
        s.request(5.0, 50.0)
        assertTrue(s.due(50.0))
        assertFalse(s.due(50.1))
        assertTrue(s.due(50.2))
    }

    @Test fun clampsHzAndDoesNotBurstAfterStall() {
        val s = PoseStream()
        s.request(1000.0, 0.0)
        assertTrue(s.due(0.0))
        assertFalse(s.due(0.02))  // 30 Hz cap
        assertTrue(s.due(5.0))
        assertFalse(s.due(5.001))
    }
}
