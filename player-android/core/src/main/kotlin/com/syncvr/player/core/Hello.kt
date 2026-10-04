package com.syncvr.player.core

import com.syncvr.player.core.sync.JsonWriter

/** The first line on every connection (docs/PROTOCOL.md).. */
object Hello {
    fun build(deviceId: String, serial: String, model: String, appVersion: String, player: String = PlayerInfo.PLAYER): String =
        JsonWriter("hello")
            .field("proto", PlayerInfo.PROTOCOL_VERSION.toLong())
            .field("device_id", deviceId)
            .field("serial", serial)
            .field("model", model)
            .field("app_version", appVersion)
            .field("player", player)
            .toString()
}
