package com.syncvr.core

/** Identity the native player reports in its `hello` message (docs/PROTOCOL.md). */
object PlayerInfo {
    const val PLAYER = "native"
    const val PACKAGE = "com.syncvr.player"

    /** Folder on the headset that holds the video library. */
    const val VIDEO_DIR = "/sdcard/Android/data/$PACKAGE/files/videos/"
}
