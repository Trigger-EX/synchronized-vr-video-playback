package com.syncvr.player.core.net

import java.net.ServerSocket
import kotlin.concurrent.thread
import kotlin.test.Test
import kotlin.test.assertEquals

class JavaSocketsTest {
    @Test fun tcpRoundTripOverLoopback() {
        ServerSocket(0).use { ss ->
            val t = thread {
                ss.accept().use { s ->
                    val line = s.getInputStream().bufferedReader().readLine()
                    s.getOutputStream().write(("echo:$line\n").toByteArray())
                }
            }
            val c = JavaTcpConnector.connect("127.0.0.1", ss.localPort, 2000, 2000)
            c.writeLine("hi")
            assertEquals("echo:hi", c.readLine())
            c.close()
            t.join()
        }
    }
}
