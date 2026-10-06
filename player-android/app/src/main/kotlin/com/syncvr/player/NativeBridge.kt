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

    /**
     * [mode] is DisplayMode.ordinal, [stereo] is ViewStereo.code, [half180] marks a 180-degree
     * half-sphere video (see native modes.h).
     */
    @JvmStatic external fun nativeSetMode(handle: Long, mode: Int, stereo: Int, half180: Boolean)

    @JvmStatic external fun nativeSetVideoAspect(handle: Long, aspect: Float)

    /** Operator recenter: the current head direction becomes the front. */
    @JvmStatic external fun nativeRecenter(handle: Long)

    /** [yaw, pitch, roll] in degrees, relative to the recentered front. */
    @JvmStatic external fun nativeGetPose(handle: Long): FloatArray

    /** Larger, centred panel while an operator message or identify banner is up. */
    @JvmStatic external fun nativeSetPanelProminent(handle: Long, prominent: Boolean)

    @JvmStatic external fun nativeGetStatus(handle: Long): String
}
