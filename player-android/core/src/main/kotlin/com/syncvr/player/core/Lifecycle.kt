package com.syncvr.player.core

import com.syncvr.player.core.sync.SyncEngine

/** Tiny key/value store; the app backs it with SharedPreferences. */
interface CalibrationStore {
    fun getFloat(key: String, default: Float): Float
    fun putFloats(values: Map<String, Float>)
}

/**
 * Persists what the sync engine learns (start latency, seek time) between runs. Same keys as the
 * Unity app's PlayerPrefs. Values outside the sane range are ignored on load.
 */
class CalibrationPersistence(
    private val engine: SyncEngine,
    private val store: CalibrationStore,
    private val localNow: () -> Double,
    private val intervalSeconds: Double = 30.0,
) {
    private var lastSaved: Pair<Float, Float>? = null
    private var nextSave = 0.0

    fun load() {
        val latency = store.getFloat(KEY_START_LATENCY, engine.startLatency.toFloat()).toDouble()
        val seek = store.getFloat(KEY_SEEK_TIME, engine.seekTime.toFloat()).toDouble()
        if (latency in 0.0..MAX_START_LATENCY) engine.startLatency = latency
        if (seek in MIN_SEEK_TIME..MAX_SEEK_TIME) engine.seekTime = seek
        lastSaved = engine.startLatency.toFloat() to engine.seekTime.toFloat()
    }

    /** Writes the values if they changed. Returns true when something was written. */
    fun save(): Boolean {
        val current = engine.startLatency.toFloat() to engine.seekTime.toFloat()
        if (current == lastSaved) return false
        store.putFloats(mapOf(KEY_START_LATENCY to current.first, KEY_SEEK_TIME to current.second))
        lastSaved = current
        return true
    }

    /** Call about once per frame; saves at most every [intervalSeconds]. */
    fun tick() {
        val t = localNow()
        if (t < nextSave) return
        nextSave = t + intervalSeconds
        save()
    }

    companion object {
        const val KEY_START_LATENCY = "syncvr.start_latency"
        const val KEY_SEEK_TIME = "syncvr.seek_time"
        const val MAX_START_LATENCY = 1.0
        const val MIN_SEEK_TIME = 0.01
        const val MAX_SEEK_TIME = 10.0
    }
}

/**
 * What happens when the headset is taken off or put on: leave VR and pause playback on suspend
 * (after saving calibration), then on resume re-enter VR, re-measure the clock and re-cue to
 * wherever the operator's play state now puts the video.
 */
class AppLifecycle(
    private val calibration: CalibrationPersistence,
    private val leaveVr: () -> Unit,
    private val enterVr: () -> Unit,
    private val pausePlayer: () -> Unit,
    private val resyncClock: () -> Unit,
    private val resyncPlayback: () -> Unit,
) {
    var resumed = false; private set

    fun onResume() {
        resumed = true
        enterVr()
        // The monotonic clock stops while the headset sleeps: re-measure it before re-cueing.
        resyncClock()
        resyncPlayback()
    }

    fun onPause() {
        resumed = false
        calibration.save()
        pausePlayer()
        leaveVr()
    }
}
