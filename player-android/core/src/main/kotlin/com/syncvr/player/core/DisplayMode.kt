package com.syncvr.player.core

/**
 * The display paths the hardware check cycles through. [ordinal] is also the mode number
 * passed to native code, so keep it in sync with native/modes.h.
 */
enum class DisplayMode(val title: String) {
    EQUIRECT_MONO("Equirect layer (mono)"),
    SPHERE_FALLBACK("Sphere fallback (app-rendered)"),
    CYLINDER_FLAT("Cylinder layer (flat screen)"),
    EQUIRECT_STEREO_TB("Equirect layer (stereo top/bottom)");

    /** True when ExoPlayer renders into the compositor's Android-surface swapchain. */
    val usesCompositorSurface: Boolean get() = this != SPHERE_FALLBACK

    /** "1/4 Equirect layer (mono)" style label. */
    fun label(): String = "${ordinal + 1}/${values().size} $title"

    companion object {
        fun fromIndex(index: Int): DisplayMode {
            val all = values()
            return all[((index % all.size) + all.size) % all.size]
        }
    }
}
