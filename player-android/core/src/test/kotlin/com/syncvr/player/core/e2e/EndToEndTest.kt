package com.syncvr.player.core.e2e

import com.syncvr.player.core.Hello
import com.syncvr.player.core.PlayerController
import com.syncvr.player.core.content.ContentManager
import com.syncvr.player.core.content.ContentStore
import com.syncvr.player.core.net.ConnectionConfig
import com.syncvr.player.core.net.ServerConnection
import com.syncvr.player.core.sync.FakePlayer
import com.syncvr.player.core.sync.Json
import com.syncvr.player.core.sync.LocalClock
import org.junit.jupiter.api.Assumptions.assumeTrue
import java.io.ByteArrayOutputStream
import java.io.File
import java.net.ServerSocket
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.ByteBuffer
import java.nio.file.Files
import java.util.concurrent.TimeUnit
import kotlin.math.abs
import kotlin.test.AfterTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue
import kotlin.test.fail

/**
 * Starts the real Python server (`python3 -m syncvr serve`), joins it with the real
 * ServerConnection + PlayerController and a FakePlayer decoder on a real-time virtual clock, then
 * drives play/pause/seek through the server's HTTP API.
 *
 * Skipped when python3 or the syncvr package (and aiohttp) is unavailable, unless the
 * SYNCVR_E2E_REQUIRED environment variable is set (CI), in which case that is a failure.
 */
class EndToEndTest {
    private val tmp: File = Files.createTempDirectory("syncvr-e2e").toFile()
    private val http = HttpClient.newHttpClient()
    private var server: Process? = null
    private var connection: ServerConnection? = null
    private var loop: Thread? = null
    @Volatile private var running = true

    /** Written only by the loop thread (which owns the player), read by the test thread. */
    private class Sample(val at: Double, val position: Double, val playing: Boolean, val state: String)
    @Volatile private var sample = Sample(0.0, 0.0, false, "idle")

    private val serverDir: File = generateSequence(File("").absoluteFile) { it.parentFile }
        .map { File(it, "server/syncvr") }.firstOrNull { it.isDirectory }?.parentFile ?: File("../../server")

    @AfterTest fun cleanup() {
        running = false
        connection?.stop()
        loop?.join(2000)
        server?.destroy()
        server?.waitFor(5, TimeUnit.SECONDS)
        server?.destroyForcibly()
        tmp.deleteRecursively()
    }

    private fun env(pb: ProcessBuilder) = pb.apply {
        environment()["PYTHONPATH"] = serverDir.absolutePath
        environment()["PYTHONUNBUFFERED"] = "1"
    }

    private fun requireServer() {
        val required = System.getenv("SYNCVR_E2E_REQUIRED") != null
        val ok = try {
            val p = env(ProcessBuilder("python3", "-c", "import aiohttp, syncvr")).redirectErrorStream(true).start()
            p.inputStream.readBytes()
            p.waitFor(30, TimeUnit.SECONDS) && p.exitValue() == 0
        } catch (e: Exception) {
            false
        }
        if (required && !ok) fail("SYNCVR_E2E_REQUIRED is set but python3 with aiohttp and syncvr (in $serverDir) is unavailable")
        assumeTrue(ok, "python3 with aiohttp and the syncvr package is required")
    }

    private fun freePort() = ServerSocket(0).use { it.localPort }

    private fun box(kind: String, payload: ByteArray): ByteArray =
        ByteBuffer.allocate(8 + payload.size).putInt(8 + payload.size).put(kind.toByteArray()).put(payload).array()

    /** Structurally valid but undecodable MP4 with a duration, like make_mp4 in server/tests/conftest.py. */
    private fun fakeMp4(durationSeconds: Int): ByteArray {
        val mvhd = ByteBuffer.allocate(4 + 16 + 80).apply {
            position(4)
            putInt(0); putInt(0); putInt(1000); putInt(durationSeconds * 1000)
        }.array()
        val tkhd = ByteBuffer.allocate(4 + 76 + 8).apply {
            put(3, 3); position(80); putInt(3840 shl 16); putInt(1920 shl 16)
        }.array()
        val hdlr = ByteArrayOutputStream().apply {
            write(ByteArray(8)); write("vide".toByteArray()); write(ByteArray(12)); write("video\u0000".toByteArray())
        }.toByteArray()
        val trak = box("trak", box("tkhd", tkhd) + box("mdia", box("hdlr", hdlr)))
        val moov = box("moov", box("mvhd", mvhd) + trak)
        val ftyp = box("ftyp", "isom".toByteArray() + ByteBuffer.allocate(4).putInt(512).array() + "isomiso2avc1mp41".toByteArray())
        return ftyp + moov + box("mdat", ByteArray(4096) { it.toByte() })
    }

    private fun get(url: String): String =
        http.send(HttpRequest.newBuilder(URI(url)).GET().build(), HttpResponse.BodyHandlers.ofString()).body()

    private fun command(httpPort: Int, json: String) {
        val r = http.send(
            HttpRequest.newBuilder(URI("http://127.0.0.1:$httpPort/api/command"))
                .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(json)).build(),
            HttpResponse.BodyHandlers.ofString(),
        )
        assertEquals(200, r.statusCode(), r.body())
    }

    private fun waitFor(what: String, timeoutMs: Long = 10_000, cond: () -> Boolean) {
        val end = System.nanoTime() + timeoutMs * 1_000_000
        while (System.nanoTime() < end) {
            if (cond()) return
            Thread.sleep(20)
        }
        fail("timed out waiting for $what")
    }

    @Suppress("UNCHECKED_CAST")
    private fun serverView(httpPort: Int): Triple<String, Double, Double>? {
        val st = Json.parseObject(get("http://127.0.0.1:$httpPort/api/state")) ?: return null
        val serverTime = ((st["server"] as Map<String, Any?>)["time"] as Number).toDouble()
        val dev = (st["devices"] as List<Map<String, Any?>>).firstOrNull() ?: return null
        val d = dev["desired"] as? Map<String, Any?> ?: return null
        val mode = d["mode"] as String
        var pos = (d["pos"] as Number).toDouble()
        if (mode == "playing") {
            pos += maxOf(0.0, serverTime - (d["at"] as Number).toDouble())
            (d["duration"] as? Number)?.toDouble()?.let { if (it > 0) pos = minOf(pos, it) }
        }
        return Triple(mode, pos, serverTime)
    }

    /** Client position minus the server's expected position, in seconds. */
    private fun errorNow(httpPort: Int): Double {
        val (_, serverPos, _) = serverView(httpPort)!!
        val s = sample
        val clientPos = s.position + if (s.playing) LocalClock.now - s.at else 0.0
        return clientPos - serverPos
    }

    private fun assertInSync(httpPort: Int, label: String, toleranceMs: Double = 150.0, seconds: Double = 2.0) {
        var worst = 0.0
        val end = System.nanoTime() + (seconds * 1e9).toLong()
        while (System.nanoTime() < end) {
            worst = maxOf(worst, abs(errorNow(httpPort)) * 1000)
            Thread.sleep(100)
        }
        assertTrue(worst < toleranceMs, "$label: client drifted ${"%.1f".format(worst)} ms from the server (limit $toleranceMs)")
    }

    @Test fun clientFollowsPlayPauseSeek() {
        requireServer()
        val content = File(tmp, "content").apply { mkdirs() }
        File(content, "e2e_360_TB.mp4").writeBytes(fakeMp4(300))
        val httpPort = freePort()
        val tcpPort = freePort()
        val log = File(tmp, "server.log")
        server = env(ProcessBuilder(
            "python3", "-m", "syncvr", "serve", "--content", content.path, "--data", File(tmp, "data").path,
            "--host", "127.0.0.1", "--http-port", "$httpPort", "--tcp-port", "$tcpPort", "--no-discovery",
        )).redirectErrorStream(true).redirectOutput(log).start()
        waitFor("server HTTP API") {
            check(server!!.isAlive) { "server exited:\n" + log.readText() }
            try { get("http://127.0.0.1:$httpPort/api/state"); true } catch (e: Exception) { false }
        }

        val conn = ServerConnection(
            ConnectionConfig(server = "127.0.0.1", port = tcpPort),
            Hello.build("e2e-1", "E2E1", "Fake", "test"),
        )
        connection = conn
        val player = FakePlayer(videoLength = 300.0)
        val ctl = PlayerController(
            player, ContentManager(ContentStore(File(tmp, "videos")), inUse = { player.loadedVideo }),
            conn.clock, conn.inbox, conn::send, { conn.connected },
        )
        loop = Thread {
            while (running) {
                val now = LocalClock.now
                player.tick(now)
                ctl.tick()
                player.tick(LocalClock.now)
                sample = Sample(LocalClock.now, player.truePosition, player.isPlaying, ctl.engine.state)
                Thread.sleep(10)
            }
        }.apply { isDaemon = true; start() }
        player.tick(LocalClock.now)
        conn.start()

        waitFor("headset to register and sync its clock") {
            val st = get("http://127.0.0.1:$httpPort/api/state")
            conn.connected && conn.clock.synced && st.contains("e2e-1")
        }

        command(httpPort, """{"action":"play","video":"e2e_360_TB.mp4"}""")
        waitFor("playing") { sample.state == "playing" && sample.playing }
        Thread.sleep(3000) // let the engine settle (first start on a fresh device)
        assertInSync(httpPort, "playing")
        assertTrue(sample.position > 2.0, "position advanced")

        command(httpPort, """{"action":"pause"}""")
        waitFor("paused") { sample.state == "paused" && !sample.playing }
        Thread.sleep(500)
        assertEquals("paused", serverView(httpPort)!!.first)
        assertInSync(httpPort, "paused", toleranceMs = 50.0, seconds = 1.0)

        command(httpPort, """{"action":"seek","pos":120.0}""")
        waitFor("seek while paused") { abs(sample.position - 120.0) < 0.01 }
        command(httpPort, """{"action":"play"}""")
        waitFor("playing after seek") { sample.state == "playing" && sample.playing }
        Thread.sleep(3000)
        assertInSync(httpPort, "playing after seek")
        assertTrue(sample.position in 120.0..130.0, "position ${sample.position}")

        command(httpPort, """{"action":"seek","delta":-60.0}""")
        Thread.sleep(3500)
        assertInSync(httpPort, "playing after seek while playing")
        assertTrue(sample.position in 60.0..70.0, "position ${sample.position}")

        command(httpPort, """{"action":"stop"}""")
        waitFor("idle") { sample.state == "idle" }
    }
}
