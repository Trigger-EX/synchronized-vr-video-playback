package com.syncvr.player.core

/** Pure timing logic for the mode cycle: which mode is active after [elapsedMs]. */
class ModeCycle(val intervalMs: Long = DEFAULT_INTERVAL_MS) {
    init {
        require(intervalMs > 0) { "intervalMs must be positive" }
    }

    fun modeAt(elapsedMs: Long): DisplayMode {
        val steps = (maxOf(elapsedMs, 0L) / intervalMs).toInt()
        return DisplayMode.fromIndex(steps)
    }

    fun next(current: DisplayMode): DisplayMode = DisplayMode.fromIndex(current.ordinal + 1)

    companion object {
        const val DEFAULT_INTERVAL_MS = 15_000L
    }
}
