package com.syncvr.player.core.operator

import com.syncvr.player.core.net.DEFAULT_DISCOVERY_PORT
import com.syncvr.player.core.net.Datagram
import com.syncvr.player.core.net.DiscoveryGuard
import com.syncvr.player.core.net.UdpReceiverFactory
import com.syncvr.player.core.sync.Beacon
import java.io.IOException

data class FoundServer(val host: String, val httpPort: Int, val name: String) {
    val endpoint: ServerEndpoint get() = ServerEndpoint(host, httpPort)
    val label: String get() = "$name  ($host:$httpPort)"
}

/**
 * Collects every server beacon heard in a time window (the server broadcasts once a second on UDP
 * 8766). Unlike the headset's Discovery it keeps listening so several servers can be offered.
 */
class ServerFinder(
    private val factory: UdpReceiverFactory,
    private val port: Int = DEFAULT_DISCOVERY_PORT,
    private val guard: DiscoveryGuard? = null,
    private val now: () -> Double = { System.nanoTime() / 1e9 },
) {
    /**
     * Listens for [windowMs]. [onFound] runs on the calling thread each time a new server shows up.
     * Throws IOException if the socket cannot be opened.
     */
    @Throws(IOException::class)
    fun scan(windowMs: Int, keepGoing: () -> Boolean = { true }, onFound: (FoundServer) -> Unit = {}): List<FoundServer> {
        val found = LinkedHashMap<String, FoundServer>()
        guard?.acquire()
        try {
            val rx = factory.open(port)
            try {
                val deadline = now() + windowMs / 1000.0
                while (keepGoing()) {
                    val left = ((deadline - now()) * 1000).toInt()
                    if (left <= 0) break
                    val dg = rx.receive(left) ?: break
                    val s = match(dg) ?: continue
                    val key = "${s.host}:${s.httpPort}"
                    if (key !in found) {
                        found[key] = s
                        onFound(s)
                    }
                }
            } finally {
                rx.close()
            }
        } finally {
            guard?.release()
        }
        return found.values.toList()
    }

    internal fun match(dg: Datagram): FoundServer? {
        val b = Beacon.parse(String(dg.data, Charsets.UTF_8)) ?: return null
        if (b.service != "syncvr" || b.httpPort <= 0 || dg.fromHost.isEmpty()) return null
        return FoundServer(dg.fromHost, b.httpPort, b.serverName?.takeIf { it.isNotEmpty() } ?: "SyncVR")
    }
}
