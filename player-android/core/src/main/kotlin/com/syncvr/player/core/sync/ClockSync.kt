package com.syncvr.player.core.sync

/** Monotonic local clock in seconds. */
object LocalClock {
    val now: Double get() = System.nanoTime() / 1e9
}

/**
 * NTP-style estimate of the offset between the local clock and the server clock.
 * Port of ClockSync.cs (itself a port of ClockSync in server/syncvr/sync_engine.py).
 * Each ping records local send time t0, server time ts and local receive time t1; the sample
 * with the smallest round trip is the least disturbed by queuing delay, so its offset
 * ts - (t0 + t1) / 2 is used.
 * Thread-safe: samples are added on the network thread.
 */
class ClockSync {
    private val samples = ArrayDeque<DoubleArray>()
    private val gate = Any()
    private var offsetValue = 0.0
    private var rttValue = 0.0
    private var syncedValue = false

    val synced: Boolean get() = synchronized(gate) { syncedValue }
    val offset: Double get() = synchronized(gate) { offsetValue }
    val rtt: Double get() = synchronized(gate) { rttValue }
    val sampleCount: Int get() = synchronized(gate) { samples.size }

    fun add(t0: Double, ts: Double, t1: Double) {
        val sampleRtt = t1 - t0
        if (sampleRtt < 0) return
        synchronized(gate) {
            samples.addLast(doubleArrayOf(sampleRtt, ts - (t0 + t1) / 2.0))
            while (samples.size > WINDOW) samples.removeFirst()
            var bestRtt = Double.MAX_VALUE
            for (s in samples) {
                if (s[0] < bestRtt) {
                    bestRtt = s[0]
                    offsetValue = s[1]
                }
            }
            rttValue = bestRtt
            syncedValue = true
        }
    }

    fun reset() {
        synchronized(gate) {
            samples.clear()
            syncedValue = false
            offsetValue = 0.0
            rttValue = 0.0
        }
    }

    fun serverTime(local: Double): Double = synchronized(gate) { local + offsetValue }

    private companion object {
        const val WINDOW = 20
    }
}
