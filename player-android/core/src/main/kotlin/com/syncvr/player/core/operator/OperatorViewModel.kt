package com.syncvr.player.core.operator

import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.max
import kotlin.math.min

/** What the dashboard screen shows. Immutable; a new one is published on every change. */
data class OperatorUiState(
    val snapshot: Snapshot? = null,
    /** Last refresh failed (null when the last one worked). */
    val connectionError: String? = null,
    val unauthorized: Boolean = false,
    /** Selected device ids; empty means every device. */
    val selected: Set<String> = emptySet(),
    val selectedVideo: String? = null,
    /** One-shot message for a toast (command result or failure). */
    val notice: String? = null,
    val noticeIsError: Boolean = false,
) {
    val connected: Boolean get() = snapshot != null && connectionError == null

    /** Devices a command would reach. */
    val targetDevices: List<OperatorDevice>
        get() = snapshot?.devices?.filter { selected.isEmpty() || it.id in selected } ?: emptyList()

    val targets: Targets get() = if (selected.isEmpty()) Targets.All else Targets.Ids(selected.sorted())

    /** The online target that drives the seek bar: the first one with a known duration. */
    val focus: OperatorDevice?
        get() = targetDevices.firstOrNull { it.online && (it.status.duration ?: 0.0) > 0 && it.status.position != null }

    val seekPosition: Double get() = focus?.status?.position ?: 0.0
    val seekDuration: Double get() = focus?.status?.duration ?: 0.0

    val onlineCount: Int get() = snapshot?.devices?.count { it.online } ?: 0
    val playingCount: Int get() = snapshot?.devices?.count { it.online && it.status.state == "playing" } ?: 0

    /** Video to load/play: the operator's choice, else what the focus device has, else the first in the library. */
    val effectiveVideo: String?
        get() {
            val lib = snapshot?.library ?: return null
            selectedVideo?.takeIf { v -> lib.any { it.name == v } }?.let { return it }
            targetDevices.firstNotNullOfOrNull { it.desired?.video }?.takeIf { v -> lib.any { it.name == v } }?.let { return it }
            return lib.firstOrNull()?.name
        }
}

/**
 * Operator logic without any Android types: polls the server, tracks the selection, and turns
 * button presses into API commands. [runner] executes work off the UI thread (a single-thread
 * executor in the app, inline in tests); [onChange] is called from whichever thread ran the work.
 */
class OperatorViewModel(
    private val api: OperatorApi,
    private val runner: (() -> Unit) -> Unit,
    private val onChange: (OperatorUiState) -> Unit,
) {
    @Volatile var state = OperatorUiState()
        private set

    private val lock = Any()

    private val refreshing = AtomicBoolean(false)

    /** Fetches /api/state once. Safe to call every second: a refresh is skipped while one is still running. */
    fun refresh() {
        if (!refreshing.compareAndSet(false, true)) return
        runner {
            try {
                refreshNow()
            } finally {
                refreshing.set(false)
            }
        }
    }

    internal fun refreshNow() {
        try {
            val snap = api.state()
            update { s ->
                s.copy(
                    snapshot = snap, connectionError = null, unauthorized = false,
                    selected = s.selected.filter { id -> snap.device(id) != null }.toSet(),
                )
            }
        } catch (e: ApiException) {
            update { it.copy(connectionError = e.message ?: "server error", unauthorized = e.unauthorized) }
        } catch (e: Exception) {
            update { it.copy(connectionError = e.message ?: "cannot reach the server", unauthorized = false) }
        }
    }

    // ---------------------------------------------------------- selection

    fun toggleSelected(id: String) = update { s ->
        s.copy(selected = if (id in s.selected) s.selected - id else s.selected + id)
    }

    fun clearSelection() = update { it.copy(selected = emptySet()) }

    fun selectOnline() = update { s -> s.copy(selected = s.snapshot?.devices?.filter { it.online }?.map { it.id }?.toSet() ?: emptySet()) }

    fun selectVideo(name: String?) = update { it.copy(selectedVideo = name) }

    fun consumeNotice() {
        if (state.notice != null) update(notify = false) { it.copy(notice = null) }
    }

    // ----------------------------------------------------------- transport

    fun play() = send("play")
    fun pause() = send("pause")
    fun stop() = send("stop")
    fun resync() = send("resync")
    fun identify() = send("identify")
    fun recenter() = send("recenter")

    fun seekTo(seconds: Double) {
        val dur = state.seekDuration
        val pos = if (dur > 0) min(max(0.0, seconds), dur) else max(0.0, seconds)
        send("seek", mapOf("pos" to pos))
    }

    fun seekBy(deltaSeconds: Double) = send("seek", mapOf("delta" to deltaSeconds))

    /** [fraction] 0..1. */
    fun setVolume(fraction: Double) = send("volume", mapOf("value" to min(1.0, max(0.0, fraction))))

    /** Loads [video] paused at the start on the targets. */
    fun load(video: String? = state.effectiveVideo) = withVideo(video, "load")

    /** Plays [video] on the targets (a synchronized start). */
    fun playVideo(video: String? = state.effectiveVideo) = withVideo(video, "play")

    private fun withVideo(video: String?, action: String) {
        if (video == null) return fail("Choose a video first")
        update(notify = false) { it.copy(selectedVideo = video) }
        send(action, mapOf("video" to video))
    }

    // ------------------------------------------------------------- library

    /** Copies [videos] (default: the whole library) to the targets. */
    fun syncContent(videos: List<String>? = null, deleteOthers: Boolean = false) =
        send("sync_content", mapOf("videos" to (videos ?: "all"), "delete_others" to deleteOthers))

    fun deleteContent(videos: List<String>) {
        if (videos.isEmpty()) return fail("Choose a video first")
        send("delete_content", mapOf("videos" to videos))
    }

    fun cancelDownloads() = send("cancel_downloads")

    fun rescanLibrary() = runner {
        try {
            api.rescanLibrary()
            notice("Library rescanned", false)
            refreshNow()
        } catch (e: Exception) {
            notice(e.message ?: "rescan failed", true)
        }
    }

    // ------------------------------------------------------------- devices

    fun rename(id: String, name: String, group: String) = runner {
        try {
            api.updateDevice(id, name, group)
            refreshNow()
        } catch (e: Exception) {
            notice(e.message ?: "rename failed", true)
        }
    }

    fun forget(id: String) = runner {
        try {
            api.forgetDevice(id)
            refreshNow()
        } catch (e: Exception) {
            notice(e.message ?: "cannot forget the headset", true)
        }
    }

    // ------------------------------------------------------------ plumbing

    private fun send(action: String, params: Map<String, Any?> = emptyMap()) {
        val targets = state.targets
        runner {
            try {
                val r = api.command(action, targets, params)
                if (r.warning != null) notice(r.warning, true)
                refreshNow()
            } catch (e: Exception) {
                notice(e.message ?: "command failed", true)
            }
        }
    }

    private fun fail(text: String) = notice(text, true)

    private fun notice(text: String, error: Boolean) = update { it.copy(notice = text, noticeIsError = error) }

    private fun update(notify: Boolean = true, change: (OperatorUiState) -> OperatorUiState) {
        val next = synchronized(lock) {
            change(state).also { state = it }
        }
        if (notify) onChange(next)
    }
}
