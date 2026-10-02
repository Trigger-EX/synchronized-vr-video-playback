package com.syncvr.player

/** JNI entry points into libsyncvr_native (app/src/main/cpp). */
object NativeBridge {
    init {
        System.loadLibrary("syncvr_native")
    }

    @JvmStatic external fun vrApiVersion(): String
}
