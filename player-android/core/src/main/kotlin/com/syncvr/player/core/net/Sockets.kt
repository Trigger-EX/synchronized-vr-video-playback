package com.syncvr.player.core.net

import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException

/** One received UDP datagram. */
class Datagram(val data: ByteArray, val fromHost: String)

/** Receives discovery datagrams. Implementations must be closeable from another thread. */
interface UdpReceiver {
    /** Blocks up to [timeoutMs]; returns null on timeout. Throws IOException when the socket fails or is closed. */
    fun receive(timeoutMs: Int): Datagram?
    fun close()
}

fun interface UdpReceiverFactory {
    /** Binds to [port] on all interfaces with address reuse. */
    fun open(port: Int): UdpReceiver
}

/** A connected line-oriented TCP stream. */
interface TcpConnection {
    /** Blocks for the next line; null on orderly close. Throws IOException on error or read timeout. */
    fun readLine(): String?
    /** Writes [line] plus a newline atomically with respect to other writers. */
    fun writeLine(line: String)
    fun close()
}

fun interface TcpConnector {
    /** Throws IOException when the connection cannot be made within [connectTimeoutMs]. */
    fun connect(host: String, port: Int, connectTimeoutMs: Int, readTimeoutMs: Int): TcpConnection
}

class JavaUdpReceiver(port: Int) : UdpReceiver {
    private val socket = DatagramSocket(null).apply {
        reuseAddress = true
        bind(InetSocketAddress(InetAddress.getByName("0.0.0.0"), port))
    }
    private val buf = ByteArray(2048)

    override fun receive(timeoutMs: Int): Datagram? {
        socket.soTimeout = timeoutMs
        val p = DatagramPacket(buf, buf.size)
        return try {
            socket.receive(p)
            Datagram(p.data.copyOfRange(p.offset, p.offset + p.length), p.address.hostAddress ?: "")
        } catch (e: SocketTimeoutException) {
            null
        }
    }

    override fun close() = socket.close()
}

object JavaUdpReceiverFactory : UdpReceiverFactory {
    override fun open(port: Int): UdpReceiver = JavaUdpReceiver(port)
}

class JavaTcpConnection(private val socket: Socket) : TcpConnection {
    private val reader = BufferedReader(InputStreamReader(socket.getInputStream(), Charsets.UTF_8))
    private val out: OutputStream = socket.getOutputStream()
    private val gate = Any()

    override fun readLine(): String? = reader.readLine()

    override fun writeLine(line: String) {
        val bytes = (line + "\n").toByteArray(Charsets.UTF_8)
        synchronized(gate) {
            out.write(bytes)
            out.flush()
        }
    }

    override fun close() {
        try { socket.close() } catch (_: Exception) { }
    }
}

object JavaTcpConnector : TcpConnector {
    override fun connect(host: String, port: Int, connectTimeoutMs: Int, readTimeoutMs: Int): TcpConnection {
        val s = Socket()
        try {
            s.tcpNoDelay = true
            s.keepAlive = true
            s.connect(InetSocketAddress(host, port), connectTimeoutMs)
            s.soTimeout = readTimeoutMs
        } catch (e: Exception) {
            try { s.close() } catch (_: Exception) { }
            throw e
        }
        return JavaTcpConnection(s)
    }
}
