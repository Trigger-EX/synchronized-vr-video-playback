package com.syncvr.player.core

import com.syncvr.player.core.content.ContentManager
import com.syncvr.player.core.sync.ClockSync
import com.syncvr.player.core.sync.JsonWriter
import com.syncvr.player.core.sync.LocalClock
import com.syncvr.player.core.sync.ServerMessage
import com.syncvr.player.core.sync.SyncEngine
import com.syncvr.player.core.sync.VideoCommand
import com.syncvr.player.core.sync.VideoPlayer
import java.util.concurrent.ConcurrentLinkedQueue

/** Things the controller asks the app to do that are not video playback. All have no-op defaults. */
interface PlayerHost {
    /** A play/pause command arrived (projection, stereo and rotation tell the app how to show it). */
    fun onVideoCommand(cmd: VideoCommand) {}
    fun setVolume(volume: Double) {}
    fun recenter() {}
    /** [seconds] 0 clears the message. */
    fun showMessage(text: String?, seconds: Double) {}
    fun identify(name: String, seconds: Double) {}
    fun onNames(serverName: String, deviceName: String) {}
}

/**
 * The message loop: drains the server inbox, routes each message to the sync engine, the content
 * manager or the [host], steps the engine, flushes queued replies and sends `status` once a
 * second. Port of the Update/Handle part of SyncVRApp.cs.
 *
 * Not thread-safe except [queueEvent]: call [tick] and [handle] from the thread that owns the
 * [VideoPlayer] (the main thread on Android, because ExoPlayer is bound to it).
 */
class PlayerController(
    val player: VideoPlayer,
    val content: ContentManager,
    private val clock: ClockSync,
    private val inbox: java.util.Queue<ServerMessage>,
    private val send: (String) -> Unit,
    private val connected: () -> Boolean,
    private val host: PlayerHost = object : PlayerHost {},
    private val localNow: () -> Double = { LocalClock.now },
    /** Telemetry for the status message; null fields are omitted. Called on the tick thread. */
    private val telemetry: () -> Telemetry? = { null },
    private val statusIntervalSeconds: Double = 1.0,
    /** Frames per second for the status message, or null when unknown. */
    private val fps: () -> Double? = { null },
) {
    private val events = ConcurrentLinkedQueue<String>()
    val engine = SyncEngine(player, ::queueEvent)

    var serverName = ""; private set
    var deviceName = ""; private set
    var volume = 1.0; private set

    private var nextStatus = 0.0

    /** Safe from any thread. Sent to the dashboard log on the next tick. */
    fun queueEvent(level: String, message: String) {
        events.add(JsonWriter("event").field("level", level).field("message", message).toString())
    }

    /** One pass of the loop; call about once per frame. */
    fun tick() {
        while (true) handle(inbox.poll() ?: break)
        val t = localNow()
        if (clock.synced) engine.update(clock.serverTime(t))
        flush()
        if (connected() && t >= nextStatus) {
            nextStatus = t + statusIntervalSeconds
            send(statusJson(t))
        }
    }

    /** Sends everything queued by the content manager and by [queueEvent]. */
    fun flush() {
        while (true) send(content.outbox.poll() ?: break)
        while (true) send(events.poll() ?: break)
    }

    /** Re-cue the current anchor, e.g. after the app was suspended (also re-measure the clock). */
    fun resync() = engine.resync()

    fun handle(m: ServerMessage) {
        when (m.type) {
            "_connected" -> {
                send(content.inventoryJson())
                nextStatus = 0.0
            }
            "_disconnected" -> {}
            "time_pong" -> clock.add(m.t0, m.ts, localNow())
            "welcome" -> {
                serverName = m.serverName ?: ""
                deviceName = m.deviceName ?: ""
                engine.onSettings(m.settings)
                host.onNames(serverName, deviceName)
            }
            "settings" -> engine.onSettings(m.settings)
            "device_info" -> {
                deviceName = m.deviceName ?: deviceName
                host.onNames(serverName, deviceName)
            }
            "play" -> VideoCommand.from(m).let { host.onVideoCommand(it); engine.onPlay(it) }
            "pause" -> VideoCommand.from(m).let { host.onVideoCommand(it); engine.onPause(it) }
            "stop" -> engine.onStop()
            "volume" -> {
                volume = m.value.coerceIn(0.0, 1.0)
                host.setVolume(volume)
            }
            "recenter" -> host.recenter()
            "message" -> host.showMessage(m.text, m.seconds)
            "identify" -> host.identify(if (m.name.isNullOrEmpty()) deviceName else m.name, if (m.seconds > 0) m.seconds else 8.0)
            else -> content.handle(m) // sync_content, cancel_downloads, delete_content; others ignored
        }
    }

    private fun statusJson(localTime: Double): String {
        val w = JsonWriter("status")
        engine.writeStatus(w, clock.serverTime(localTime))
        telemetry()?.writeTo(w)
        w.field("volume", volume)
            .field("rtt_ms", if (clock.synced) Math.rint(clock.rtt * 10000.0) / 10.0 else null)
            .field("clock_synced", clock.synced)
            .field("fps", fps())
            .raw("download", content.progressJson())
            .field("error", if (engine.state == "error") player.error else null)
        return w.toString()
    }
}
