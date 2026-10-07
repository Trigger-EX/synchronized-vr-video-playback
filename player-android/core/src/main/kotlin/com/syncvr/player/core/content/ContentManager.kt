package com.syncvr.player.core.content

import com.syncvr.player.core.sync.JsonWriter
import com.syncvr.player.core.sync.ServerMessage
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * Runs [ContentStore] jobs for the `sync_content`, `cancel_downloads` and `delete_content`
 * messages on a background thread, one job at a time. Replies (`inventory`, `downloads_finished`)
 * are queued in [outbox] as JSON lines for the connection. Failed files that may work later
 * (network errors, 5xx) are retried after [RETRY_DELAYS_MS]; [sleep] waits and returns early when
 * its `cancelled` callback turns true (injected in tests).
 */
class ContentManager(
    val store: ContentStore,
    /** Free bytes on the video volume, or -1 when unknown. */
    private val freeBytes: () -> Long = { -1L },
    private val inUse: () -> String? = { null },
    private val onWarning: (String) -> Unit = {},
    private val sleep: ((Long, () -> Boolean) -> Unit)? = null,
    private val probe: BandwidthProbe = BandwidthProbe(),
) {
    val outbox = ConcurrentLinkedQueue<String>()

    private val generation = AtomicInteger()
    private val lock = Any()
    private var worker: Thread? = null
    private var current: Triple<String, Long, Long>? = null
    private var currentJob: String? = null
    private val probing = AtomicBoolean(false)

    val busy: Boolean get() = synchronized(lock) { worker?.isAlive == true }

    /** JSON for the status `download` field, or null when idle. */
    fun progressJson(): String? {
        val c = synchronized(lock) { current } ?: return null
        return "{\"name\":" + JsonWriter.stringLiteral(c.first) + ",\"received\":${c.second},\"total\":${c.third}}"
    }

    /** The running job's id as a JSON literal, or null when idle or the server sent none. */
    fun jobJson(): String? = synchronized(lock) { currentJob }

    fun inventoryJson(): String {
        val sb = StringBuilder("{\"type\":\"inventory\",\"files\":[")
        store.inventory().forEachIndexed { i, e ->
            if (i > 0) sb.append(',')
            sb.append("{\"name\":").append(JsonWriter.stringLiteral(e.name)).append(",\"size\":").append(e.size).append('}')
        }
        return sb.append("]}").toString()
    }

    /** Handles a content message; returns false when [m] is not one. */
    fun handle(m: ServerMessage): Boolean {
        when (m.type) {
            "sync_content" -> startSync(m)
            "cancel_downloads" -> {
                generation.incrementAndGet()
                store.abort()
                wake()
            }
            "delete_content" -> {
                for (name in store.delete(m.names, inUse())) onWarning("not deleting $name: it is loaded")
                outbox.add(inventoryJson())
            }
            "bandwidth_test" -> startBandwidthTest(m)
            else -> return false
        }
        return true
    }

    /** One speed test at a time, never while downloading; a refused test is answered with an error result. */
    private fun startBandwidthTest(m: ServerMessage) {
        val url = m.url
        val refusal = when {
            url.isNullOrEmpty() -> "no url"
            busy -> "busy downloading"
            !probing.compareAndSet(false, true) -> "a bandwidth test is already running"
            else -> null
        }
        if (refusal != null) {
            outbox.add(BandwidthResult(false, 0, 0.0, 0.0, refusal).toJson(m.job))
            return
        }
        val want = (if (m.bytes <= 0) DEFAULT_TEST_BYTES else m.bytes).coerceIn(MIN_TEST_BYTES, MAX_TEST_BYTES)
        val seconds = (if (m.seconds <= 0.0) DEFAULT_TEST_SECONDS else m.seconds).coerceIn(MIN_TEST_SECONDS, MAX_TEST_SECONDS)
        val t = Thread({
            try {
                outbox.add(probe.run(url!!, want, seconds).toJson(m.job))
            } catch (e: Exception) {
                outbox.add(BandwidthResult(false, 0, 0.0, 0.0, e.message ?: "test failed").toJson(m.job))
            } finally {
                probing.set(false)
            }
        }, "SyncVR bandwidth")
        t.isDaemon = true
        t.start()
    }

    private fun startSync(m: ServerMessage) {
        val free = freeBytes()
        val needed = store.bytesMissing(m.files)
        if (free >= 0 && needed > free - RESERVE_BYTES) {
            onWarning("not enough space: need $needed bytes, $free free")
            outbox.add(finished(emptyList(), m.files.map { it.name ?: "?" }, false, m.job))
            return
        }
        val gen = generation.incrementAndGet()
        store.abort()
        wake()
        val previous = synchronized(lock) { worker }
        val t = Thread({
            previous?.join()
            run(gen, m)
        }, "SyncVR downloads")
        t.isDaemon = true
        synchronized(lock) { worker = t }
        t.start()
    }

    private fun run(gen: Int, m: ServerMessage) {
        val cancelled = { gen != generation.get() }
        val ok = ArrayList<String>()
        var result = SyncResult(emptyList(), emptyList())
        synchronized(lock) { currentJob = m.job }
        try {
            var files = m.files
            var attempt = 0
            while (true) {
                result = store.sync(files, m.deleteOthers && attempt == 0, inUse(), cancelled) { n, r, t ->
                    synchronized(lock) { current = Triple(n, r, t) }
                }
                ok.addAll(result.ok)
                if (!result.retryable || cancelled() || attempt >= RETRY_DELAYS_MS.size) break
                val delay = RETRY_DELAYS_MS[attempt++]
                onWarning("download failed (${result.retryNames.joinToString()}), retrying in ${delay / 1000} s")
                // `current` stays set so status keeps showing the job while waiting.
                doSleep(delay, cancelled)
                if (cancelled()) break
                val again = result.retryNames.toSet()
                files = files.filter { it.name in again }
            }
        } finally {
            synchronized(lock) { current = null; currentJob = null }
            outbox.add(inventoryJson())
            outbox.add(finished(ok, result.failed, cancelled(), m.job))
        }
    }

    private fun doSleep(ms: Long, cancelled: () -> Boolean) {
        val s = sleep
        if (s != null) { s(ms, cancelled); return }
        val end = System.nanoTime() + ms * 1_000_000
        synchronized(lock) {
            while (!cancelled()) {
                val left = (end - System.nanoTime()) / 1_000_000
                if (left <= 0) break
                (lock as Object).wait(left)
            }
        }
    }

    private fun wake() = synchronized(lock) { (lock as Object).notifyAll() }

    private fun finished(ok: List<String>, failed: List<String>, cancelled: Boolean, job: String?) =
        JsonWriter("downloads_finished")
            .raw("ok", JsonWriter.stringArray(ok))
            .raw("failed", JsonWriter.stringArray(failed))
            .field("cancelled", cancelled)
            .also { if (job != null) it.raw("job", job) }
            .toString()

    private companion object {
        const val RESERVE_BYTES = 200L * 1024 * 1024
        const val MIN_TEST_BYTES = 1L * 1024 * 1024
        const val DEFAULT_TEST_BYTES = 20L * 1024 * 1024
        const val MAX_TEST_BYTES = 200L * 1024 * 1024
        const val MIN_TEST_SECONDS = 5.0
        const val DEFAULT_TEST_SECONDS = 30.0
        const val MAX_TEST_SECONDS = 120.0
        val RETRY_DELAYS_MS = longArrayOf(5_000, 15_000, 30_000)
    }
}
