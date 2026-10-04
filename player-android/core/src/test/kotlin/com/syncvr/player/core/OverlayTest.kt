package com.syncvr.player.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class OverlayTest {
    private fun input(
        connected: Boolean = true,
        state: String = "idle",
        video: String? = null,
        receiving: Boolean = false,
        error: String? = null,
    ) = OverlayInput("Go 3", "Studio", connected, "Searching for server...", state, error, receiving, "mode", video, "VR on")

    @Test
    fun idleShowsNameAndConnectionState() {
        val o = Overlay()
        val off = o.compose(0.0, input(connected = false))
        assertEquals("Go 3  mode", off.line1)
        assertEquals("Searching for server...", off.line2)
        val on = o.compose(0.0, input())
        assertEquals("Connected to Studio, waiting for the operator", on.line2)
        assertTrue(o.compose(0.0, input(receiving = true)).line2.endsWith("(receiving content)"))
        assertEquals("Loading...", o.compose(0.0, input(state = "loading")).line2)
    }

    @Test
    fun videoLineAndError() {
        val c = Overlay().compose(0.0, input(state = "playing", video = "a.mp4  0:01 / 0:10", error = "boom"))
        assertEquals("a.mp4  0:01 / 0:10  [playing]", c.line2)
        assertEquals("Player error: boom", c.line3)
    }

    @Test
    fun messageExpiresAndClears() {
        val o = Overlay()
        o.showMessage("Please take a seat", 5.0, 100.0)
        assertTrue(o.messageActive(104.9))
        assertTrue(o.compose(101.0, input()).prominent)
        assertFalse(o.messageActive(105.0))
        assertFalse(o.compose(106.0, input()).prominent)
        o.showMessage("x", 5.0, 100.0)
        o.showMessage("", 5.0, 101.0)
        assertFalse(o.messageActive(101.5))
        o.showMessage("x", 0.0, 100.0)
        assertFalse(o.messageActive(100.0))
    }

    @Test
    fun identifyShowsName() {
        val o = Overlay()
        o.identify("Headset 7", 8.0, 0.0)
        val c = o.compose(1.0, input())
        assertEquals("Headset 7", c.line1)
        assertTrue(c.prominent)
        o.showMessage("hello", 3.0, 2.0)
        assertEquals("hello", o.compose(2.5, input()).line1)
    }

    @Test
    fun wordWrap() {
        assertEquals(listOf("aaa bbb", "ccc"), Overlay.wordWrap("aaa bbb ccc", 8))
        assertEquals(listOf("longerthanwidth"), Overlay.wordWrap("longerthanwidth", 5))
        assertEquals(3, Overlay.beepOffsetsMs().size)
    }
}
