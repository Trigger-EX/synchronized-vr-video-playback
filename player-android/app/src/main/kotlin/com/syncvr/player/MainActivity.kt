package com.syncvr.player

import android.app.Activity
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Surface
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.WindowManager
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.VideoSize
import androidx.media3.exoplayer.ExoPlayer
import com.syncvr.player.core.DisplayMode
import com.syncvr.player.core.ModeCycle
import com.syncvr.player.core.PanelText
import com.syncvr.player.core.PlayerInfo
import com.syncvr.player.core.VideoSelection
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
    private var player: ExoPlayer? = null
    private var videoFile: File? = null
    private var playerError: String? = null
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
        player?.let {
            it.clearVideoSurface()
            it.release()
        }
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
        val exo = ExoPlayer.Builder(this).build()
        exo.addListener(object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                playerError = "${error.errorCodeName}: ${error.message}"
                Log.e(TAG, "ExoPlayer error: $playerError", error)
                redrawPanel()
            }

            override fun onVideoSizeChanged(videoSize: VideoSize) {
                if (videoSize.width <= 0 || videoSize.height <= 0) return
                val aspect = videoSize.width * videoSize.pixelWidthHeightRatio / videoSize.height
                Log.i(TAG, "Video size ${videoSize.width}x${videoSize.height} aspect=$aspect")
                if (handle != 0L) NativeBridge.nativeSetVideoAspect(handle, aspect)
            }

            override fun onPlaybackStateChanged(playbackState: Int) {
                Log.i(TAG, "Playback state ${stateName(playbackState)}")
            }
        })
        exo.repeatMode = Player.REPEAT_MODE_ONE
        exo.setMediaItem(MediaItem.fromUri(Uri.fromFile(file)))
        exo.playWhenReady = true
        exo.prepare()
        player = exo
    }

    private fun applyMode(newMode: DisplayMode) {
        mode = newMode
        Log.i(TAG, "Mode -> ${mode.label()}")
        if (handle != 0L) NativeBridge.nativeSetMode(handle, mode.ordinal)
        val target = if (mode.usesCompositorSurface) videoSurface else sphereSurface
        val exo = player
        if (exo != null) {
            if (target != null && target.isValid) exo.setVideoSurface(target) else exo.clearVideoSurface()
        }
        redrawPanel()
    }

    private fun redrawPanel() {
        if (!resumed) return
        val exo = player
        val file = videoFile
        val line2 = if (file == null) {
            "No video in ${PlayerInfo.VIDEO_DIR}"
        } else {
            PanelText.videoLine(file.name, exo?.currentPosition ?: 0L, exo?.duration ?: -1L)
        }
        val nativeStatus = if (handle != 0L) NativeBridge.nativeGetStatus(handle) else "native not running"
        val line3 = playerError?.let { "Player error: $it" } ?: nativeStatus
        panel.draw(mode.label(), line2, line3)
    }

    private fun stateName(state: Int) = when (state) {
        Player.STATE_IDLE -> "idle"
        Player.STATE_BUFFERING -> "buffering"
        Player.STATE_READY -> "ready"
        Player.STATE_ENDED -> "ended"
        else -> state.toString()
    }

    private companion object {
        const val TAG = "SyncVR"
        const val PANEL_REFRESH_MS = 1000L
    }
}
