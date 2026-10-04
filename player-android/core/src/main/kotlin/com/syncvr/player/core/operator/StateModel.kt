package com.syncvr.player.core.operator

import com.syncvr.player.core.sync.Json
import kotlin.math.max
import kotlin.math.min

/** Playback status a headset reports (the `status` object of a device in /api/state). */
data class DeviceStatus(
    val state: String = "",
    val video: String? = null,
    val position: Double? = null,
    val duration: Double? = null,
    val driftMs: Double? = null,
    val battery: Double? = null,
    val error: String? = null,
    val downloadName: String? = null,
    val downloadFraction: Double? = null,
)

data class DeviceDesired(val mode: String, val video: String?, val pos: Double, val at: Double, val duration: Double, val loop: Boolean)

data class OperatorDevice(
    val id: String,
    val label: String,
    val name: String,
    val group: String,
    val online: Boolean,
    val volume: Double,
    val lastSeen: Double,
    val status: DeviceStatus,
    val inventory: Map<String, Long>,
    val desired: DeviceDesired?,
) {
    /** Short state for lists: "offline", or what the headset reports. */
    val stateText: String get() = if (!online) "offline" else status.state.ifEmpty { "connecting" }
}

data class LibraryVideo(val name: String, val title: String, val size: Long, val duration: Double?) {
    val displayName: String get() = title.ifEmpty { name }
}

data class OperatorEvent(val time: Double, val level: String, val message: String, val device: String?)

data class DownloadState(val active: List<String>, val queued: List<String>) {
    val busy: Boolean get() = active.isNotEmpty() || queued.isNotEmpty()
}

/** One /api/state reply. */
data class Snapshot(
    val serverName: String,
    val serverTime: Double,
    val devices: List<OperatorDevice>,
    val library: List<LibraryVideo>,
    val downloads: DownloadState,
    val events: List<OperatorEvent>,
) {
    fun device(id: String): OperatorDevice? = devices.firstOrNull { it.id == id }

    /** How many online devices hold the complete file of [video]. */
    fun haveCount(video: LibraryVideo): Int = devices.count { it.inventory[video.name] == video.size }

    companion object {
        fun parse(text: String): Snapshot? {
            val o = Json.parseObject(text) ?: return null
            val server = o.obj("server") ?: return null
            return Snapshot(
                serverName = server.str("name") ?: "SyncVR",
                serverTime = server.dbl("time"),
                devices = o.objects("devices").map(::parseDevice),
                library = o.objects("library").map {
                    LibraryVideo(it.str("name") ?: "", it.str("title") ?: "", it.dbl("size").toLong(), it.dblOrNull("duration"))
                }.filter { it.name.isNotEmpty() },
                downloads = o.obj("downloads").let { d ->
                    DownloadState(d?.strings("active") ?: emptyList(), d?.strings("queued") ?: emptyList())
                },
                events = o.objects("events").map {
                    OperatorEvent(it.dbl("t"), it.str("level") ?: "info", it.str("message") ?: "", it.str("device"))
                },
            )
        }

        private fun parseDevice(d: Map<String, Any?>): OperatorDevice {
            val st = d.obj("status") ?: emptyMap()
            val dl = st.obj("download")
            val total = dl?.dbl("total") ?: 0.0
            val inv = HashMap<String, Long>()
            d.obj("inventory")?.forEach { (k, v) -> (v as? Double)?.let { inv[k] = it.toLong() } }
            val des = d.obj("desired")?.let {
                DeviceDesired(
                    it.str("mode") ?: "stopped", it.str("video"), it.dbl("pos"), it.dbl("at"),
                    it.dbl("duration"), (it["loop"] as? Boolean) ?: false,
                )
            }
            val id = d.str("id") ?: ""
            return OperatorDevice(
                id = id,
                label = d.str("label")?.takeIf { it.isNotEmpty() } ?: id,
                name = d.str("name") ?: "",
                group = d.str("group") ?: "",
                online = (d["online"] as? Boolean) ?: false,
                volume = d.dbl("volume", 1.0),
                lastSeen = d.dbl("last_seen"),
                status = DeviceStatus(
                    state = st.str("state") ?: "",
                    video = st.str("video"),
                    position = st.dblOrNull("position"),
                    duration = st.dblOrNull("duration"),
                    driftMs = st.dblOrNull("drift_ms"),
                    battery = st.dblOrNull("battery")?.takeIf { it >= 0 },
                    error = st.str("error")?.takeIf { it.isNotEmpty() },
                    downloadName = dl?.str("name"),
                    downloadFraction = if (dl != null && total > 0) min(1.0, max(0.0, dl.dbl("received") / total)) else null,
                ),
                inventory = inv,
                desired = des,
            )
        }

        private fun Map<String, Any?>.str(k: String): String? = this[k] as? String
        private fun Map<String, Any?>.dbl(k: String, default: Double = 0.0): Double = (this[k] as? Double) ?: default
        private fun Map<String, Any?>.dblOrNull(k: String): Double? = this[k] as? Double

        @Suppress("UNCHECKED_CAST")
        private fun Map<String, Any?>.obj(k: String): Map<String, Any?>? = this[k] as? Map<String, Any?>

        @Suppress("UNCHECKED_CAST")
        private fun Map<String, Any?>.objects(k: String): List<Map<String, Any?>> =
            (this[k] as? List<*>)?.mapNotNull { it as? Map<String, Any?> } ?: emptyList()

        private fun Map<String, Any?>.strings(k: String): List<String> =
            (this[k] as? List<*>)?.mapNotNull { it as? String } ?: emptyList()
    }
}

fun formatTime(seconds: Double?): String {
    if (seconds == null || !seconds.isFinite()) return "-"
    val s = max(0, seconds.toInt())
    val h = s / 3600
    val m = (s % 3600) / 60
    val sec = s % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, sec) else "%d:%02d".format(m, sec)
}

fun formatBytes(n: Long): String {
    val units = arrayOf("B", "KB", "MB", "GB", "TB")
    var v = n.toDouble()
    var i = 0
    while (v >= 1024 && i < units.size - 1) { v /= 1024; i++ }
    return if (i == 0) "$n B" else "%.1f %s".format(v, units[i])
}
