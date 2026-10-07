package com.syncvr.player.core.content

import com.syncvr.player.core.sync.ServerMessage
import java.io.ByteArrayInputStream
import java.io.IOException
import java.io.InputStream
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

private class FakeProbeHttp(val status: Int = 200, val size: Int = 0, val failAfter: Int = -1) : HttpFetcher {
    var urls = ArrayList<String>()
    override fun get(url: String, rangeStart: Long): HttpResponse {
        urls.add(url)
        return object : HttpResponse {
            override val status = this@FakeProbeHttp.status
            override val body: InputStream = if (failAfter < 0) ByteArrayInputStream(ByteArray(size)) else object : InputStream() {
                var sent = 0
                override fun read(): Int = throw UnsupportedOperationException()
                override fun read(b: ByteArray, off: Int, len: Int): Int {
                    if (sent >= failAfter) throw IOException("connection reset")
                    val n = minOf(len, failAfter - sent)
                    sent += n
                    return n
                }
            }
            override fun close() {}
        }
    }
}

/** A clock that advances [stepNs] per reading. */
private fun ticking(stepNs: Long): () -> Long {
    var t = 0L
    return { t.also { t += stepNs } }
}

class BandwidthProbeTest {
    @Test fun readsRequestedBytesAndComputesMbps() {
        val http = FakeProbeHttp(size = 1_000_000)
        // start reading, then one reading per loop check and one at the end; 0.5 s per reading
        val result = BandwidthProbe(http, ticking(500_000_000)).run("http://x/v", 400_000, 30.0)
        assertTrue(result.ok)
        assertEquals(400_000L, result.bytes)
        assertEquals(listOf("http://x/v"), http.urls)
        assertTrue(result.mbps > 0.0)
        assertEquals(result.bytes * 8 / result.seconds / 1e6, result.mbps, 1e-9)
    }

    @Test fun stopsAtEndOfStreamWithWhatItGot() {
        val result = BandwidthProbe(FakeProbeHttp(size = 100_000), ticking(100_000_000)).run("http://x/v", 5_000_000, 30.0)
        assertTrue(result.ok)
        assertEquals(100_000L, result.bytes)
    }

    @Test fun stopsAtTheDeadline() {
        val result = BandwidthProbe(FakeProbeHttp(size = 10_000_000), ticking(2_000_000_000)).run("http://x/v", 10_000_000, 5.0)
        assertTrue(result.ok)
        assertTrue(result.bytes < 10_000_000)
    }

    @Test fun httpErrorIsReported() {
        val result = BandwidthProbe(FakeProbeHttp(status = 404), ticking(1000)).run("http://x/v", 1000, 30.0)
        assertFalse(result.ok)
        assertEquals("HTTP 404", result.error)
    }

    @Test fun emptyBodyAndIoErrorsFail() {
        assertFalse(BandwidthProbe(FakeProbeHttp(size = 0), ticking(1000)).run("u", 1000, 30.0).ok)
        val broken = BandwidthProbe(FakeProbeHttp(failAfter = 0), ticking(1000)).run("u", 1000, 30.0)
        assertFalse(broken.ok)
        assertEquals("connection reset", broken.error)
    }

    @Test fun resultJsonEchoesTheJob() {
        val json = BandwidthResult(true, 2000, 0.5, 0.032).toJson("\"j1\"")
        assertEquals("""{"type":"bandwidth_result","job":"j1","ok":true,"bytes":2000,"seconds":0.5,"mbps":0.03,"error":""}""", json)
    }

    @Test fun parsesBandwidthTest() {
        val m = ServerMessage.parse("""{"type":"bandwidth_test","job":"j1","url":"http://h/content/a.mp4","bytes":1048576,"seconds":30}""")!!
        assertEquals("http://h/content/a.mp4", m.url)
        assertEquals(1048576L, m.bytes)
        assertEquals(30.0, m.seconds)
        assertEquals("\"j1\"", m.job)
    }

    @Test fun managerAnswersBandwidthTestAndRefusesBadOnes() {
        val dir = java.nio.file.Files.createTempDirectory("bw").toFile()
        try {
            val mgr = ContentManager(ContentStore(dir, FakeProbeHttp()), probe = BandwidthProbe(FakeProbeHttp(size = 300_000), ticking(100_000_000)))
            assertTrue(mgr.handle(ServerMessage(type = "bandwidth_test", url = "", job = "\"j0\"")))
            assertTrue(mgr.outbox.poll().contains("\"ok\":false"))
            mgr.handle(ServerMessage.parse("""{"type":"bandwidth_test","job":"j1","url":"http://x/v","bytes":2000000,"seconds":30}""")!!)
            val deadline = System.currentTimeMillis() + 5000
            while (mgr.outbox.isEmpty() && System.currentTimeMillis() < deadline) Thread.sleep(10)
            val line = mgr.outbox.poll()
            assertTrue(line.startsWith("""{"type":"bandwidth_result","job":"j1","ok":true,"bytes":300000"""), line)
        } finally {
            dir.deleteRecursively()
        }
    }
}
