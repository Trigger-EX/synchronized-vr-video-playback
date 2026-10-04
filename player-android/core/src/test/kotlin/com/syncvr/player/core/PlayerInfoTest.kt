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

class HelloTest {
    @Test
    fun helloReportsNativePlayer() {
        val m = com.syncvr.player.core.sync.Json.parseObject(Hello.build("id", "ser", "Oculus Go", "1.0"))!!
        assertEquals("native", m["player"])
        assertEquals("hello", m["type"])
    }
}
