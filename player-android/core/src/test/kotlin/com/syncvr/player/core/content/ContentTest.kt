package com.syncvr.player.core.content

import com.sun.net.httpserver.HttpServer
import com.syncvr.player.core.sync.ContentFile
import com.syncvr.player.core.sync.ServerMessage
import java.io.ByteArrayInputStream
import java.io.File
import java.net.InetSocketAddress
import java.nio.file.Files
import java.security.MessageDigest
import kotlin.test.AfterTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

private fun sha(b: ByteArray) = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }

private class FakeHttp(val data: Map<String, ByteArray>, val honorRange: Boolean = true) : HttpFetcher {
    val ranges = ArrayList<Long>()
    var corruptFirst = false
    override fun get(url: String, rangeStart: Long): HttpResponse {
        ranges.add(rangeStart)
        val d = data[url] ?: return resp(404, ByteArray(0))
        var bytes = d
        if (corruptFirst && ranges.size == 1) bytes = d.copyOf().also { it[0] = (it[0] + 1).toByte() }
        return if (honorRange && rangeStart > 0) resp(206, bytes.copyOfRange(rangeStart.toInt(), bytes.size)) else resp(200, bytes)
    }
    private fun resp(code: Int, b: ByteArray) = object : HttpResponse {
        override val status = code
        override val body = ByteArrayInputStream(b)
        override fun close() {}
    }
}

private class BlockingHttp : HttpFetcher {
    override fun get(url: String, rangeStart: Long): HttpResponse {
        val closed = java.util.concurrent.CountDownLatch(1)
        return object : HttpResponse {
            override val status = 200
            override val body = object : java.io.InputStream() {
                var sent = false
                override fun read(): Int = throw UnsupportedOperationException()
                override fun read(b: ByteArray, off: Int, len: Int): Int {
                    if (!sent) { sent = true; b.fill(1, off, off + 100); return 100 }
                    closed.await()
                    throw java.io.IOException("closed")
                }
            }
            override fun close() = closed.countDown()
        }
    }
}

class ContentTest {
    private val dir: File = Files.createTempDirectory("syncvr").toFile()
    private val bytes = ByteArray(1000) { (it * 7).toByte() }

    @AfterTest fun cleanup() { dir.deleteRecursively() }

    private fun file(sha: String? = null) = ContentFile("a.mp4", bytes.size.toLong(), "http://x/a", sha)

    @Test fun downloadsAndVerifies() {
        val store = ContentStore(dir, FakeHttp(mapOf("http://x/a" to bytes)))
        val r = store.sync(listOf(file(sha(bytes))), false, null)
        assertEquals(listOf("a.mp4"), r.ok)
        assertTrue(File(dir, "a.mp4").readBytes().contentEquals(bytes))
        assertFalse(File(dir, "a.mp4.part").exists())
        assertEquals(listOf(InventoryEntry("a.mp4", 1000)), store.inventory())
    }

    @Test fun resumesPartialWithRange() {
        File(dir, "a.mp4.part").writeBytes(bytes.copyOf(400))
        val http = FakeHttp(mapOf("http://x/a" to bytes))
        val store = ContentStore(dir, http)
        assertEquals(600L, store.bytesMissing(listOf(file())))
        assertEquals(listOf("a.mp4"), store.sync(listOf(file()), false, null).ok)
        assertEquals(listOf(400L), http.ranges)
        assertTrue(File(dir, "a.mp4").readBytes().contentEquals(bytes))
    }

    @Test fun serverIgnoringRangeRestarts() {
        File(dir, "a.mp4.part").writeBytes(bytes.copyOf(400))
        val store = ContentStore(dir, FakeHttp(mapOf("http://x/a" to bytes), honorRange = false))
        assertEquals(listOf("a.mp4"), store.sync(listOf(file()), false, null).ok)
        assertTrue(File(dir, "a.mp4").readBytes().contentEquals(bytes))
    }

    @Test fun checksumMismatchRetriesOnceThenFails() {
        val http = FakeHttp(mapOf("http://x/a" to bytes)).also { it.corruptFirst = true }
        assertEquals(listOf("a.mp4"), ContentStore(dir, http).sync(listOf(file(sha(bytes))), false, null).ok)
        assertEquals(2, http.ranges.size)

        val bad = ContentStore(dir, FakeHttp(mapOf("http://x/a" to bytes)))
        val r = bad.sync(listOf(file("0".repeat(64))), false, null)
        assertEquals(listOf("a.mp4"), r.failed)
        assertFalse(File(dir, "a.mp4").exists())
    }

    @Test fun existingFileWithWrongChecksumIsRefetched() {
        File(dir, "a.mp4").writeBytes(ByteArray(1000))
        val store = ContentStore(dir, FakeHttp(mapOf("http://x/a" to bytes)))
        assertEquals(listOf("a.mp4"), store.sync(listOf(file(sha(bytes))), false, null).ok)
        assertTrue(File(dir, "a.mp4").readBytes().contentEquals(bytes))
    }

    @Test fun failuresAreReported() {
        val store = ContentStore(dir, FakeHttp(emptyMap()))
        val r = store.sync(listOf(file(), ContentFile("../evil.mp4", 1, "http://x/e")), false, null)
        assertEquals(listOf("a.mp4", "../evil.mp4"), r.failed)
    }

    @Test fun deleteKeepsLoadedAndRejectsUnsafe() {
        for (n in listOf("a.mp4", "b.mp4", "b.mp4.part", "notes.txt")) File(dir, n).writeBytes(ByteArray(3))
        val store = ContentStore(dir)
        assertEquals(listOf("a.mp4"), store.delete(listOf("a.mp4", "b.mp4", "../x"), "a.mp4"))
        assertTrue(File(dir, "a.mp4").exists())
        assertFalse(File(dir, "b.mp4").exists())
        assertFalse(File(dir, "b.mp4.part").exists())
        assertEquals(listOf("a.mp4"), store.inventory().map { it.name })
    }

    @Test fun deleteOthersKeepsWantedAndLoaded() {
        for (n in listOf("keep.mp4", "loaded.mp4", "old.mp4", "old2.mp4.part")) File(dir, n).writeBytes(ByteArray(1000))
        val store = ContentStore(dir, FakeHttp(emptyMap()))
        store.sync(listOf(ContentFile("keep.mp4", 1000, "http://x/k")), true, "loaded.mp4")
        assertEquals(setOf("keep.mp4", "loaded.mp4"), dir.list()!!.toSet())
    }

    @Test fun managerHandlesMessagesAndReplies() {
        val mgr = ContentManager(ContentStore(dir, FakeHttp(mapOf("http://x/a" to bytes))))
        val msg = ServerMessage.parse("""{"type":"sync_content","files":[{"name":"a.mp4","size":1000,"url":"http://x/a"}],"delete_others":false}""")!!
        assertTrue(mgr.handle(msg))
        val deadline = System.currentTimeMillis() + 5000
        while (mgr.outbox.size < 2 && System.currentTimeMillis() < deadline) Thread.sleep(10)
        val out = mgr.outbox.toList()
        assertEquals("""{"type":"inventory","files":[{"name":"a.mp4","size":1000}]}""", out[0])
        assertEquals("""{"type":"downloads_finished","ok":["a.mp4"],"failed":[],"cancelled":false}""", out[1])
        mgr.outbox.clear()
        mgr.handle(ServerMessage.parse("""{"type":"delete_content","names":["a.mp4"]}""")!!)
        assertEquals("""{"type":"inventory","files":[]}""", mgr.outbox.poll())
        assertFalse(mgr.handle(ServerMessage(type = "play")))
    }

    @Test fun notEnoughSpaceFailsEverything() {
        val mgr = ContentManager(ContentStore(dir, FakeHttp(emptyMap())), freeBytes = { 10L })
        mgr.handle(ServerMessage(type = "sync_content", files = listOf(file())))
        assertEquals("""{"type":"downloads_finished","ok":[],"failed":["a.mp4"],"cancelled":false}""", mgr.outbox.poll())
    }

    @Test fun loopbackHttpWithRange() {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        val seen = ArrayList<String?>()
        server.createContext("/content/a.mp4") { ex ->
            val range = ex.requestHeaders.getFirst("Range")
            seen.add(range)
            val start = range?.removePrefix("bytes=")?.removeSuffix("-")?.toInt() ?: 0
            val body = bytes.copyOfRange(start, bytes.size)
            ex.responseHeaders.add("X-Content-SHA256", sha(bytes))
            ex.sendResponseHeaders(if (range != null) 206 else 200, body.size.toLong())
            ex.responseBody.use { it.write(body) }
        }
        server.start()
        try {
            File(dir, "a.mp4.part").writeBytes(bytes.copyOf(250))
            val url = "http://127.0.0.1:${server.address.port}/content/a.mp4"
            val r = ContentStore(dir, JavaHttpFetcher).sync(listOf(ContentFile("a.mp4", 1000, url, sha(bytes))), false, null)
            assertEquals(listOf("a.mp4"), r.ok)
            assertEquals(listOf<String?>("bytes=250-"), seen)
            assertTrue(File(dir, "a.mp4").readBytes().contentEquals(bytes))
        } finally {
            server.stop(0)
        }
    }

    @Test fun abortUnblocksReadAndKeepsPart() {
        val store = ContentStore(dir, BlockingHttp())
        var cancel = false
        val done = java.util.concurrent.CountDownLatch(1)
        var result: SyncResult? = null
        Thread {
            result = store.sync(listOf(file()), false, null, { cancel }) { _, _, _ -> }
            done.countDown()
        }.start()
        Thread.sleep(300) // let the worker block in read()
        assertFalse(done.await(100, java.util.concurrent.TimeUnit.MILLISECONDS))
        cancel = true
        store.abort()
        assertTrue(done.await(2, java.util.concurrent.TimeUnit.SECONDS))
        assertEquals(emptyList(), result!!.failed)
        assertTrue(File(dir, "a.mp4.part").isFile)
    }

    @Test fun secondSyncAbortsBlockedFirst() {
        val mgr = ContentManager(ContentStore(dir, BlockingHttp()))
        val msg = ServerMessage(type = "sync_content", files = listOf(file()))
        mgr.handle(msg)
        Thread.sleep(300)
        mgr.handle(msg)
        val deadline = System.currentTimeMillis() + 2000
        while (mgr.outbox.size < 2 && System.currentTimeMillis() < deadline) Thread.sleep(10)
        assertTrue(mgr.outbox.toList().any { it.contains("downloads_finished") && it.contains("\"cancelled\":true") })
        assertTrue(File(dir, "a.mp4.part").isFile)
        mgr.handle(ServerMessage(type = "cancel_downloads"))
    }
}
