package com.syncvr.player.core.operator

import com.syncvr.player.core.net.Datagram
import com.syncvr.player.core.net.DiscoveryGuard
import com.syncvr.player.core.net.UdpReceiver
import com.syncvr.player.core.net.UdpReceiverFactory
import com.syncvr.player.core.sync.Json
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

internal const val STATE_JSON = """
{"server":{"name":"Lobby","time":1000.5,"http_port":8080,"addresses":["192.168.1.2"]},
 "settings":{"play_lead_ms":1500},
 "devices":[
  {"id":"a1","name":"","label":"Go 1","group":"left","online":true,"volume":0.5,"last_seen":1.0,
   "status":{"state":"playing","video":"a.mp4","position":12.5,"duration":100.0,"drift_ms":-4.0,"battery":0.8,
             "download":{"name":"b.mp4","received":50,"total":200}},
   "inventory":{"a.mp4":1000,"b.mp4":5},
   "desired":{"mode":"playing","video":"a.mp4","pos":10.0,"at":999.0,"duration":100.0,"loop":true}},
  {"id":"b2","name":"Two","label":"Two","group":"","online":false,"volume":1.0,"last_seen":0,"status":{},"inventory":{},"desired":null}
 ],
 "library":[{"name":"a.mp4","title":"Alpha","size":1000,"duration":100.0},{"name":"b.mp4","title":"","size":2000,"duration":null}],
 "downloads":{"max_concurrent":4,"queued":["b2"],"active":["a1"],"idle":[]},
 "events":[{"t":5.0,"level":"warn","message":"hello","device":"a1"},{"t":6.0,"level":"info","message":"x"}]}
"""

class FakeTransport(private val replies: MutableList<HttpReply> = mutableListOf()) : HttpTransport {
    class Call(val method: String, val url: String, val headers: Map<String, String>, val body: String?)
    val calls = mutableListOf<Call>()
    fun reply(status: Int, body: String) = replies.add(HttpReply(status, body))
    override fun request(method: String, url: String, headers: Map<String, String>, body: String?): HttpReply {
        calls += Call(method, url, headers, body)
        return if (replies.isEmpty()) HttpReply(200, "{}") else replies.removeAt(0)
    }
}

class StateModelTest {
    @Test fun parsesSnapshot() {
        val s = assertNotNull(Snapshot.parse(STATE_JSON))
        assertEquals("Lobby", s.serverName)
        assertEquals(2, s.devices.size)
        val a = s.devices[0]
        assertEquals("Go 1", a.label)
        assertEquals("playing", a.stateText)
        assertEquals(12.5, a.status.position)
        assertEquals(0.25, a.status.downloadFraction)
        assertEquals("a.mp4", a.desired?.video)
        assertTrue(a.desired!!.loop)
        assertEquals("offline", s.devices[1].stateText)
        assertNull(s.devices[1].desired)
        assertEquals("Alpha", s.library[0].displayName)
        assertEquals("b.mp4", s.library[1].displayName)
        assertNull(s.library[1].duration)
        assertEquals(1, s.haveCount(s.library[0]))
        assertEquals(0, s.haveCount(s.library[1]))
        assertTrue(s.downloads.busy)
        assertEquals(2, s.events.size)
        assertNull(s.events[1].device)
    }

    @Test fun rejectsGarbage() {
        assertNull(Snapshot.parse("nope"))
        assertNull(Snapshot.parse("{}"))
    }

    @Test fun formatting() {
        assertEquals("1:05", formatTime(65.4))
        assertEquals("1:01:01", formatTime(3661.0))
        assertEquals("-", formatTime(null))
        assertEquals("1.5 KB", formatBytes(1536))
        assertEquals("12 B", formatBytes(12))
    }
}

class OperatorApiTest {
    @Test fun parsesAddresses() {
        assertEquals(ServerEndpoint("192.168.1.5", 8080), ServerEndpoint.parse(" 192.168.1.5 "))
        assertEquals(ServerEndpoint("h", 9000, "pw"), ServerEndpoint.parse("http://h:9000/", "pw"))
        assertNull(ServerEndpoint.parse(""))
        assertNull(ServerEndpoint.parse("a b"))
        assertNull(ServerEndpoint.parse("h:99999"))
        assertNull(ServerEndpoint.parse("h:abc"))
        assertEquals("http://1.2.3.4:8080", ServerEndpoint("1.2.3.4").baseUrl)
    }

    @Test fun sendsBasicAuthOnlyWithPassword() {
        val t = FakeTransport()
        t.reply(200, STATE_JSON); t.reply(200, STATE_JSON)
        OperatorApi(ServerEndpoint("h", 1, "secret"), t).state()
        OperatorApi(ServerEndpoint("h", 1), t).state()
        assertEquals("Basic OnNlY3JldA==", t.calls[0].headers["Authorization"]) // base64(":secret")
        assertNull(t.calls[1].headers["Authorization"])
        assertEquals("http://h:1/api/state", t.calls[0].url)
        assertEquals("GET", t.calls[0].method)
    }

    @Test fun basicAuthPadding() {
        assertEquals("Basic OnA=", basicAuthHeader("p"))   // ":p"
        assertEquals("Basic OnB3", basicAuthHeader("pw"))  // ":pw"
        assertEquals("Basic OnB3ZA==", basicAuthHeader("pwd")) // ":pwd"
    }

    @Test fun commandBodies() {
        val t = FakeTransport()
        t.reply(200, """{"ok":true,"result":{"targets":2,"online":1,"warning":"careful"}}""")
        val api = OperatorApi(ServerEndpoint("h"), t)
        val r = api.command("seek", Targets.Ids(listOf("a", "b")), mapOf("pos" to 12.5, "loop" to true))
        assertEquals(CommandResult(2, 1, "careful"), r)
        val body = Json.parseObject(t.calls[0].body!!)!!
        assertEquals("seek", body["action"])
        assertEquals(listOf("a", "b"), body["targets"])
        assertEquals(12.5, body["pos"])
        assertEquals(true, body["loop"])
        assertEquals("POST", t.calls[0].method)
        assertEquals("""{"action":"stop","targets":"all"}""", JsonWriter.write(OperatorApi.commandBody("stop", Targets.All, emptyMap())))
        assertEquals("""{"a":"q\"\n","b":[1,2.5,null],"c":3}""", JsonWriter.write(mapOf("a" to "q\"\n", "b" to listOf(1, 2.5, null), "c" to 3.0)))
    }

    @Test fun errors() {
        val t = FakeTransport()
        t.reply(401, ""); t.reply(400, """{"error":"no headsets match the selected targets"}""")
        val api = OperatorApi(ServerEndpoint("h"), t)
        assertTrue(assertFailsWith<ApiException> { api.state() }.unauthorized)
        val e = assertFailsWith<ApiException> { api.command("play", Targets.All) }
        assertEquals("no headsets match the selected targets", e.message)
        assertFalse(e.unauthorized)
    }

    @Test fun deviceAndLibraryEndpoints() {
        val t = FakeTransport()
        val api = OperatorApi(ServerEndpoint("h"), t)
        api.updateDevice("a/b c", name = "N", group = "G")
        api.forgetDevice("x")
        api.rescanLibrary()
        assertEquals("http://h:8080/api/devices/a%2Fb%20c", t.calls[0].url)
        assertEquals("""{"name":"N","group":"G"}""", t.calls[0].body)
        assertEquals("DELETE", t.calls[1].method)
        assertEquals("http://h:8080/api/library/rescan", t.calls[2].url)
    }
}

private class FakeUdp(private val datagrams: List<Datagram>) : UdpReceiverFactory {
    var closed = false
    override fun open(port: Int): UdpReceiver {
        val it = datagrams.iterator()
        return object : UdpReceiver {
            override fun receive(timeoutMs: Int) = if (it.hasNext()) it.next() else null
            override fun close() { closed = true }
        }
    }
}

class ServerFinderTest {
    private fun beacon(service: String = "syncvr", name: String = "Lobby", http: Int = 8080) =
        """{"type":"beacon","service":"$service","proto":1,"server_name":"$name","tcp_port":8765,"http_port":$http}"""
    private fun dg(text: String, from: String = "10.0.0.2") = Datagram(text.toByteArray(), from)

    @Test fun collectsDistinctServersAndIgnoresNoise() {
        val udp = FakeUdp(listOf(
            dg("junk"), dg(beacon(service = "other")), dg(beacon(http = 0)),
            dg(beacon()), dg(beacon()), dg(beacon(name = "B"), "10.0.0.3"),
        ))
        var acquired = 0
        var released = 0
        val guard = object : DiscoveryGuard {
            override fun acquire() { acquired++ }
            override fun release() { released++ }
        }
        val seen = mutableListOf<FoundServer>()
        val found = ServerFinder(udp, guard = guard).scan(1000, onFound = { seen += it })
        assertEquals(listOf(FoundServer("10.0.0.2", 8080, "Lobby"), FoundServer("10.0.0.3", 8080, "B")), found)
        assertEquals(found, seen)
        assertTrue(udp.closed)
        assertEquals(1, acquired); assertEquals(1, released)
        assertEquals(ServerEndpoint("10.0.0.2", 8080), found[0].endpoint)
    }
}

class OperatorViewModelTest {
    private val transport = FakeTransport()
    private val states = mutableListOf<OperatorUiState>()
    private fun vm(): OperatorViewModel = OperatorViewModel(OperatorApi(ServerEndpoint("h"), transport), { it() }, { states += it })

    private fun loaded(): OperatorViewModel {
        transport.reply(200, STATE_JSON)
        return vm().also { it.refresh() }
    }

    private fun lastBody() = Json.parseObject(transport.calls.last { it.method == "POST" }.body!!)!!

    @Test fun refreshPublishesSnapshot() {
        val vm = loaded()
        assertTrue(vm.state.connected)
        assertEquals(1, vm.state.onlineCount)
        assertEquals(1, vm.state.playingCount)
        assertEquals("a.mp4", vm.state.effectiveVideo) // what the headsets play
    }

    @Test fun failedRefreshKeepsLastSnapshotAndFlagsPassword() {
        val vm = loaded()
        transport.reply(401, "")
        vm.refresh()
        assertFalse(vm.state.connected)
        assertTrue(vm.state.unauthorized)
        assertNotNull(vm.state.snapshot)
    }

    @Test fun selectionDrivesTargetsAndIsPruned() {
        val vm = loaded()
        assertEquals(Targets.All, vm.state.targets)
        vm.toggleSelected("a1"); vm.toggleSelected("gone")
        assertEquals(Targets.Ids(listOf("a1", "gone")), vm.state.targets)
        transport.reply(200, STATE_JSON)
        vm.refresh()
        assertEquals(setOf("a1"), vm.state.selected)
        vm.selectOnline()
        assertEquals(setOf("a1"), vm.state.selected)
        vm.clearSelection()
        assertEquals(Targets.All, vm.state.targets)
    }

    @Test fun transportCommands() {
        val vm = loaded()
        vm.toggleSelected("a1")
        vm.pause()
        assertEquals("pause", lastBody()["action"])
        assertEquals(listOf("a1"), lastBody()["targets"])
        vm.seekTo(500.0) // clamped to the focus device's duration
        assertEquals(100.0, lastBody()["pos"])
        vm.seekBy(-10.0)
        assertEquals(-10.0, lastBody()["delta"])
        vm.setVolume(1.7)
        assertEquals(1.0, lastBody()["value"])
        vm.resync(); assertEquals("resync", lastBody()["action"])
        vm.identify(); assertEquals("identify", lastBody()["action"])
        vm.stop(); assertEquals("stop", lastBody()["action"])
        assertEquals(12.5, vm.state.seekPosition)
        assertEquals(100.0, vm.state.seekDuration)
    }

    @Test fun loadAndPlayUseTheChosenVideo() {
        val vm = loaded()
        vm.selectVideo("b.mp4")
        vm.playVideo()
        assertEquals("play", lastBody()["action"])
        assertEquals("b.mp4", lastBody()["video"])
        assertEquals("all", lastBody()["targets"])
        vm.load("a.mp4")
        assertEquals("load", lastBody()["action"])
        assertEquals("a.mp4", vm.state.selectedVideo)
    }

    @Test fun libraryDistribution() {
        val vm = loaded()
        vm.syncContent()
        assertEquals("sync_content", lastBody()["action"])
        assertEquals("all", lastBody()["videos"])
        assertEquals(false, lastBody()["delete_others"])
        vm.syncContent(listOf("a.mp4"), deleteOthers = true)
        assertEquals(listOf("a.mp4"), lastBody()["videos"])
        assertEquals(true, lastBody()["delete_others"])
        vm.cancelDownloads(); assertEquals("cancel_downloads", lastBody()["action"])
        val before = transport.calls.size
        vm.deleteContent(emptyList())
        assertEquals(before, transport.calls.size)
        assertTrue(vm.state.noticeIsError)
    }

    @Test fun serverWarningAndErrorsBecomeNotices() {
        val vm = loaded()
        transport.reply(200, """{"ok":true,"result":{"targets":1,"online":0,"warning":"No targeted headset is online"}}""")
        vm.play()
        assertEquals("No targeted headset is online", vm.state.notice)
        assertTrue(vm.state.noticeIsError)
        vm.consumeNotice()
        assertNull(vm.state.notice)
        transport.reply(400, """{"error":"choose a video to play"}""")
        vm.play()
        assertEquals("choose a video to play", vm.state.notice)
    }

    @Test fun playWithoutVideoIsRefusedLocally() {
        transport.reply(200, STATE_JSON.replace(Regex("\"library\":\\[.*?\\],\\s*\"downloads\""), "\"library\":[],\"downloads\""))
        val vm = vm().also { it.refresh() }
        val calls = transport.calls.size
        vm.playVideo()
        assertEquals(calls, transport.calls.size)
        assertEquals("Choose a video first", vm.state.notice)
    }
}
