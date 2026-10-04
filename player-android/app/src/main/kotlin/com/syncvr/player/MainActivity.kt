package com.syncvr.player

import android.app.Activity
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Surface
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.WindowManager
import android.provider.Settings
import com.syncvr.player.core.DisplayMode
import com.syncvr.player.core.ModeCycle
import com.syncvr.player.core.Hello
import com.syncvr.player.core.PanelText
import com.syncvr.player.core.PlayerController
import com.syncvr.player.core.PlayerHost
import com.syncvr.player.core.PlayerInfo
import com.syncvr.player.core.net.ConnectionConfig
import com.syncvr.player.core.net.ServerConnection
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

    private var handle = 0L
    private var player: ExoVideoPlayer? = null
    private var contentHost: ContentHost? = null
    private var connection: ServerConnection? = null
    private var controller: PlayerController? = null
    private var resumed = false

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
        if (handle != 0L) NativeBridge.nativeResume(handle)
        // The monotonic clock stops while the headset sleeps: re-measure it, then re-cue.
        connection?.resyncClock()
        controller?.resync()
        main.removeCallbacks(modeTick)
        main.removeCallbacks(panelTick)
        main.removeCallbacks(loopTick)
        main.postDelayed(modeTick, cycle.intervalMs)
        loopTick.run()
        panelTick.run()
    }

    override fun onPause() {
        resumed = false
        main.removeCallbacks(modeTick)
        main.removeCallbacks(panelTick)
        main.removeCallbacks(loopTick)
        player?.pause()
        // Blocks (bounded) until the render thread has left VR mode.
        if (handle != 0L) NativeBridge.nativePause(handle)
        super.onPause()
    }

    override fun onDestroy() {
        main.removeCallbacksAndMessages(null)
        connection?.stop()
        connection = null
        controller = null
        player?.release()
        player = null
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
        controller = PlayerController(
            player = exo,
            content = host.manager,
            clock = conn.clock,
            inbox = conn.inbox,
            send = conn::send,
            connected = { conn.connected },
            host = this,
        )
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
        val title = c?.deviceName?.takeIf { it.isNotEmpty() } ?: "SyncVR"
        val line2 = when {
            exo != null && exo.loadedVideo != null -> PanelText.videoLine(
                exo.loadedVideo,
                ((exo.time) * 1000).toLong(),
                if (exo.length > 0) (exo.length * 1000).toLong() else -1L,
            ) + "  [" + (c?.engine?.reportedState ?: "") + "]"
            else -> "$title: ${connection?.status ?: "starting"}"
        }
        val nativeStatus = if (handle != 0L) NativeBridge.nativeGetStatus(handle) else "native not running"
        val line3 = player?.error?.let { "Player error: $it" } ?: nativeStatus
        panel.draw(mode.label(), line2, line3)
    }

    private companion object {
        const val TAG = "SyncVR"
        const val PANEL_REFRESH_MS = 1000L
        const val LOOP_INTERVAL_MS = 16L
    }
}
