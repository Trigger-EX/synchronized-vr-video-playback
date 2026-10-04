package com.syncvr.player

import android.app.Activity
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Surface
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.WindowManager
import com.syncvr.player.core.DisplayMode
import com.syncvr.player.core.ModeCycle
import com.syncvr.player.core.PanelText
import com.syncvr.player.core.PlayerInfo
import com.syncvr.player.core.VideoSelection
import com.syncvr.player.core.sync.VideoCommand
import com.syncvr.player.core.sync.VideoFiles
import java.io.File

/**
 * VrApi host activity: forwards lifecycle and surface events to native code, plays the first
 * video found with ExoPlayer, and cycles through the display modes every 15 s.
 */
class MainActivity : Activity(), SurfaceHolder.Callback {
    private val main = Handler(Looper.getMainLooper())
    private val cycle = ModeCycle()
    private val panel = PanelRenderer()

    private var handle = 0L
    private var player: ExoVideoPlayer? = null
    private var videoFile: File? = null
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
            // Demo behaviour until the sync engine drives playback: start once the decoder is ready.
            player?.let { if (resumed && it.isPrepared && !it.isPlaying) it.play() }
            redrawPanel()
            main.postDelayed(this, PANEL_REFRESH_MS)
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
    }

    override fun onResume() {
        super.onResume()
        resumed = true
        if (handle != 0L) NativeBridge.nativeResume(handle)
        player?.play()
        main.removeCallbacks(modeTick)
        main.removeCallbacks(panelTick)
        main.postDelayed(modeTick, cycle.intervalMs)
        panelTick.run()
    }

    override fun onPause() {
        resumed = false
        main.removeCallbacks(modeTick)
        main.removeCallbacks(panelTick)
        player?.pause()
        // Blocks (bounded) until the render thread has left VR mode.
        if (handle != 0L) NativeBridge.nativePause(handle)
        super.onPause()
    }

    override fun onDestroy() {
        main.removeCallbacksAndMessages(null)
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
        val dir = File(getExternalFilesDir(null), "videos")
        if (!dir.exists() && !dir.mkdirs()) Log.w(TAG, "Could not create $dir")
        val picked = VideoSelection.pick(dir.list()?.toList().orEmpty())
        if (picked == null) {
            Log.w(TAG, "No video in $dir")
            return
        }
        val file = File(dir, picked)
        videoFile = file
        Log.i(TAG, "Playing ${file.absolutePath}")
        val exo = ExoVideoPlayer(
            this,
            resolveFile = { name ->
                VideoFiles.resolve(name, dir.list()?.toList().orEmpty())?.let { File(dir, it) }
            },
            onVideoAspect = { aspect ->
                Log.i(TAG, "Video aspect=$aspect")
                if (handle != 0L) NativeBridge.nativeSetVideoAspect(handle, aspect)
            },
            onPlayerEvent = { msg ->
                Log.e(TAG, msg)
                redrawPanel()
            },
        )
        exo.load(VideoCommand(video = picked, loop = true))
        player = exo
        applyMode(mode)
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
        val file = videoFile
        val line2 = if (file == null) {
            "No video in ${PlayerInfo.VIDEO_DIR}"
        } else {
            PanelText.videoLine(
                file.name,
                ((exo?.time ?: 0.0) * 1000).toLong(),
                if (exo != null && exo.length > 0) (exo.length * 1000).toLong() else -1L,
            )
        }
        val nativeStatus = if (handle != 0L) NativeBridge.nativeGetStatus(handle) else "native not running"
        val line3 = player?.error?.let { "Player error: $it" } ?: nativeStatus
        panel.draw(mode.label(), line2, line3)
    }

    private companion object {
        const val TAG = "SyncVR"
        const val PANEL_REFRESH_MS = 1000L
    }
}
