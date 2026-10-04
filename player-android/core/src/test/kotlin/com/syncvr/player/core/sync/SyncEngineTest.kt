package com.syncvr.player.core.sync

import kotlin.math.abs
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Same scenarios as headset/Tests~/EngineTests.cs and server/tests/test_sync_engine.py. */
class SyncEngineTest {
    private val frame = 1.0 / 72.0

    private fun cmd(pos: Double, at: Double, loop: Boolean = false, duration: Double = 600.0) =
        VideoCommand(video = "v.mp4", pos = pos, at = at, loop = loop, duration = duration)

    private fun run(engine: SyncEngine, player: FakePlayer, from: Double, to: Double): Double {
        var t = from
        while (t < to) {
            player.tick(t)
            engine.update(t)
            t += frame
        }
        player.tick(t)
        return t
    }

    private fun error(engine: SyncEngine, player: FakePlayer, now: Double): Double =
        player.truePosition - engine.expectedPosition(now)!!

    private fun ms(v: Double) = "%.1f".format(v * 1000)

    @Test fun scheduledStart() {
        val p = FakePlayer()
        val e = SyncEngine(p, null)
        e.onPlay(cmd(10.0, 1.5))
        val t = run(e, p, 0.0, 8.0)
        val err = error(e, p, t)
        // Residual drift below the deadband (20 ms) is deliberately left alone.
        assertTrue(e.state == "playing" && abs(err) < 0.02, "scheduled start converges: error ${ms(err)} ms")
        assertTrue(abs(e.startLatency - 0.08) < 0.03, "start latency learned: estimate ${e.startLatency * 1000} ms")
    }

    @Test fun lateJoin() {
        val p = FakePlayer(seekDuration = 0.4)
        val e = SyncEngine(p, null)
        e.onPlay(cmd(0.0, -95.0))
        val t = run(e, p, 0.0, 8.0)
        val err = error(e, p, t)
        assertTrue(e.state == "playing" && abs(err) < 0.01, "late join converges: error ${ms(err)} ms, pos ${p.truePosition}")
        assertTrue(e.seekTime >= 0.39, "seek time measured: estimate ${e.seekTime * 1000} ms")
    }

    @Test fun clockErrorCorrectedByRate() {
        val p = FakePlayer(rateError = 0.004)
        val e = SyncEngine(p, null)
        e.onPlay(cmd(0.0, 1.5))
        val t = run(e, p, 0.0, 120.0)
        val err = error(e, p, t)
        assertTrue(abs(err) < 0.025 && p.seeks <= 2, "0.4% fast decoder held in sync by rate: error ${ms(err)} ms, seeks ${p.seeks}")
    }

    @Test fun seekModeCorrects() {
        val p = FakePlayer(rateError = 0.004)
        val e = SyncEngine(p, null)
        e.settings.correctionMode = "seek"
        e.onPlay(cmd(0.0, 1.5))
        val t = run(e, p, 0.0, 120.0)
        val err = error(e, p, t)
        assertTrue(abs(err) < 0.09 && abs(e.rate - 1) < 1e-9, "seek mode keeps drift under threshold: error ${ms(err)} ms, seeks ${p.seeks}")
    }

    @Test fun pauseAndResume() {
        val p = FakePlayer()
        val e = SyncEngine(p, null)
        e.onPlay(cmd(0.0, 1.5))
        var t = run(e, p, 0.0, 6.0)
        e.onPause(cmd(7.25, t + 0.3))
        t = run(e, p, t, t + 2)
        assertTrue(e.state == "paused" && abs(p.truePosition - 7.25) < 1e-9, "pause lands on exact frame: pos ${p.truePosition}")
        e.onPlay(cmd(7.25, t + 1.5))
        t = run(e, p, t, t + 6)
        val err = error(e, p, t)
        assertTrue(e.state == "playing" && abs(err) < 0.01, "resume after pause in sync: error ${ms(err)} ms")
    }

    @Test fun loopWraps() {
        val p = FakePlayer(videoLength = 10.0, rateError = 0.002)
        val e = SyncEngine(p, null)
        e.onPlay(cmd(0.0, 1.5, loop = true, duration = 10.0))
        val t = run(e, p, 0.0, 34.3)
        val raw = error(e, p, t)
        val err = ((raw + 5) % 10 + 10) % 10 - 5
        assertTrue(e.state == "playing" && abs(err) < 0.03, "looping video stays in sync across the loop point: error ${ms(err)} ms")
    }

    @Test fun endsWithoutLoop() {
        val p = FakePlayer(videoLength = 5.0)
        val e = SyncEngine(p, null)
        e.onPlay(cmd(0.0, 1.5, duration = 5.0))
        run(e, p, 0.0, 9.0)
        assertEquals("ended", e.state, "non-looping video reports ended")
    }

    @Test fun stallFallsBackToSeek() {
        val p = FakePlayer(rateError = 0.004, freezesWhenRateChanged = true)
        val events = ArrayList<String>()
        val e = SyncEngine(p) { _, msg -> events.add(msg) }
        e.onPlay(cmd(0.0, 1.5))
        val t = run(e, p, 0.0, 60.0)
        val err = error(e, p, t)
        assertTrue(
            e.forcedMode == "seek" && events.size == 1 && abs(err) < 0.1,
            "player that freezes on speed change falls back to seeks: mode ${e.forcedMode}, error ${ms(err)} ms",
        )
    }

    @Test fun missingFile() {
        val p = FakePlayer(fileMissing = true)
        val e = SyncEngine(p, null)
        e.onPlay(cmd(0.0, 1.5))
        run(e, p, 0.0, 1.0)
        assertEquals("error", e.state, "missing video reports error")
        p.fileMissing = false
        e.onPlay(cmd(0.0, 3.0))
        val t = run(e, p, 1.0, 8.0)
        assertTrue(e.state == "playing" && abs(error(e, p, t)) < 0.02, "retry after content arrives plays: state ${e.state}")
    }

    @Test fun resyncAfterSleep() {
        val p = FakePlayer()
        val e = SyncEngine(p, null)
        e.onPlay(cmd(0.0, 1.5))
        var t = run(e, p, 0.0, 5.0)
        p.pause() // the OS paused the decoder while the app was suspended
        t = run(e, p, t, t + 10)
        e.resync()
        t = run(e, p, t, t + 5)
        val err = error(e, p, t)
        assertTrue(e.state == "playing" && abs(err) < 0.01, "resync after suspend: error ${ms(err)} ms")
    }

    @Test fun jsonNumbersAreInvariant() {
        val saved = java.util.Locale.getDefault()
        java.util.Locale.setDefault(java.util.Locale.GERMANY)
        val json = try {
            JsonWriter("status").field("x", 1.5).field("s", "a\"b\n").field("n", null as Double?).toString()
        } finally {
            java.util.Locale.setDefault(saved)
        }
        assertEquals("{\"type\":\"status\",\"x\":1.5,\"s\":\"a\\\"b\\n\",\"n\":null}", json, "JSON writer ignores locale and escapes")
    }

    // From server/tests/test_sync_engine.py (not in the C# file; ClockSync is ported).
    @Test fun clockSyncPrefersFastestRoundTrip() {
        val c = ClockSync()
        c.add(t0 = 10.0, ts = 1010.2, t1 = 10.3) // slow, asymmetric
        c.add(t0 = 20.0, ts = 1020.005, t1 = 20.01) // fast round trip wins
        assertEquals(1020.005 - 20.005, c.offset, 1e-9)
        assertEquals(0.01, c.rtt, 1e-9)
    }
}
