package com.syncvr.player.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class PlayerInfoTest {
    @Test
    fun videoDirMatchesPackage() {
        assertTrue(PlayerInfo.VIDEO_DIR.contains(PlayerInfo.PACKAGE))
        assertEquals("native", PlayerInfo.PLAYER)
    }
}
