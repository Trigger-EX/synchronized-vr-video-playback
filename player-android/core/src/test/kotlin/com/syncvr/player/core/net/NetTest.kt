package com.syncvr.player.core.net

import com.syncvr.player.core.sync.JsonWriter
import java.io.IOException
import java.util.concurrent.BlockingQueue
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

private const val EOF = "\u0000EOF"

/** Fake server-side TCP connection. Incoming lines are scripted; pings can be answered automatically. */
class FakeConn(private val answerPings: Boolean = false, private val serverOffset: Double = 100.0) : TcpConnection {
    val incoming: BlockingQueue<String> = LinkedBlockingQueue()
    val sent = CopyOnWriteArrayList<String>()
    @Volatile var closed = false

    override fun readLine(): String? {
        val l = incoming.poll(5, TimeUnit.SECONDS) ?: throw IOException("read timeout")
        if (l == EOF) return null
        if (l.startsWith("\u0000ERR")) throw IOException("reset")
        return l
    }

    override fun writeLine(line: String) {
        if (closed) throw IOException("closed")
        sent.add(line)
        if (answerPings && line.contains("\"time_ping\"")) {
            val id = Regex("\"id\":(\\d+)").find(line)!!.groupValues[1]
            val t0 = Regex("\"t0\":([-0-9.eE]+)").find(line)!!.groupValues[1].toDouble()
            incoming.add("""{"type":"time_pong","id":$id,"t0":$t0,"ts":${t0 + serverOffset}}""")
        }
    }

    fun eof() = incoming.add(EOF)
    override fun close() {
        closed = true
        incoming.add(EOF)
    }
}

class FakeConnector(private val results: List<() -> TcpConnection>) : TcpConnector {
    val attempts = java.util.concurrent.atomic.AtomicInteger()
    val hosts = CopyOnWriteArrayList<String>()
    override fun connect(host: String, port: Int, connectTimeoutMs: Int, readTimeoutMs: Int): TcpConnection {
        hosts.add("$host:$port")
        val i = attempts.getAndIncrement()
        return results[minOf(i, results.size - 1)]()
    }
}

class FakeUdp(private val datagrams: List<Datagram>) : UdpReceiverFactory {
    var openedPort = -1
    var closedCount = 0
    override fun open(port: Int): UdpReceiver {
        openedPort = port
        val q = ArrayDeque(datagrams)
        return object : UdpReceiver {
            override fun receive(timeoutMs: Int): Datagram? = q.removeFirstOrNull()
            override fun close() { closedCount++ }
        }
    }
}

private fun beacon(service: String = "syncvr", tcp: Int = 8765, name: String = "SyncVR") =
    """{"type":"beacon","service":"$service","proto":1,"version":"0.1.0","server_name":"$name","tcp_port":$tcp,"http_port":8080}"""

private fun dg(s: String, from: String = "10.0.0.5") = Datagram(s.toByteArray(), from)

private fun <T> await(timeoutMs: Long = 5000, f: () -> T?): T {
    val end = System.currentTimeMillis() + timeoutMs
    while (System.currentTimeMillis() < end) {
        f()?.let { return it }
        Thread.sleep(5)
    }
    throw AssertionError("timed out")
}

class DiscoveryTest {
    @Test fun skipsForeignAndMalformedThenUsesSourceAddressAndBeaconPort() {
        val udp = FakeUdp(listOf(dg("garbage"), dg(beacon(service = "other")), dg(beacon(tcp = 0)), dg(beacon(tcp = 9000), "192.168.1.7")))
        var acquired = 0; var released = 0
        val guard = object : DiscoveryGuard {
            override fun acquire() { acquired++ }
            override fun release() { released++ }
        }
        val found = Discovery(udp, 8766, guard = guard).discover(1000)
        assertEquals(ServerAddress("192.168.1.7", 9000, "SyncVR"), found)
        assertEquals(8766, udp.openedPort)
        assertEquals(1, udp.closedCount)
        assertEquals(1, acquired); assertEquals(1, released)
    }

    @Test fun filtersByServerName() {
        val udp = FakeUdp(listOf(dg(beacon(name = "A"), "1.1.1.1"), dg(beacon(name = "B"), "2.2.2.2")))
        assertEquals("2.2.2.2", Discovery(udp, serverName = "B").discover(1000)?.host)
    }

    @Test fun timeoutReturnsNullAndStillReleasesGuard() {
        var released = 0
        val guard = object : DiscoveryGuard {
            override fun acquire() {}
            override fun release() { released++ }
        }
        assertNull(Discovery(FakeUdp(emptyList()), guard = guard).discover(50))
        assertEquals(1, released)
    }
}

class ServerConnectionTest {
    private val fast = ConnectionConfig(server = "10.0.0.1", initialBackoffMs = 10, maxBackoffMs = 40, fastPingMs = 5, slowPingMs = 20)

    @Test fun sendsHelloFirstQueuesMessagesAndSyncsClockFromPongs() {
        val c = FakeConn(answerPings = true, serverOffset = 100.0)
        val sc = ServerConnection(fast, """{"type":"hello"}""", FakeConnector(listOf { c }))
        sc.start()
        try {
            await { if (sc.connected) true else null }
            c.incoming.add("""{"type":"pause","pos":1.5}""")
            val msg = await { sc.inbox.poll() ?: null }
            // _connected comes first
            val first = msg
            val second = if (first.type == "_connected") await { sc.inbox.poll() } else first
            assertEquals("pause", second.type)
            assertEquals("""{"type":"hello"}""", c.sent[0])
            await { if (sc.clock.synced && sc.clock.sampleCount >= 3) true else null }
            assertTrue(Math.abs(sc.clock.offset - 100.0) < 0.05, "offset ${sc.clock.offset}")
            // pongs are consumed, never queued
            assertTrue(sc.inbox.none { it.type == "time_pong" })
        } finally { sc.stop() }
    }

    @Test fun pongUsesReceiveTimeNotProcessingTime() {
        var t = 0.0
        val c = FakeConn()
        val sc = ServerConnection(fast.copy(slowPingMs = 100000, fastPingMs = 100000), "{}", FakeConnector(listOf { c }), now = { t })
        sc.start()
        try {
            await { if (sc.connected) true else null }
            t = 10.0
            c.incoming.add("""{"type":"time_pong","id":1,"t0":10.0,"ts":60.01}""")
            // t1 is sampled at read time = 10.0 (t0 == t1 => rtt 0, offset 50.01)
            await { if (sc.clock.synced) true else null }
            assertEquals(50.01, sc.clock.offset, 1e-9)
        } finally { sc.stop() }
    }

    @Test fun reconnectsWithBackoffAfterDropAndConnectFailure() {
        val c1 = FakeConn(); val c2 = FakeConn()
        val connector = FakeConnector(listOf({ c1 }, { throw IOException("refused") }, { c2 }))
        val sc = ServerConnection(fast, "{}", connector)
        sc.start()
        try {
            await { if (sc.connected) true else null }
            c1.eof()
            await { sc.inbox.firstOrNull { it.type == "_disconnected" } }
            await { if (connector.attempts.get() >= 3 && sc.connected) true else null }
            assertTrue(c2.sent.isNotEmpty())
            assertEquals("10.0.0.1:8765", connector.hosts[0])
        } finally { sc.stop() }
    }

    @Test fun sendIsDroppedWhenOffline() {
        val sc = ServerConnection(fast, "{}", FakeConnector(listOf { throw IOException("down") }))
        sc.send(JsonWriter("status").toString()) // must not throw
        assertNull(sc.inbox.poll())
    }

    @Test fun discoversThenConnectsToBeaconAddress() {
        val c = FakeConn()
        val connector = FakeConnector(listOf { c })
        val udp = FakeUdp(listOf(dg(beacon(tcp = 9001), "10.1.2.3")))
        val sc = ServerConnection(ConnectionConfig(initialBackoffMs = 10, fastPingMs = 5, slowPingMs = 20), "{}", connector, udp)
        sc.start()
        try {
            await { if (sc.connected) true else null }
            assertEquals("10.1.2.3:9001", connector.hosts[0])
            assertEquals("10.1.2.3", sc.serverHost)
        } finally { sc.stop() }
    }

    @Test fun stopClosesConnection() {
        val c = FakeConn()
        val sc = ServerConnection(fast, "{}", FakeConnector(listOf { c }))
        sc.start()
        await { if (sc.connected) true else null }
        sc.stop()
        await { if (c.closed) true else null }
        assertNotNull(c)
    }
}
