package com.syncvr.player.core.sync

import kotlin.math.abs

/** A decoder on a virtual clock with start/seek/load delays and a crystal error. */
class FakePlayer(
    var startLatency: Double = 0.08,
    var seekDuration: Double = 0.2,
    var loadTime: Double = 0.3,
    /** e.g. 0.003 = runs 0.3% fast */
    var rateError: Double = 0.0,
    var freezesWhenRateChanged: Boolean = false,
    var fileMissing: Boolean = false,
    var videoLength: Double = 600.0,
) : VideoPlayer {
    var now = 0.0
    var seeks = 0

    private var pos = 0.0
    private var posAt = 0.0
    private var rate = 1.0
    private var playing = false
    private var prepared = false
    private var looping = false
    private var startAt = -1.0
    private var seekDoneAt = -1.0
    private var seekTarget = 0.0
    private var loadDoneAt = -1.0

    override var loadedVideo: String? = null
        private set
    override var error: String? = null
        private set

    override val isPrepared: Boolean get() = prepared
    override val isSeeking: Boolean get() = seekDoneAt >= 0
    override val isPlaying: Boolean get() = playing
    override val length: Double get() = if (prepared) videoLength else 0.0
    override val canSetRate: Boolean get() = true
    override val time: Double get() { advance(); return pos }
    val truePosition: Double get() { advance(); return pos }

    private fun advance() {
        if (playing && seekDoneAt < 0 && prepared) {
            val speed = if (freezesWhenRateChanged && abs(rate - 1) > 1e-9) 0.0 else rate * (1 + rateError)
            pos += (now - posAt) * speed
            if (pos >= videoLength) {
                if (looping) {
                    pos %= videoLength
                } else {
                    pos = videoLength
                    playing = false
                }
            }
        }
        posAt = now
    }

    fun tick(now: Double) {
        this.now = now
        advance()
        if (loadDoneAt >= 0 && now >= loadDoneAt) { prepared = true; loadDoneAt = -1.0 }
        if (seekDoneAt >= 0 && now >= seekDoneAt) { pos = seekTarget; seekDoneAt = -1.0 }
        if (startAt >= 0 && now >= startAt) { playing = true; startAt = -1.0 }
    }

    override fun load(cmd: VideoCommand) {
        stop()
        if (fileMissing) { error = "video not on this headset"; return }
        loadedVideo = cmd.video
        loadDoneAt = now + loadTime
    }

    override fun play() { advance(); if (prepared) startAt = now + startLatency }
    override fun pause() { advance(); playing = false; startAt = -1.0 }
    override fun stop() {
        prepared = false
        playing = false
        startAt = -1.0
        seekDoneAt = -1.0
        loadDoneAt = -1.0
        pos = 0.0
        loadedVideo = null
        error = null
    }
    override fun seek(seconds: Double) { advance(); seeks++; seekTarget = seconds; seekDoneAt = now + seekDuration }
    override fun setRate(rate: Double) { advance(); this.rate = rate }
    override fun setLooping(loop: Boolean) { looping = loop }
    override fun setExternalTime(seconds: Double) {}
    override fun clearExternalTime() {}
}
