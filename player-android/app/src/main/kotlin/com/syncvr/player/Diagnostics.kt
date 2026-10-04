package com.syncvr.player

import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.os.Build
import android.util.Log

/** One-shot startup logging (tag SyncVR) for the hardware check: device identity and video decoders. */
object Diagnostics {
    private const val TAG = "SyncVR"
    private val MIME_TYPES = listOf("video/avc", "video/hevc")
    private val SIZES = listOf(3840 to 1920, 4096 to 2048, 5120 to 2560, 5760 to 2880)

    fun logAll() {
        Log.i(TAG, "Device model=${Build.MODEL} device=${Build.DEVICE} display=${Build.DISPLAY}")
        Log.i(TAG, "Fingerprint=${Build.FINGERPRINT} sdk=${Build.VERSION.SDK_INT}")
        try {
            logDecoders()
        } catch (e: Exception) {
            Log.w(TAG, "Decoder enumeration failed: $e")
        }
    }

    private fun logDecoders() {
        val infos = MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos
        var found = 0
        for (info in infos) {
            if (info.isEncoder) continue
            for (mime in MIME_TYPES) {
                if (info.supportedTypes.none { it.equals(mime, ignoreCase = true) }) continue
                found++
                try {
                    logDecoder(info, mime)
                } catch (e: Exception) {
                    Log.w(TAG, "Decoder ${info.name} $mime query failed: $e")
                }
            }
        }
        Log.i(TAG, "Decoder enumeration done: $found avc/hevc decoder entries")
    }

    private fun logDecoder(info: MediaCodecInfo, mime: String) {
        val caps = info.getCapabilitiesForType(mime)
        val video = caps.videoCapabilities
        val instances = if (Build.VERSION.SDK_INT >= 23) caps.maxSupportedInstances.toString() else "n/a"
        val hw = if (Build.VERSION.SDK_INT >= 29) {
            info.isHardwareAccelerated.toString()
        } else {
            "${guessHardware(info.name)} (guessed from name)"
        }
        Log.i(
            TAG,
            "Decoder ${info.name} mime=$mime hw=$hw instances=$instances " +
                "widths=${video.supportedWidths} heights=${video.supportedHeights} " +
                "bitrate=${video.bitrateRange}",
        )
        for ((w, h) in SIZES) {
            val size = video.isSizeSupported(w, h)
            val rate = try {
                video.areSizeAndRateSupported(w, h, 30.0)
            } catch (e: Exception) {
                false
            }
            Log.i(TAG, "  ${info.name} $mime ${w}x$h size=$size at30fps=$rate")
        }
    }

    private fun guessHardware(name: String): Boolean {
        val n = name.lowercase()
        return !(n.startsWith("omx.google.") || n.startsWith("c2.android.") || n.contains(".sw.") ||
            n.endsWith(".sw") || n.contains("software"))
    }
}
