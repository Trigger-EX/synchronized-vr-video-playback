package com.syncvr.player

/** JNI entry points into libsyncvr_native (app/src/main/cpp). The handle is an App* from nativeCreate. */
object NativeBridge {
    init {
        System.loadLibrary("syncvr_native")
    }

    /** Starts the render thread; returns 0 on failure. Native calls MainActivity.onNativeSurfacesReady. */
    @JvmStatic external fun nativeCreate(activity: MainActivity): Long

    @JvmStatic external fun nativeDestroy(handle: Long)

    @JvmStatic external fun nativeResume(handle: Long)

    @JvmStatic external fun nativePause(handle: Long)

    /** Used for both surfaceCreated and surfaceChanged. */
    @JvmStatic external fun nativeSurfaceChanged(handle: Long, surface: android.view.Surface)

    @JvmStatic external fun nativeSurfaceDestroyed(handle: Long)

    /** Mode number is DisplayMode.ordinal (see native modes.h). */
    @JvmStatic external fun nativeSetMode(handle: Long, mode: Int)

    @JvmStatic external fun nativeSetVideoAspect(handle: Long, aspect: Float)

    @JvmStatic external fun nativeGetStatus(handle: Long): String
}
