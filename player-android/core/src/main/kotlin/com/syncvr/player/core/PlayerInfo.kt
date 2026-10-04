package com.syncvr.player.core

/** Identity the native player reports to the server (see docs/PROTOCOL.md, `hello`). */
object PlayerInfo {
    const val PROTOCOL_VERSION = 1
    const val PLAYER = "native"
    const val PACKAGE = "com.syncvr.player"

    /** Videos live in the app's external files dir, the same folder the Unity app used. */
    const val VIDEO_DIR = "/sdcard/Android/data/$PACKAGE/files/videos/"
}
