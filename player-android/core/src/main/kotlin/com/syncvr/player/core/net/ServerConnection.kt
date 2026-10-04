package com.syncvr.player.core.net

import com.syncvr.player.core.sync.ClockSync
import com.syncvr.player.core.sync.JsonWriter
import com.syncvr.player.core.sync.LocalClock
import com.syncvr.player.core.sync.ServerMessage
import java.io.IOException
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.atomic.AtomicLong

data class ConnectionConfig(
    /** Fixed server host; null or empty means discover by beacon. */
    val server: String? = null,
    val port: Int = DEFAULT_TCP_PORT,
    val discoveryPort: Int = DEFAULT_DISCOVERY_PORT,
    val serverName: String? = null,
    val connectTimeoutMs: Int = 5000,
    /** The server expects status at least every few seconds; silence beyond this means a dead link. */
    val receiveTimeoutMs: Int = 15000,
    val discoveryTimeoutMs: Int = 3000,
    val initialBackoffMs: Long = 500,
    val maxBackoffMs: Long = 5000,
    val fastPingMs: Long = 100,
    val slowPingMs: Long = 2000,
    val fastPingSamples: Int = 8,
)

/**
 * Finds the server, keeps one TCP connection to it (reconnecting with backoff) and runs clock-sync
 * pings on a dedicated thread. Port of ServerConnection.cs. Messages go to [inbox]; `_connected`
 * and `_disconnected` are synthetic. `time_pong` is consumed on the receive thread, timestamped
 * as soon as the line is read, so frame work on the consumer never skews the clock estimate.
 */
class ServerConnection(
    private val config: ConnectionConfig,
    private val helloJson: String,
    private val connector: TcpConnector = JavaTcpConnector,
    private val receivers: UdpReceiverFactory = JavaUdpReceiverFactory,
    private val guard: DiscoveryGuard? = null,
    private val now: () -> Double = { LocalClock.now },
    private val log: (String) -> Unit = {},
) {
    val inbox = ConcurrentLinkedQueue<ServerMessage>()
    val clock = ClockSync()

    @Volatile var connected = false; private set
    @Volatile var serverHost: String? = null; private set
    @Volatile var status = "Searching for server..."; private set

    @Volatile private var running = false
    @Volatile private var conn: TcpConnection? = null
    @Volatile private var fastPings = false
    private val pingId = AtomicLong()
    private var netThread: Thread? = null
    private var pingThread: Thread? = null

    @Synchronized
    fun start() {
        if (running) return
        running = true
        netThread = Thread(::run, "SyncVR network").apply { isDaemon = true; start() }
        pingThread = Thread(::pingLoop, "SyncVR clock").apply { isDaemon = true; start() }
    }

    @Synchronized
    fun stop() {
        running = false
        closeConn()
        netThread?.interrupt()
        pingThread?.interrupt()
    }

    /** Queue a line for the server. Safe from any thread; dropped when offline. */
    fun send(json: String) {
        val c = conn ?: return
        try {
            c.writeLine(json)
        } catch (e: IOException) {
            closeConn()
        }
    }

    /** Re-measure the clock quickly (after connecting or waking from sleep). */
    fun resyncClock() {
        clock.reset()
        fastPings = true
    }

    private fun closeConn() {
        val c = conn
        conn = null
        c?.close()
    }

    private fun run() {
        var backoff = config.initialBackoffMs
        try {
            while (running) {
                val host: String
                val port: Int
                if (!config.server.isNullOrEmpty()) {
                    host = config.server
                    port = if (config.port > 0) config.port else DEFAULT_TCP_PORT
                } else {
                    status = "Searching for server..."
                    val found = try {
                        Discovery(receivers, config.discoveryPort, config.serverName, guard).discover(config.discoveryTimeoutMs) { running }
                    } catch (e: IOException) {
                        log("discovery failed: ${e.message}")
                        null
                    }
                    if (found == null) {
                        sleep(200)
                        continue
                    }
                    host = found.host
                    port = found.port
                }
                try {
                    status = "Connecting to $host..."
                    session(host, port)
                    backoff = config.initialBackoffMs
                } catch (e: IOException) {
                    log("connection to $host ended: ${e.message}")
                } finally {
                    closeConn()
                    if (connected) {
                        connected = false
                        inbox.add(ServerMessage(type = "_disconnected"))
                    }
                }
                if (!running) break
                status = "Lost server, retrying..."
                sleep(backoff)
                backoff = minOf(backoff * 2, config.maxBackoffMs)
            }
        } catch (_: InterruptedException) {
        }
    }

    private fun session(host: String, port: Int) {
        val c = connector.connect(host, port, config.connectTimeoutMs, config.receiveTimeoutMs)
        c.writeLine(helloJson)
        serverHost = host
        resyncClock()
        conn = c
        connected = true
        status = "Connected to $host"
        inbox.add(ServerMessage(type = "_connected"))
        while (running) {
            val line = c.readLine()
            val t1 = now() // receive time, before any parsing
            if (line == null) break
            if (line.isEmpty()) continue
            val msg = ServerMessage.parse(line) ?: continue
            if (msg.type == "time_pong") {
                clock.add(msg.t0, msg.ts, t1)
                if (clock.sampleCount >= config.fastPingSamples) fastPings = false
                continue
            }
            inbox.add(msg)
        }
    }

    private fun pingLoop() {
        try {
            while (running) {
                sleep(if (fastPings) config.fastPingMs else config.slowPingMs)
                if (conn == null) continue
                val id = pingId.incrementAndGet()
                send(JsonWriter("time_ping").field("id", id).field("t0", now()).toString())
            }
        } catch (_: InterruptedException) {
        }
    }

    private fun sleep(ms: Long) = Thread.sleep(ms)
}
