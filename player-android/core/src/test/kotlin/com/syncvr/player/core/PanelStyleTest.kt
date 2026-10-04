package com.syncvr.player.core

import kotlin.test.Test
import kotlin.test.assertTrue

class PanelStyleTest {
    @Test
    fun smallPanelIsNotMinifiedTooMuch() {
        val r = PanelStyle.texelsPerDisplayPixel(1.2, 3.0, PanelStyle.TEXTURE_WIDTH)
        assertTrue(r <= 2.0, "ratio $r")
    }

    @Test
    fun prominentPanelIsNotMinifiedTooMuch() {
        val r = PanelStyle.texelsPerDisplayPixel(2.4, 3.0, PanelStyle.TEXTURE_WIDTH)
        assertTrue(r <= 2.0, "ratio $r")
    }

    @Test
    fun marginIsAtLeastTwoTexels() {
        assertTrue(PanelStyle.MARGIN >= 2f)
        assertTrue(PanelStyle.BORDER_WIDTH >= 6f)
        assertTrue(PanelStyle.MARGIN + PanelStyle.CORNER_RADIUS < PanelStyle.TEXTURE_HEIGHT / 2f)
    }
}
