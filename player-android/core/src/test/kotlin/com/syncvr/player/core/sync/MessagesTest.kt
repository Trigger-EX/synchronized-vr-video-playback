package com.syncvr.player.core.sync

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Additional coverage for the protocol codec (no C# counterpart). */
class MessagesTest {
    @Test fun parsesPlay() {
        val m = ServerMessage.parse(
            """{"type":"play","video":"a b.mp4","projection":"180","stereo":"sbs","rotation":12.5,"duration":60,"pos":1.25,"at":1000.5,"loop":true}""",
        )!!
        assertEquals("play", m.type)
        val c = VideoCommand.from(m)
        assertEquals(VideoCommand("a b.mp4", "180", "sbs", 12.5, 60.0, 1.25, 1000.5, true), c)
    }

    @Test fun missingFieldsKeepDefaults() {
        val c = VideoCommand.from(ServerMessage.parse("""{"type":"pause","video":"v.mp4"}""")!!)
        assertEquals(VideoCommand(video = "v.mp4"), c)
        assertEquals("360", c.projection)
        assertEquals("mono", c.stereo)
    }

    @Test fun parsesWelcomeSettings() {
        val m = ServerMessage.parse(
            """{"type":"welcome","server_name":"S","device_name":"d","group":"g","http_port":8080,"server_time":5.0,"settings":{"hard_seek_ms":250,"correction_mode":"seek"}}""",
        )!!
        assertEquals(8080, m.httpPort)
        assertEquals("S", m.serverName)
        val s = m.settings!!
        assertEquals(250.0, s.hardSeekMs)
        assertEquals("seek", s.correctionMode)
        assertEquals(750.0, s.settleMs) // default kept
    }

    @Test fun parsesTimePongAndContent() {
        val pong = ServerMessage.parse("""{"type":"time_pong","id":7,"t0":1.5,"ts":2.5e3}""")!!
        assertEquals(7L, pong.id)
        assertEquals(1.5, pong.t0)
        assertEquals(2500.0, pong.ts)
        val sync = ServerMessage.parse(
            """{"type":"sync_content","files":[{"name":"a.mp4","size":123456789012,"url":"http://x/a.mp4","sha256":"ab"},{"name":"b.mp4","size":1,"url":"u"}],"delete_others":true}""",
        )!!
        assertEquals(2, sync.files.size)
        assertEquals(ContentFile("a.mp4", 123456789012L, "http://x/a.mp4", "ab"), sync.files[0])
        assertNull(sync.files[1].sha256)
        assertTrue(sync.deleteOthers)
        assertEquals(listOf("x", "y"), ServerMessage.parse("""{"type":"delete_content","names":["x","y"]}""")!!.names)
    }

    @Test fun decodesStringEscapes() {
        val m = ServerMessage.parse("""{"type":"message","text":"a\"b\\né\n","seconds":3}""")!!
        assertEquals("a\"b\\né\n", m.text)
        assertEquals(3.0, m.seconds)
    }

    @Test fun badInputIsNull() {
        assertNull(ServerMessage.parse("not json"))
        assertNull(ServerMessage.parse("""{"video":"x"}"""))
        assertNull(ServerMessage.parse("""{"type":"play"} trailing"""))
        assertNull(ServerMessage.parse("""{"type":"play""""))
    }

    @Test fun parsesBeacon() {
        val b = Beacon.parse("""{"type":"beacon","service":"syncvr","proto":1,"version":"0.1.0","server_name":"SyncVR","tcp_port":8765,"http_port":8080}""")!!
        assertEquals(Beacon("syncvr", 1, 8765, 8080, "SyncVR"), b)
    }

    @Test fun writerFormatsValues() {
        val json = JsonWriter("hello").field("proto", 1L).field("ok", true).field("d", 0.25).field("whole", 3.0)
            .field("nan", Double.NaN).raw("files", "[]").raw("none", null)
            .raw("names", JsonWriter.stringArray(listOf("a", "b\u0001"))).toString()
        assertEquals(
            """{"type":"hello","proto":1,"ok":true,"d":0.25,"whole":3,"nan":null,"files":[],"none":null,"names":["a","b\u0001"]}""",
            json,
        )
    }

    @Test fun statusRoundTripsThroughParser() {
        val p = FakePlayer()
        val e = SyncEngine(p, null)
        e.onPlay(VideoCommand(video = "v.mp4", pos = 0.0, at = 1.5, duration = 600.0))
        var t = 0.0
        while (t < 8) { p.tick(t); e.update(t); t += 1.0 / 72 }
        val w = JsonWriter("status")
        e.writeStatus(w, t)
        val o = Json.parseObject(w.toString())!!
        assertEquals("status", o["type"])
        assertEquals("playing", o["state"])
        assertEquals("v.mp4", o["video"])
        assertTrue((o["rate"] as Double) in 0.95..1.05)
    }

    @Test fun statusCarriesAnchorOnlyWhileAnchored() {
        val e = SyncEngine(FakePlayer(), null)
        assertTrue(Json.parseObject(JsonWriter("status").also { e.writeStatus(it, 0.0) }.toString())!!["anchor"] == null)
        e.onPlay(VideoCommand(video = "v.mp4", pos = 12.5, at = 3.25, loop = true, duration = 600.0))
        val o = Json.parseObject(JsonWriter("status").also { e.writeStatus(it, 1.0) }.toString())!!
        @Suppress("UNCHECKED_CAST")
        val a = o["anchor"] as Map<String, Any?>
        assertEquals(12.5, a["pos"])
        assertEquals(3.25, a["at"])
        assertEquals(true, a["loop"])
    }
}
