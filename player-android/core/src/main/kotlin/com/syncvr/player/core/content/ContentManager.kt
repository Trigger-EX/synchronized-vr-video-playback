package com.syncvr.player.core.content

import com.syncvr.player.core.sync.JsonWriter
import com.syncvr.player.core.sync.ServerMessage
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.atomic.AtomicInteger

/**
 * Runs [ContentStore] jobs for the `sync_content`, `cancel_downloads` and `delete_content`
 * messages on a background thread, one job at a time. Replies (`inventory`, `downloads_finished`)
 * are queued in [outbox] as JSON lines for the connection. Port of ContentManager.cs.
 */
class ContentManager(
    val store: ContentStore,
    /** Free bytes on the video volume, or -1 when unknown. */
    private val freeBytes: () -> Long = { -1L },
    private val inUse: () -> String? = { null },
    private val onWarning: (String) -> Unit = {},
) {
    val outbox = ConcurrentLinkedQueue<String>()

    private val generation = AtomicInteger()
    private val lock = Any()
    private var worker: Thread? = null
    private var current: Triple<String, Long, Long>? = null

    val busy: Boolean get() = synchronized(lock) { worker?.isAlive == true }

    /** JSON for the status `download` field, or null when idle. */
    fun progressJson(): String? {
        val c = synchronized(lock) { current } ?: return null
        return "{\"name\":" + JsonWriter.stringLiteral(c.first) + ",\"received\":${c.second},\"total\":${c.third}}"
    }

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
            "cancel_downloads" -> generation.incrementAndGet()
            "delete_content" -> {
                for (name in store.delete(m.names, inUse())) onWarning("not deleting $name: it is loaded")
                outbox.add(inventoryJson())
            }
            else -> return false
        }
        return true
    }

    private fun startSync(m: ServerMessage) {
        val free = freeBytes()
        val needed = store.bytesMissing(m.files)
        if (free >= 0 && needed > free - RESERVE_BYTES) {
            onWarning("not enough space: need $needed bytes, $free free")
            outbox.add(finished(emptyList(), m.files.map { it.name ?: "?" }, false))
            return
        }
        val gen = generation.incrementAndGet()
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
        var result = SyncResult(emptyList(), emptyList())
        try {
            result = store.sync(m.files, m.deleteOthers, inUse(), { gen != generation.get() }) { n, r, t ->
                synchronized(lock) { current = Triple(n, r, t) }
            }
        } finally {
            synchronized(lock) { current = null }
            outbox.add(inventoryJson())
            outbox.add(finished(result.ok, result.failed, gen != generation.get()))
        }
    }

    private fun finished(ok: List<String>, failed: List<String>, cancelled: Boolean) =
        JsonWriter("downloads_finished")
            .raw("ok", JsonWriter.stringArray(ok))
            .raw("failed", JsonWriter.stringArray(failed))
            .field("cancelled", cancelled)
            .toString()

    private companion object {
        const val RESERVE_BYTES = 200L * 1024 * 1024
    }
}
