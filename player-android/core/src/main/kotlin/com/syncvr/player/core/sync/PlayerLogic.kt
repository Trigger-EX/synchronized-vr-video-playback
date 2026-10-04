package com.syncvr.player.core.sync

import kotlin.math.max
import kotlin.math.min

/** Pure helpers for the ExoPlayer-backed [VideoPlayer] (app/.../ExoVideoPlayer.kt). */
object PlayerMath {
    /** Slowest and fastest speed the backend will ask the decoder for; drift correction needs far less. */
    const val MIN_RATE = 0.5
    const val MAX_RATE = 2.0

    fun clampRate(rate: Double): Double =
        if (rate.isNaN()) 1.0 else max(MIN_RATE, min(MAX_RATE, rate))

    /** Seconds to the whole milliseconds ExoPlayer takes; negative and NaN become 0. */
    fun secondsToMs(seconds: Double): Long =
        if (seconds.isNaN() || seconds <= 0.0) 0L else Math.round(seconds * 1000.0)

    fun msToSeconds(ms: Long): Double = ms / 1000.0

    /** ExoPlayer reports C.TIME_UNSET (a large negative number) for an unknown duration. */
    fun durationSeconds(durationMs: Long): Double = if (durationMs <= 0) 0.0 else durationMs / 1000.0
}

/**
 * Tracks "a seek is in flight". Cleared by [complete], or after [timeout] seconds so a seek the
 * decoder never reports back on (e.g. to the current position) cannot stall the engine.
 */
class SeekTracker(private val timeout: Double = 4.0) {
    private var startedAt: Double? = null

    fun begin(now: Double) { startedAt = now }

    fun complete() { startedAt = null }

    fun isSeeking(now: Double): Boolean {
        val s = startedAt ?: return false
        if (now - s > timeout) { startedAt = null; return false }
        return true
    }
}

/** Maps a server video name to a file in the videos folder. */
object VideoFiles {
    /** The name must be a plain file name that is present in [available]; anything else is null. */
    fun resolve(name: String?, available: Collection<String>): String? {
        if (name.isNullOrEmpty()) return null
        if (name.contains('/') || name.contains('\\') || name == "." || name == "..") return null
        return available.firstOrNull { it == name }
    }
}
