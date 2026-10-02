package com.syncvr.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class PlayerInfoTest {
    @Test
    fun reportsNativePlayer() {
        assertEquals("native", PlayerInfo.PLAYER)
    }

    @Test
    fun videoDirMatchesUnityApp() {
        assertTrue(PlayerInfo.VIDEO_DIR.endsWith("/com.syncvr.player/files/videos/"))
    }
}
