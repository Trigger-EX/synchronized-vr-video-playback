package com.syncvr.player.core

object PanelText {
    /** m:ss, or h:mm:ss from one hour up. Negative or unknown values show as 0:00. */
    fun formatTime(ms: Long): String {
        val total = maxOf(ms, 0L) / 1000
        val h = total / 3600
        val m = (total % 3600) / 60
        val s = total % 60
        return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
    }

    /** "name  0:12 / 3:45"; duration is omitted while unknown (ExoPlayer reports negative). */
    fun videoLine(fileName: String?, positionMs: Long, durationMs: Long): String {
        if (fileName == null) return "No video"
        val dur = if (durationMs > 0) formatTime(durationMs) else "--:--"
        return "$fileName  ${formatTime(positionMs)} / $dur"
    }
}
