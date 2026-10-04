package com.syncvr.player

import android.content.Context
import android.os.SystemClock
import android.view.Surface
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.PlaybackParameters
import androidx.media3.common.Player
import androidx.media3.common.VideoSize
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.SeekParameters
import com.syncvr.player.core.sync.PlayerMath
import com.syncvr.player.core.sync.SeekTracker
import com.syncvr.player.core.sync.VideoCommand
import com.syncvr.player.core.sync.VideoPlayer
import java.io.File

/**
 * [VideoPlayer] on top of Media3 ExoPlayer (MediaCodec).
 *
 * - Seeks are exact (SeekParameters.EXACT), not snapped to the previous keyframe.
 * - Speed changes keep the pitch (PlaybackParameters pitch stays 1.0; ExoPlayer time-stretches audio).
 * - [time] is ExoPlayer's playback position, which it derives from the audio renderer's clock
 *   while audio is playing, so it tracks what is actually audible.
 * - Looping uses Player.REPEAT_MODE_ONE.
 * - Frames go to the Surface given to [setSurface]: the compositor layer's swapchain surface, or
 *   the SurfaceTexture surface of the sphere fallback (see NativeBridge / external_texture.cpp).
 *
 * Threading: ExoPlayer is bound to the thread it was built on, so every call here, including the
 * property getters, must be made on that thread (the main thread in MainActivity).
 */
class ExoVideoPlayer(
    context: Context,
    /** Maps a server video name to a local file, or null when this headset does not have it. */
    private val resolveFile: (String) -> File?,
    /** Called with width/height (pixel-ratio corrected) when the decoder reports the video size. */
    private val onVideoAspect: (Float) -> Unit = {},
    private val onPlayerEvent: (String) -> Unit = {},
) : VideoPlayer {

    private val exo: ExoPlayer = ExoPlayer.Builder(context).build()
    private val seekTracker = SeekTracker()
    private var surface: Surface? = null
    private var prepared = false
    private var looping = false

    override var loadedVideo: String? = null
        private set
    override var error: String? = null
        private set

    init {
        exo.setSeekParameters(SeekParameters.EXACT)
        // Do not let audio focus changes pause playback; the operator drives the fleet.
        exo.setAudioAttributes(
            AudioAttributes.Builder()
                .setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_MOVIE)
                .build(),
            /* handleAudioFocus = */ false,
        )
        exo.addListener(object : Player.Listener {
            override fun onPlaybackStateChanged(playbackState: Int) {
                if (playbackState == Player.STATE_READY) {
                    prepared = true
                    seekTracker.complete()
                }
            }

            override fun onPlayerError(e: PlaybackException) {
                error = "${e.errorCodeName}: ${e.message}"
                loadedVideo = null // so the next command for this video reloads it
                prepared = false
                onPlayerEvent("ExoPlayer error: $error")
            }

            override fun onVideoSizeChanged(videoSize: VideoSize) {
                if (videoSize.width <= 0 || videoSize.height <= 0) return
                onVideoAspect(videoSize.width * videoSize.pixelWidthHeightRatio / videoSize.height)
            }
        })
    }

    /** Sets (or clears, with null) the Surface the decoder renders into. */
    fun setSurface(target: Surface?) {
        surface = target
        if (target != null && target.isValid) exo.setVideoSurface(target) else exo.clearVideoSurface()
    }

    fun setVolume(volume: Float) {
        exo.volume = volume.coerceIn(0f, 1f)
    }

    fun release() {
        exo.clearVideoSurface()
        exo.release()
    }

    override val isPrepared: Boolean get() = prepared && loadedVideo != null
    override val isSeeking: Boolean get() = seekTracker.isSeeking(now())
    override val isPlaying: Boolean get() = exo.isPlaying
    override val time: Double get() = if (loadedVideo == null) 0.0 else PlayerMath.msToSeconds(exo.currentPosition)
    override val length: Double get() = if (prepared) PlayerMath.durationSeconds(exo.duration) else 0.0
    override val canSetRate: Boolean get() = prepared

    override fun load(cmd: VideoCommand) {
        stop()
        val name = cmd.video
        val file = name?.let(resolveFile)
        if (name == null || file == null) {
            error = "video not on this headset: $name"
            return
        }
        loadedVideo = name
        looping = cmd.loop
        exo.repeatMode = if (looping) Player.REPEAT_MODE_ONE else Player.REPEAT_MODE_OFF
        exo.setMediaItem(MediaItem.fromUri(android.net.Uri.fromFile(file)))
        exo.playWhenReady = false // the engine decides when to start
        exo.prepare()
    }

    override fun play() {
        if (prepared) exo.playWhenReady = true
    }

    override fun pause() {
        if (prepared) exo.playWhenReady = false
    }

    override fun stop() {
        exo.stop()
        exo.clearMediaItems()
        exo.playbackParameters = PlaybackParameters.DEFAULT
        exo.playWhenReady = false
        seekTracker.complete()
        prepared = false
        error = null
        loadedVideo = null
    }

    override fun seek(seconds: Double) {
        if (!prepared) return
        seekTracker.begin(now())
        exo.seekTo(PlayerMath.secondsToMs(seconds))
    }

    override fun setRate(rate: Double) {
        if (!prepared) return
        // Pitch stays at 1.0 so a drift-correcting speed nudge is inaudible.
        exo.playbackParameters = PlaybackParameters(PlayerMath.clampRate(rate).toFloat(), 1f)
    }

    override fun setLooping(loop: Boolean) {
        looping = loop
        exo.repeatMode = if (loop) Player.REPEAT_MODE_ONE else Player.REPEAT_MODE_OFF
    }

    // ExoPlayer has no external time reference; the engine falls back to speed nudging.
    override fun setExternalTime(seconds: Double) {}

    override fun clearExternalTime() {}

    private fun now(): Double = SystemClock.elapsedRealtime() / 1000.0
}
