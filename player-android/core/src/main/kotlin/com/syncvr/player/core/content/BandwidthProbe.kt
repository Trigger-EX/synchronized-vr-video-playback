package com.syncvr.player.core.content

import com.syncvr.player.core.sync.JsonWriter
import java.io.IOException

/** Outcome of one download speed test; [mbps] is megabits per second of what was actually read. */
data class BandwidthResult(
    val ok: Boolean,
    val bytes: Long,
    val seconds: Double,
    val mbps: Double,
    val error: String = "",
) {
    /** The `bandwidth_result` line for the server; [job] is the request's id as a JSON literal. */
    fun toJson(job: String?): String =
        JsonWriter("bandwidth_result")
            .raw("job", job)
            .field("ok", ok)
            .field("bytes", bytes)
            .field("seconds", Math.round(seconds * 1000) / 1000.0)
            .field("mbps", Math.round(mbps * 100) / 100.0)
            .field("error", error)
            .toString()
}

/**
 * Reads up to [wantBytes] of [url] (or until [maxSeconds] pass) and throws the data away, so the number
 * reflects the same HTTP path the real downloads use. Pure: HTTP and the clock are injected.
 */
class BandwidthProbe(
    private val http: HttpFetcher = JavaHttpFetcher,
    private val nanoTime: () -> Long = System::nanoTime,
) {
    fun run(url: String, wantBytes: Long, maxSeconds: Double): BandwidthResult {
        val start = nanoTime()
        var total = 0L
        fun elapsed() = (nanoTime() - start) / 1e9
        try {
            http.get(url, 0).use { resp ->
                if (resp.status !in 200..299) return fail("HTTP ${resp.status}", elapsed())
                val buf = ByteArray(CHUNK)
                val body = resp.body
                while (total < wantBytes && elapsed() < maxSeconds) {
                    val n = body.read(buf, 0, minOf(CHUNK.toLong(), wantBytes - total).toInt())
                    if (n < 0) break
                    total += n
                }
            }
        } catch (e: IOException) {
            return fail(e.message ?: "I/O error", elapsed(), total)
        }
        val seconds = elapsed()
        if (total <= 0) return fail("no data received", seconds)
        return BandwidthResult(true, total, seconds, total * 8 / maxOf(seconds, MIN_SECONDS) / 1e6)
    }

    private fun fail(error: String, seconds: Double, bytes: Long = 0) = BandwidthResult(false, bytes, seconds, 0.0, error)

    private companion object {
        const val CHUNK = 64 * 1024
        const val MIN_SECONDS = 1e-3
    }
}
