package com.syncvr.player.core.net

import com.syncvr.player.core.sync.Beacon

const val DEFAULT_TCP_PORT = 8765
const val DEFAULT_DISCOVERY_PORT = 8766

data class ServerAddress(val host: String, val port: Int, val serverName: String?)

/** Called around a discovery attempt; the app uses it to hold the Wi-Fi multicast lock. */
interface DiscoveryGuard {
    fun acquire()
    fun release()
}

/**
 * Listens for the server's UDP beacon (see server/syncvr/discovery.py). The host is the datagram's
 * source address, the TCP port comes from the beacon. Beacons from other services, or other servers
 * when [serverName] is set, are ignored.
 */
class Discovery(
    private val factory: UdpReceiverFactory,
    private val port: Int = DEFAULT_DISCOVERY_PORT,
    private val serverName: String? = null,
    private val guard: DiscoveryGuard? = null,
    private val now: () -> Double = { System.nanoTime() / 1e9 },
) {
    /** Returns the first matching server within [timeoutMs], or null. Throws IOException if the socket cannot be opened. */
    fun discover(timeoutMs: Int, keepGoing: () -> Boolean = { true }): ServerAddress? {
        guard?.acquire()
        try {
            val rx = factory.open(port)
            try {
                val deadline = now() + timeoutMs / 1000.0
                while (keepGoing()) {
                    val left = ((deadline - now()) * 1000).toInt()
                    if (left <= 0) return null
                    val dg = rx.receive(left) ?: return null
                    match(dg)?.let { return it }
                }
                return null
            } finally {
                rx.close()
            }
        } finally {
            guard?.release()
        }
    }

    internal fun match(dg: Datagram): ServerAddress? {
        val b = Beacon.parse(String(dg.data, Charsets.UTF_8)) ?: return null
        if (b.service != "syncvr" || b.tcpPort <= 0) return null
        if (!serverName.isNullOrEmpty() && b.serverName != serverName) return null
        return ServerAddress(dg.fromHost, b.tcpPort, b.serverName)
    }
}
