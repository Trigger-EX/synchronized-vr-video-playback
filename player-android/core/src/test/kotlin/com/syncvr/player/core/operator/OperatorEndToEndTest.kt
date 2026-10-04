package com.syncvr.player.core.operator

import com.syncvr.player.core.Hello
import com.syncvr.player.core.net.ConnectionConfig
import com.syncvr.player.core.net.ServerConnection
import org.junit.jupiter.api.Assumptions.assumeTrue
import java.io.ByteArrayOutputStream
import java.io.File
import java.net.ServerSocket
import java.nio.ByteBuffer
import java.nio.file.Files
import java.util.concurrent.TimeUnit
import kotlin.test.AfterTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotNull
import kotlin.test.assertTrue
import kotlin.test.fail

/**
 * Drives the real Python server through [OperatorApi] / [OperatorViewModel]: a bare ServerConnection
 * stands in for a headset (it registers, which is all the server needs to accept commands).
 * Same skip rules as e2e/EndToEndTest: skipped without python3+aiohttp unless SYNCVR_E2E_REQUIRED is set.
 */
class OperatorEndToEndTest {
    private val tmp: File = Files.createTempDirectory("syncvr-operator-e2e").toFile()
    private var server: Process? = null
    private var connection: ServerConnection? = null

    private val serverDir: File = generateSequence(File("").absoluteFile) { it.parentFile }
        .map { File(it, "server/syncvr") }.firstOrNull { it.isDirectory }?.parentFile ?: File("../../server")

    @AfterTest fun cleanup() {
        connection?.stop()
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

    private fun waitFor(what: String, timeoutMs: Long = 10_000, cond: () -> Boolean) {
        val end = System.nanoTime() + timeoutMs * 1_000_000
        while (System.nanoTime() < end) {
            if (cond()) return
            Thread.sleep(20)
        }
        fail("timed out waiting for $what")
    }

    private fun startServer(extra: List<String> = emptyList()): Pair<Int, Int> {
        val content = File(tmp, "content").apply { mkdirs() }
        File(content, "op_360_TB.mp4").writeBytes(fakeMp4(300))
        val httpPort = freePort()
        val tcpPort = freePort()
        val log = File(tmp, "server.log")
        server = env(ProcessBuilder(
            listOf(
                "python3", "-m", "syncvr", "serve", "--content", content.path, "--data", File(tmp, "data").path,
                "--host", "127.0.0.1", "--http-port", "$httpPort", "--tcp-port", "$tcpPort", "--no-discovery",
            ) + extra,
        )).redirectErrorStream(true).redirectOutput(log).start()
        return httpPort to tcpPort
    }

    private fun waitForServer(api: OperatorApi) = waitFor("server HTTP API") {
        check(server!!.isAlive) { "server exited:\n" + File(tmp, "server.log").readText() }
        try { api.state(); true } catch (e: ApiException) { e.unauthorized } catch (e: Exception) { false }
    }

    @Test fun operatorControlsTheRealServer() {
        requireServer()
        val (httpPort, tcpPort) = startServer()
        val api = OperatorApi(ServerEndpoint("127.0.0.1", httpPort))
        waitForServer(api)

        val conn = ServerConnection(ConnectionConfig(server = "127.0.0.1", port = tcpPort), Hello.build("op-1", "OP1", "Fake", "test"))
        connection = conn
        conn.start()
        waitFor("headset to register") { api.state().devices.any { it.id == "op-1" && it.online } }

        val snap = api.state()
        assertEquals(listOf("op_360_TB.mp4"), snap.library.map { it.name })
        assertEquals(300.0, snap.library[0].duration)

        val vm = OperatorViewModel(api, { it() }, { })
        vm.refresh()
        assertTrue(vm.state.connected)
        assertEquals("op_360_TB.mp4", vm.state.effectiveVideo)

        vm.load()
        assertEquals("paused", assertNotNull(api.state().device("op-1")?.desired).mode)
        vm.playVideo()
        assertEquals("playing", api.state().device("op-1")!!.desired!!.mode)
        vm.toggleSelected("op-1")
        vm.seekTo(120.0)
        assertEquals(120.0, api.state().device("op-1")!!.desired!!.pos, 5.0) // +lead while playing
        vm.pause()
        assertEquals("paused", api.state().device("op-1")!!.desired!!.mode)
        vm.setVolume(0.25)
        assertEquals(0.25, api.state().device("op-1")!!.volume)
        vm.identify(); vm.resync()
        vm.syncContent()
        vm.stop()
        assertEquals("stopped", api.state().device("op-1")!!.desired!!.mode)
        assertEquals(null, vm.state.notice, "no command failed")

        api.updateDevice("op-1", name = "Front", group = "A")
        assertEquals("Front", api.state().device("op-1")!!.label)
        api.rescanLibrary()
        val err = assertFailsWith<ApiException> { api.command("play", Targets.Ids(listOf("nope")), mapOf("video" to "op_360_TB.mp4")) }
        assertTrue(err.message!!.contains("unknown headset"), err.message)
    }
}
