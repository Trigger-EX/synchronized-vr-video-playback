package com.syncvr.player.core

import com.syncvr.player.core.content.ContentManager
import com.syncvr.player.core.content.ContentStore
import com.syncvr.player.core.content.HttpFetcher
import com.syncvr.player.core.content.HttpResponse
import com.syncvr.player.core.sync.ClockSync
import com.syncvr.player.core.sync.FakePlayer
import com.syncvr.player.core.sync.Json
import com.syncvr.player.core.sync.ServerMessage
import com.syncvr.player.core.sync.VideoCommand
import java.io.ByteArrayInputStream
import java.io.File
import java.nio.file.Files
import java.util.concurrent.ConcurrentLinkedQueue
import kotlin.test.AfterTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

private class RecordingHost : PlayerHost {
    val commands = ArrayList<VideoCommand>()
    val volumes = ArrayList<Double>()
    var recenters = 0
    val messages = ArrayList<Pair<String?, Double>>()
    val identifies = ArrayList<Pair<String, Double>>()
    val names = ArrayList<Pair<String, String>>()
    override fun onVideoCommand(cmd: VideoCommand) { commands.add(cmd) }
    override fun setVolume(volume: Double) { volumes.add(volume) }
    override fun recenter() { recenters++ }
    override fun showMessage(text: String?, seconds: Double) { messages.add(text to seconds) }
    override fun identify(name: String, seconds: Double) { identifies.add(name to seconds) }
    override fun onNames(serverName: String, deviceName: String) { names.add(serverName to deviceName) }
}

private class BytesHttp(val data: ByteArray) : HttpFetcher {
    override fun get(url: String, rangeStart: Long): HttpResponse = object : HttpResponse {
        override val status = 200
        override val body = ByteArrayInputStream(data)
        override fun close() {}
    }
}

class PlayerControllerTest {
    private val dir: File = Files.createTempDirectory("syncvr-ctl").toFile()
    private val player = FakePlayer()
    private val clock = ClockSync()
    private val inbox = ConcurrentLinkedQueue<ServerMessage>()
    private val sent = ArrayList<String>()
    private val host = RecordingHost()
    private var connected = true
    private var t = 1000.0
    private val bytes = ByteArray(500) { it.toByte() }
    private val content = ContentManager(ContentStore(dir, BytesHttp(bytes)), inUse = { player.loadedVideo })
    private val ctl = PlayerController(
        player, content, clock, inbox, { sent.add(it) }, { connected }, host, { t },
        telemetry = { null },
    )

    @AfterTest fun cleanup() { dir.deleteRecursively() }

    private fun msg(json: String) = ServerMessage.parse(json)!!
    private fun feed(json: String) { inbox.add(msg(json)); ctl.tick() }
    private fun sentOfType(type: String) = sent.map { Json.parseObject(it)!! }.filter { it["type"] == type }

    @Test fun welcomeStoresNamesAndSettings() {
        feed("""{"type":"welcome","server_name":"S","device_name":"Go 1","settings":{"play_lead_ms":2000}}""")
        assertEquals("S", ctl.serverName)
        assertEquals("Go 1", ctl.deviceName)
        assertEquals(2000.0, ctl.engine.settings.playLeadMs)
        feed("""{"type":"device_info","device_name":"Go 2"}""")
        assertEquals("Go 2", ctl.deviceName)
        assertEquals(listOf("S" to "Go 1", "S" to "Go 2"), host.names)
    }

    @Test fun pongFeedsClock() {
        t = 10.0
        feed("""{"type":"time_pong","id":1,"t0":9.9,"ts":500.0}""")
        assertTrue(clock.synced)
        assertEquals(500.0 - 9.95, clock.offset, 1e-6)
    }

    @Test fun playAndPauseReachEngineAndHost() {
        feed("""{"type":"play","video":"a.mp4","pos":5,"at":1001,"loop":true}""")
        assertEquals("a.mp4", player.loadedVideo)
        assertEquals("loading", ctl.engine.state)
        assertEquals("a.mp4", host.commands.single().video)
        assertNotNull(ctl.engine.anchor)
        feed("""{"type":"pause","video":"a.mp4","pos":7,"at":1001}""")
        assertEquals(2, host.commands.size)
        assertEquals(7.0, ctl.engine.pauseRequest!!.pos)
    }

    @Test fun stopUnloads() {
        feed("""{"type":"pause","video":"a.mp4","pos":0,"at":0}""")
        feed("""{"type":"stop"}""")
        assertNull(player.loadedVideo)
        assertEquals("idle", ctl.engine.state)
    }

    @Test fun engineRunsOnlyOnceClockIsSynced() {
        feed("""{"type":"pause","video":"a.mp4","pos":0,"at":0}""")
        player.tick(t + 1.0)
        t += 1.0
        ctl.tick()
        assertEquals("loading", ctl.engine.state) // no clock yet, engine not stepped
        clock.add(t, t + 50, t)
        ctl.tick()
        assertEquals("paused", ctl.engine.state)
    }

    @Test fun operatorCommandsGoToHost() {
        feed("""{"type":"volume","value":1.7}""")
        assertEquals(listOf(1.0), host.volumes)
        assertEquals(1.0, ctl.volume)
        feed("""{"type":"recenter"}""")
        assertEquals(1, host.recenters)
        feed("""{"type":"message","text":"hi","seconds":3}""")
        assertEquals(listOf<Pair<String?, Double>>("hi" to 3.0), host.messages)
        feed("""{"type":"welcome","device_name":"Go 1"}""")
        feed("""{"type":"identify","seconds":0}""")
        feed("""{"type":"identify","name":"X","seconds":2}""")
        assertEquals(listOf("Go 1" to 8.0, "X" to 2.0), host.identifies)
    }

    @Test fun connectSendsInventoryThenStatusOncePerSecond() {
        File(dir, "a.mp4").writeBytes(ByteArray(10))
        feed("""{"type":"_connected"}""")
        assertEquals("inventory", Json.parseObject(sent[0])!!["type"])
        assertEquals(1, sentOfType("status").size)
        t += 0.5; ctl.tick()
        assertEquals(1, sentOfType("status").size)
        t += 0.6; ctl.tick()
        assertEquals(2, sentOfType("status").size)
    }

    @Test fun noStatusWhileDisconnected() {
        connected = false
        ctl.tick()
        assertTrue(sent.isEmpty())
    }

    @Test fun statusCarriesEngineAndLinkFields() {
        feed("""{"type":"volume","value":0.5}""")
        val s = sentOfType("status").single()
        assertEquals("idle", s["state"])
        assertEquals(0.5, s["volume"])
        assertEquals(false, s["clock_synced"])
        assertNull(s["rtt_ms"])
        assertTrue(s.containsKey("download"))
        clock.add(t - 0.02, t + 5, t)
        t += 1.0; ctl.tick()
        val s2 = sentOfType("status").last()
        assertEquals(true, s2["clock_synced"])
        assertEquals(20.0, s2["rtt_ms"] as Double, 1e-6)
    }

    @Test fun statusIncludesTelemetryAndFps() {
        val c = PlayerController(
            player, content, clock, inbox, { sent.add(it) }, { connected }, host, { t },
            telemetry = { Telemetry(battery = 0.4, charging = true, tempC = 30.0, storageFree = 123, wifiRssi = -60, worn = false) },
            fps = { 72.0 },
        )
        c.tick()
        val s = sentOfType("status").single()
        assertEquals(0.4, s["battery"])
        assertEquals(true, s["charging"])
        assertEquals(30.0, s["temp_c"])
        assertEquals(123.0, s["storage_free"])
        assertEquals(-60.0, s["wifi_rssi"])
        assertEquals(false, s["worn"])
        assertEquals(72.0, s["fps"])
    }

    @Test fun queuedEventsAreFlushed() {
        ctl.queueEvent("warn", "hello \"x\"")
        ctl.tick()
        val e = sentOfType("event").single()
        assertEquals("warn", e["level"])
        assertEquals("hello \"x\"", e["message"])
    }

    @Test fun deleteContentRepliesWithInventoryAndKeepsLoadedVideo() {
        File(dir, "a.mp4").writeBytes(ByteArray(10))
        File(dir, "b.mp4").writeBytes(ByteArray(10))
        feed("""{"type":"pause","video":"a.mp4","pos":0,"at":0}""")
        sent.clear()
        feed("""{"type":"delete_content","names":["a.mp4","b.mp4"]}""")
        assertTrue(File(dir, "a.mp4").exists())
        assertTrue(!File(dir, "b.mp4").exists())
        val inv = sentOfType("inventory").single()
        assertEquals(1, (inv["files"] as List<*>).size)
    }

    @Test fun syncContentDownloadsAndFlushesOutbox() {
        val json = """{"type":"sync_content","files":[{"name":"n.mp4","size":500,"url":"http://x/n"}],"delete_others":false}"""
        feed(json)
        val end = System.currentTimeMillis() + 5000
        while (sentOfType("downloads_finished").isEmpty() && System.currentTimeMillis() < end) {
            Thread.sleep(5)
            ctl.tick()
        }
        val done = sentOfType("downloads_finished").single()
        assertEquals(listOf("n.mp4"), done["ok"])
        assertTrue(File(dir, "n.mp4").exists())
        assertTrue(sentOfType("inventory").isNotEmpty())
    }

    @Test fun cancelDownloadsIsRoutedToContent() {
        feed("""{"type":"cancel_downloads"}""") // must not throw or reply
        assertTrue(sentOfType("downloads_finished").isEmpty())
    }
}
