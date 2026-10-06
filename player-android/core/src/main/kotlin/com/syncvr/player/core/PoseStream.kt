package com.syncvr.player.core

/** Head orientation in degrees: yaw + = turned left (counter-clockwise from above), pitch + = looking up. */
data class Pose(val yaw: Double, val pitch: Double, val roll: Double)

/** Implemented by the app: the current head orientation relative to the recentered front, or null if unknown. */
fun interface PoseSource {
    fun pose(): Pose?
}

/**
 * Rate limiter with an auto-off lease for the operator's `pose_stream` request. The server renews
 * the lease about every 10 s; if the renewal stops (laptop closed, network gone) the stream ends by
 * itself after [leaseSeconds]. Pure logic, times are caller supplied seconds.
 */
class PoseStream(private val leaseSeconds: Double = 15.0, private val maxHz: Double = 30.0) {
    private var hz = 0.0
    private var leaseEnd = 0.0
    private var nextSend = 0.0

    /** Handle a `pose_stream` message: hz <= 0 stops, anything else (re)starts and renews the lease. */
    fun request(hz: Double, now: Double) {
        if (hz.isNaN() || hz <= 0.0) {
            stop()
            return
        }
        if (this.hz <= 0.0 || now >= leaseEnd) nextSend = now  // fresh start: send right away
        this.hz = minOf(hz, maxHz)
        leaseEnd = now + leaseSeconds
    }

    fun stop() {
        hz = 0.0
        leaseEnd = 0.0
    }

    fun active(now: Double): Boolean = hz > 0.0 && now < leaseEnd

    /** True when a pose should be sent now; call once per tick, it advances the schedule. */
    fun due(now: Double): Boolean {
        if (!active(now) || now < nextSend) return false
        nextSend += 1.0 / hz
        if (nextSend <= now) nextSend = now + 1.0 / hz  // no burst after a stall
        return true
    }
}
