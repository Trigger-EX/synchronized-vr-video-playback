package com.syncvr.player.core.sync

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/** What the sync engine needs from a video player. */
interface VideoPlayer {
    val loadedVideo: String?
    val isPrepared: Boolean
    val isSeeking: Boolean
    val isPlaying: Boolean
    val error: String?
    val time: Double
    val length: Double
    val canSetRate: Boolean
    fun load(cmd: VideoCommand)
    fun play()
    fun pause()
    fun stop()
    fun seek(seconds: Double)
    fun setRate(rate: Double)
    fun setLooping(loop: Boolean)
    fun setExternalTime(seconds: Double)
    fun clearExternalTime()
}

/**
 * Keeps a video player on the server's schedule. This is a line-by-line port of SyncEngine.cs,
 * which in turn ports server/syncvr/sync_engine.py (the reference implementation the simulator
 * and tests exercise); keep them in step.
 *
 * The server sends anchors: "video V is at position P at server time T". Starting and hard
 * re-syncs are done by cueing: pick a moment C far enough ahead for a seek to finish, pause,
 * seek to where the video must be at C, and call play() just before C (early by the learned
 * start-up latency). While playing, small drift is absorbed by nudging playback speed and large
 * drift triggers a new cue.
 */
class SyncEngine(
    private val player: VideoPlayer,
    emitEvent: ((level: String, message: String) -> Unit)? = null,
) {
    private val emitEvent: (String, String) -> Unit = emitEvent ?: { _, _ -> }

    var settings = SyncSettings()
    /** idle|loading|ready|playing|paused|ended|error */
    var state = "idle"
    var anchor: VideoCommand? = null
    var pauseRequest: VideoCommand? = null
    var videoMsg: VideoCommand? = null
    var pendingStart = false
    var drift: Double? = null
    var rate = 1.0
    /** Learned: how long a seek takes. */
    var seekTime = 0.3
    /** Learned: delay between play() and frames moving. */
    var startLatency = 0.1
    var forcedMode: String? = null

    private var cueAt: Double? = null
    private var seekStarted: Double? = null
    private var settleUntil = 0.0
    private var lastSeek = Double.NEGATIVE_INFINITY
    private var learning = false
    private val learnSamples = ArrayList<Double>()
    private var lastProgressPos: Double? = null
    private var lastProgressT = 0.0

    // ------------------------------------------------------------ commands

    private fun ensureLoaded(msg: VideoCommand) {
        if (player.loadedVideo != msg.video) {
            player.load(msg)
            state = "loading"
            drift = null
        }
        videoMsg = msg
    }

    fun onPlay(msg: VideoCommand) {
        val a = anchor
        val same = a != null && a.video == msg.video &&
            abs(a.pos - msg.pos) < 1e-3 && abs(a.at - msg.at) < 1e-3
        ensureLoaded(msg)
        anchor = msg
        pauseRequest = null
        if (same && state == "playing") return // resync of the anchor we already follow
        pendingStart = true
        cueAt = null
    }

    fun onPause(msg: VideoCommand) {
        ensureLoaded(msg)
        pauseRequest = msg
        if (state != "playing") {
            anchor = null
            pendingStart = false
        }
    }

    fun onStop() {
        player.stop()
        anchor = null
        pauseRequest = null
        videoMsg = null
        pendingStart = false
        seekStarted = null
        state = "idle"
        applyRate(1.0)
    }

    fun onSettings(settings: SyncSettings?) {
        if (settings != null) this.settings = settings
        forcedMode = null
    }

    /** Re-cue the current anchor, e.g. after the app was suspended. */
    fun resync() {
        if (anchor != null && (state == "playing" || state == "ready")) {
            pendingStart = true
            cueAt = null
        }
    }

    // -------------------------------------------------------------- update

    fun expectedPosition(now: Double): Double? {
        val a = anchor ?: return null
        var pos = a.pos + (now - a.at)
        val length = length()
        if (length > 0 && a.loop) pos = mod(pos, length)
        return pos
    }

    fun length(): Double {
        if (player.length > 0) return player.length
        val a = anchor
        if (a != null && a.duration > 0) return a.duration
        return 0.0
    }

    private fun mod(a: Double, b: Double): Double {
        val r = a % b
        return if (r < 0) r + b else r
    }

    private fun applyRate(rate: Double) {
        if (abs(rate - this.rate) > 1e-4) {
            this.rate = rate
            player.setRate(rate)
        }
    }

    private fun settle(now: Double, learn: Boolean) {
        settleUntil = now + settings.settleMs / 1000.0
        drift = null
        learning = learn
        learnSamples.clear()
        lastProgressPos = null
    }

    private fun seek(now: Double, pos: Double) {
        player.seek(pos)
        seekStarted = now
    }

    private fun trackSeek(now: Double) {
        val started = seekStarted
        if (started != null && !player.isSeeking) {
            val took = now - started
            seekStarted = null
            // Rise immediately, decay slowly: better to cue a little too early.
            seekTime = if (took > seekTime) took else 0.9 * seekTime + 0.1 * took
        }
    }

    fun cueMargin(): Double = min(5.0, 1.5 * seekTime + 0.15)

    /** Call once per rendered frame with the current server time. */
    fun update(now: Double) {
        trackSeek(now)
        if (!player.error.isNullOrEmpty()) {
            state = "error"
            return
        }
        if (state == "loading") {
            if (!player.isPrepared) return
            state = "paused"
            val vm = videoMsg
            if (anchor == null && pauseRequest == null && vm != null) pauseRequest = vm.with(0.0, now)
        }

        val req = pauseRequest
        if (req != null && now >= req.at) {
            pauseRequest = null
            applyRate(1.0)
            player.pause()
            seek(now, req.pos)
            anchor = null
            pendingStart = false
            state = "paused"
            return
        }

        if (anchor == null) return

        if (pendingStart) {
            start(now)
            return
        }

        if (state == "playing") correct(now)
    }

    private fun start(now: Double) {
        val anchor = this.anchor!!
        var cue = cueAt
        if (cue == null) {
            var at = anchor.at
            if (at - now < cueMargin()) at = now + cueMargin() // late: pick a reachable point further on
            cue = at
            cueAt = at
            applyRate(1.0)
            player.setLooping(anchor.loop)
            player.pause()
            seek(now, wrap(expectedPosition(at)!!))
            if (state != "playing") state = "ready"
        }
        if (player.isSeeking || now < cue - startLatency) return
        if (now > cue + LATE_START_TOLERANCE) {
            cueAt = null // the seek took too long; cue again further ahead
            return
        }
        player.play()
        pendingStart = false
        cueAt = null
        state = "playing"
        settle(now, true)
    }

    private fun wrap(pos: Double): Double {
        val length = length()
        val a = anchor
        if (length > 0 && a != null && a.loop) return mod(pos, length)
        if (length > 0) return max(0.0, min(pos, length - 0.05))
        return max(0.0, pos)
    }

    private fun correct(now: Double) {
        if (player.isSeeking || now < settleUntil) return
        val anchor = this.anchor!!
        val expected = expectedPosition(now)!!
        val length = length()
        if (length > 0 && !anchor.loop && expected >= length - 0.25) {
            if (expected >= length) state = "ended"
            return
        }
        val actual = player.time
        if (stalled(now, actual)) return
        var raw = actual - expected
        if (length > 0 && anchor.loop) raw = mod(raw + length / 2.0, length) - length / 2.0
        val prevDrift = drift
        val smoothed = if (prevDrift != null) prevDrift + DRIFT_SMOOTHING * (raw - prevDrift) else raw
        drift = smoothed

        if (learning) {
            // Right after a cued start, drift is exactly how wrong our
            // start-latency estimate was (negative = started late).
            learnSamples.add(raw)
            if (learnSamples.size >= LEARN_SAMPLES) {
                learnSamples.sort()
                val err = learnSamples[learnSamples.size / 2]
                startLatency = min(1.0, max(0.0, startLatency - LEARN_GAIN * err))
                learning = false
            }
        }

        val s = settings
        var mode = forcedMode ?: s.correctionMode
        if (mode == "rate" && !player.canSetRate) mode = "seek"
        val d = smoothed
        val threshold = if (mode == "seek") s.seekModeThresholdMs else s.hardSeekMs
        if (abs(d) > threshold / 1000.0) {
            if (now - lastSeek >= s.seekCooldownMs / 1000.0) {
                lastSeek = now
                pendingStart = true
                cueAt = null
            }
            return
        }
        if (mode == "rate") {
            val deadband = s.deadbandMs / 1000.0
            if (abs(d) > deadband) {
                val adjust = max(-s.maxRateAdjust, min(s.maxRateAdjust, s.rateGain * d))
                applyRate(1.0 - adjust)
            } else if (abs(d) < deadband * RATE_RELEASE) {
                applyRate(1.0)
            }
        }
        if (mode == "external") player.setExternalTime(expected) else player.clearExternalTime()
    }

    /** Detect players that freeze when their speed is changed. */
    private fun stalled(now: Double, actual: Double): Boolean {
        val last = lastProgressPos
        if (last == null || abs(actual - last) > 1e-6) {
            lastProgressPos = actual
            lastProgressT = now
            return false
        }
        if (rate != 1.0 && now - lastProgressT > STALL_TIMEOUT) {
            forcedMode = "seek"
            applyRate(1.0)
            emitEvent("warn", "video stalled while changing speed; using seek-only correction")
            settle(now, false)
            return true
        }
        return false
    }

    val reportedState: String get() = if (state == "playing" && pendingStart) "syncing" else state

    /** Adds the engine's fields to a status message. */
    fun writeStatus(w: JsonWriter, now: Double) {
        val expected = expectedPosition(now)
        val length = length()
        val d = drift
        w.field("state", reportedState)
            .field("video", videoMsg?.video)
            .field("position", if (player.loadedVideo != null) round(player.time, 3) else null)
            .field("expected", if (expected != null && state == "playing") round(expected, 3) else null)
            .field("duration", if (length > 0) round(length, 3) else null)
            .field("drift_ms", if (d != null) round(d * 1000.0, 1) else null)
            .field("rate", round(rate, 4))
            .field("mode", forcedMode ?: settings.correctionMode)
            .field("seek_time_ms", round(seekTime * 1000.0, 0))
            .field("start_latency_ms", round(startLatency * 1000.0, 0))
        val a = anchor
        if (a != null) {
            w.raw("anchor", "{\"pos\":${round(a.pos, 3)},\"at\":${round(a.at, 3)},\"loop\":${a.loop}}")
        }
    }

    /** Like .NET Math.Round(value, digits): scale, round half to even, unscale. */
    private fun round(value: Double, digits: Int): Double {
        val scale = Math.pow(10.0, digits.toDouble())
        return Math.rint(value * scale) / scale
    }

    private companion object {
        const val DRIFT_SMOOTHING = 0.1
        const val LEARN_SAMPLES = 6
        const val LEARN_GAIN = 0.7
        const val RATE_RELEASE = 0.2
        const val LATE_START_TOLERANCE = 0.05
        // Must stay well below the time drift needs to reach hard_seek_ms, or a
        // re-cue (which resets the speed) would hide the stall.
        const val STALL_TIMEOUT = 0.25
    }
}
