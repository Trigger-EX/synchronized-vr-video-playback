package com.syncvr.player

import android.app.Activity
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.media.AudioManager
import android.media.ToneGenerator
import android.util.Log
import android.view.Surface
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.WindowManager
import android.provider.Settings
import com.syncvr.player.core.AppLifecycle
import com.syncvr.player.core.CalibrationPersistence
import com.syncvr.player.core.DisplayMode
import com.syncvr.player.core.ModeCycle
import com.syncvr.player.core.FrameRate
import com.syncvr.player.core.Hello
import com.syncvr.player.core.Overlay
import com.syncvr.player.core.OverlayInput
import com.syncvr.player.core.PanelText
import com.syncvr.player.core.PlayerController
import com.syncvr.player.core.PlayerHost
import com.syncvr.player.core.PlayerInfo
import com.syncvr.player.core.TelemetryCache
import com.syncvr.player.core.net.ConnectionConfig
import com.syncvr.player.core.net.ServerConnection
import com.syncvr.player.core.sync.LocalClock
import com.syncvr.player.core.sync.VideoCommand

/**
 * VrApi host activity: forwards lifecycle and surface events to native code, finds the server
 * (UDP beacon, multicast lock held by [MulticastLockGuard]) and runs the [PlayerController]
 * message loop. Threads: ServerConnection's own threads fill its inbox; everything else, including
 * every ExoPlayer call, runs on the main thread from [loopTick]. The display mode still cycles
 * every 15 s for the hardware check.
 */
class MainActivity : Activity(), SurfaceHolder.Callback, PlayerHost {
    private val main = Handler(Looper.getMainLooper())
    private val cycle = ModeCycle()
    private val panel = PanelRenderer()
    private val overlay = Overlay()
    private var tone: ToneGenerator? = null

    private var handle = 0L
    private var player: ExoVideoPlayer? = null
    private var contentHost: ContentHost? = null
    private var connection: ServerConnection? = null
    private var controller: PlayerController? = null
    private var sampler: TelemetrySampler? = null
    private var wifiLock: WifiPerformanceLock? = null
    private var resumed = false
    private var calibration: CalibrationPersistence? = null
    private var appLifecycle: AppLifecycle? = null

    private var videoSurface: Surface? = null
    private var panelSurface: Surface? = null
    private var sphereSurface: Surface? = null
    private var mode = DisplayMode.EQUIRECT_MONO

    private val modeTick = object : Runnable {
        override fun run() {
            applyMode(cycle.next(mode))
            main.postDelayed(this, cycle.intervalMs)
        }
    }
    private val panelTick = object : Runnable {
        override fun run() {
            redrawPanel()
            main.postDelayed(this, PANEL_REFRESH_MS)
        }
    }
    /** Drains the server inbox and steps the sync engine; main thread, so ExoPlayer is touched only here. */
    private val loopTick = object : Runnable {
        override fun run() {
            controller?.tick()
            calibration?.tick()
            main.postDelayed(this, LOOP_INTERVAL_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Log.i(TAG, "SyncVR player ${BuildConfig.VERSION_NAME} (${PlayerInfo.PLAYER}), videos: ${PlayerInfo.VIDEO_DIR}")
        Diagnostics.logAll()
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        val surfaceView = SurfaceView(this)
        surfaceView.holder.addCallback(this)
        setContentView(surfaceView)

        handle = NativeBridge.nativeCreate(this)
        if (handle == 0L) Log.e(TAG, "nativeCreate failed; no VR output")

        createPlayer()
        startNetworking()
    }

    override fun onResume() {
        super.onResume()
        resumed = true
        sampler?.start()
        appLifecycle?.onResume() ?: run { if (handle != 0L) NativeBridge.nativeResume(handle) }
        main.removeCallbacks(modeTick)
        main.removeCallbacks(panelTick)
        main.removeCallbacks(loopTick)
        main.postDelayed(modeTick, cycle.intervalMs)
        loopTick.run()
        panelTick.run()
    }

    override fun onPause() {
        resumed = false
        sampler?.stop()
        main.removeCallbacks(modeTick)
        main.removeCallbacks(panelTick)
        main.removeCallbacks(loopTick)
        // Saves calibration, pauses the player, then blocks (bounded) until the render thread has
        // left VR mode.
        appLifecycle?.onPause() ?: run {
            player?.pause()
            if (handle != 0L) NativeBridge.nativePause(handle)
        }
        super.onPause()
    }

    override fun onDestroy() {
        main.removeCallbacksAndMessages(null)
        connection?.stop()
        connection = null
        wifiLock?.release()
        wifiLock = null
        calibration?.save()
        appLifecycle = null
        calibration = null
        controller = null
        player?.release()
        player = null
        tone?.release()
        tone = null
        panel.setSurface(null)
        videoSurface = null
        panelSurface = null
        sphereSurface = null
        if (handle != 0L) {
            NativeBridge.nativeDestroy(handle)
            handle = 0L
        }
        super.onDestroy()
    }

    // SurfaceHolder.Callback: the activity window surface that VrApi presents to.

    override fun surfaceCreated(holder: SurfaceHolder) {
        Log.i(TAG, "surfaceCreated")
        if (handle != 0L) NativeBridge.nativeSurfaceChanged(handle, holder.surface)
    }

    override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int) {
        Log.i(TAG, "surfaceChanged ${width}x$height format=$format")
        if (handle != 0L) NativeBridge.nativeSurfaceChanged(handle, holder.surface)
    }

    override fun surfaceDestroyed(holder: SurfaceHolder) {
        Log.i(TAG, "surfaceDestroyed")
        if (handle != 0L) NativeBridge.nativeSurfaceDestroyed(handle)
    }

    /** Called by native code on its render thread once the swapchain surfaces exist. */
    @Suppress("unused")
    fun onNativeSurfacesReady(video: Surface?, panelSurf: Surface?, sphere: Surface?) {
        runOnUiThread {
            Log.i(TAG, "Native surfaces ready: video=${video != null} panel=${panelSurf != null} sphere=${sphere != null}")
            if (handle == 0L) return@runOnUiThread
            videoSurface = video
            panelSurface = panelSurf
            sphereSurface = sphere
            panel.setSurface(panelSurf)
            applyMode(mode)
        }
    }

    private fun createPlayer() {
        val host = ContentHost(
            this,
            inUse = { player?.loadedVideo },
            onWarning = { controller?.queueEvent("warn", it) },
        )
        contentHost = host
        val store = host.manager.store
        player = ExoVideoPlayer(
            this,
            resolveFile = { name -> store.pathFor(name) },
            onVideoAspect = { aspect ->
                Log.i(TAG, "Video aspect=$aspect")
                if (handle != 0L) NativeBridge.nativeSetVideoAspect(handle, aspect)
            },
            onPlayerEvent = { msg ->
                Log.e(TAG, msg)
                controller?.queueEvent("error", msg)
                redrawPanel()
            },
        )
        applyMode(mode)
    }

    private fun startNetworking() {
        val exo = player ?: return
        val host = contentHost ?: return
        val hello = Hello.build(
            deviceId = deviceId(),
            serial = serial(),
            model = Build.MODEL ?: "",
            appVersion = BuildConfig.VERSION_NAME,
        )
        val conn = ServerConnection(
            ConnectionConfig(),
            hello,
            guard = MulticastLockGuard(this),
            log = { Log.i(TAG, it) },
        )
        connection = conn
        val telemetrySampler = TelemetrySampler(this, host.manager.store.folder)
        sampler = telemetrySampler
        if (resumed) telemetrySampler.start()
        val telemetry = TelemetryCache(TELEMETRY_INTERVAL_S, { LocalClock.now }, telemetrySampler::sample)
        controller = PlayerController(
            player = exo,
            content = host.manager,
            clock = conn.clock,
            inbox = conn.inbox,
            send = conn::send,
            connected = { conn.connected },
            host = this,
            telemetry = telemetry::get,
            // The render thread publishes "VR on, 72.0 fps" as its status text.
            fps = { if (handle != 0L) FrameRate.parse(NativeBridge.nativeGetStatus(handle)) else null },
        )
        val ctl = controller!!
        val cal = CalibrationPersistence(ctl.engine, PrefsCalibrationStore(this), { LocalClock.now })
        cal.load()
        calibration = cal
        appLifecycle = AppLifecycle(
            calibration = cal,
            leaveVr = { if (handle != 0L) NativeBridge.nativePause(handle) },
            enterVr = { if (handle != 0L) NativeBridge.nativeResume(handle) },
            pausePlayer = { exo.pause() },
            resyncClock = { conn.resyncClock() },
            resyncPlayback = { ctl.resync() },
        )
        wifiLock = WifiPerformanceLock(this).also { it.acquire() }
        conn.start()
    }

    // The serial matches `adb devices`, which makes headsets easy to map; ANDROID_ID is the fallback.
    @Suppress("DEPRECATION")
    private fun serial(): String = try {
        Build.SERIAL ?: ""
    } catch (e: SecurityException) {
        ""
    }

    private fun deviceId(): String {
        val s = serial()
        if (s.isNotEmpty() && s != Build.UNKNOWN) return s
        return Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID) ?: "unknown"
    }

    // PlayerHost. Called on the main thread from the controller's tick.

    override fun onVideoCommand(cmd: VideoCommand) {
        Log.i(TAG, "Video command ${cmd.video} ${cmd.projection}/${cmd.stereo}")
    }

    override fun setVolume(volume: Double) {
        player?.setVolume(volume.toFloat())
    }

    override fun recenter() {
        if (handle != 0L) NativeBridge.nativeRecenter(handle)
    }

    override fun showMessage(text: String?, seconds: Double) {
        overlay.showMessage(text, seconds, LocalClock.now)
        redrawPanel()
    }

    override fun identify(name: String, seconds: Double) {
        overlay.identify(name, seconds, LocalClock.now)
        beep()
        redrawPanel()
    }

    /** Three short pips (Overlay.beepOffsetsMs); ToneGenerator plays on the music stream. */
    private fun beep() {
        val gen = tone ?: try {
            ToneGenerator(AudioManager.STREAM_MUSIC, 100).also { tone = it }
        } catch (e: RuntimeException) {
            Log.w(TAG, "No ToneGenerator: $e")
            return
        }
        for (offset in Overlay.beepOffsetsMs()) {
            main.postDelayed({ if (tone != null) gen.startTone(ToneGenerator.TONE_PROP_BEEP, 180) }, offset)
        }
    }

    private fun applyMode(newMode: DisplayMode) {
        mode = newMode
        Log.i(TAG, "Mode -> ${mode.label()}")
        if (handle != 0L) NativeBridge.nativeSetMode(handle, mode.ordinal)
        val target = if (mode.usesCompositorSurface) videoSurface else sphereSurface
        player?.setSurface(target)
        redrawPanel()
    }

    private fun redrawPanel() {
        if (!resumed) return
        val exo = player
        val c = controller
        val conn = connection
        val now = LocalClock.now
        val videoLine = if (exo != null && exo.loadedVideo != null) PanelText.videoLine(
            exo.loadedVideo,
            (exo.time * 1000).toLong(),
            if (exo.length > 0) (exo.length * 1000).toLong() else -1L,
        ) else null
        val content = overlay.compose(
            now,
            OverlayInput(
                deviceName = c?.deviceName ?: "",
                serverName = c?.serverName ?: "",
                connected = conn?.connected ?: false,
                connectionStatus = conn?.status ?: "starting",
                state = c?.engine?.reportedState ?: "idle",
                playerError = exo?.error,
                receivingContent = contentHost?.manager?.progressJson() != null,
                modeLabel = mode.label(),
                videoLine = videoLine,
                nativeStatus = if (handle != 0L) NativeBridge.nativeGetStatus(handle) else "native not running",
            ),
        )
        if (handle != 0L) NativeBridge.nativeSetPanelProminent(handle, content.prominent)
        panel.draw(content.line1, content.line2, content.line3)
    }

    private companion object {
        const val TAG = "SyncVR"
        const val PANEL_REFRESH_MS = 1000L
        const val LOOP_INTERVAL_MS = 16L
        const val TELEMETRY_INTERVAL_S = 5.0
    }
}
