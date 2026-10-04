package com.syncvr.player.core

/** Which compositor layer shows the video. */
enum class ViewLayer { EQUIRECT, CYLINDER }

/** Frame packing. [code] is passed to native code, keep it in sync with native/modes.h (Stereo). */
enum class ViewStereo(val code: Int) { MONO(0), TOP_BOTTOM(1), SIDE_BY_SIDE(2) }

/**
 * How one video is shown, derived from the `projection` / `stereo` strings of play and view
 * messages: `flat` -> cylinder layer, `360` -> full equirect, `180` -> equirect layer with the
 * front half-sphere ([half]); stereo `mono`, `tb`, `sbs`. Unknown values fall back to 360 / mono
 * (each field on its own).
 */
data class ViewSpec(val layer: ViewLayer, val stereo: ViewStereo, val half: Boolean) {
    /** The display path for [layer]; the cycle-only modes (sphere, equirect TB) are not used here. */
    val displayMode: DisplayMode
        get() = if (layer == ViewLayer.CYLINDER) DisplayMode.CYLINDER_FLAT else DisplayMode.EQUIRECT_MONO

    /** Short panel label, e.g. "360 mono", "180 sbs", "flat tb". */
    fun label(): String {
        val p = when {
            layer == ViewLayer.CYLINDER -> "flat"
            half -> "180"
            else -> "360"
        }
        val s = when (stereo) {
            ViewStereo.MONO -> "mono"
            ViewStereo.TOP_BOTTOM -> "tb"
            ViewStereo.SIDE_BY_SIDE -> "sbs"
        }
        return "$p $s"
    }

    companion object {
        val DEFAULT = ViewSpec(ViewLayer.EQUIRECT, ViewStereo.MONO, false)

        fun from(projection: String?, stereo: String?): ViewSpec {
            val (layer, half) = when (projection?.trim()?.lowercase()) {
                "flat" -> ViewLayer.CYLINDER to false
                "180" -> ViewLayer.EQUIRECT to true
                else -> ViewLayer.EQUIRECT to false // "360" and anything unknown
            }
            val st = when (stereo?.trim()?.lowercase()) {
                "tb" -> ViewStereo.TOP_BOTTOM
                "sbs" -> ViewStereo.SIDE_BY_SIDE
                else -> ViewStereo.MONO
            }
            return ViewSpec(layer, st, half)
        }
    }
}
