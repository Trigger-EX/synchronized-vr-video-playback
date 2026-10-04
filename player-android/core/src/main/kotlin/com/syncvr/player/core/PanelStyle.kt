package com.syncvr.player.core

import kotlin.math.PI

/**
 * Geometry of the headset status panel texture. The panel is minified on the headset, so the
 * texture is kept near the display's pixel density and surrounded by a transparent margin: the
 * visible box edge is then drawn anti-aliased inside the texture instead of being the compositor's
 * hard clip boundary.
 */
object PanelStyle {
    const val TEXTURE_WIDTH = 576
    const val TEXTURE_HEIGHT = 144

    /** Fully transparent border on every side, about 8% of the height, in texels. */
    const val MARGIN = 12f

    /** Corner radius of the box, in texels. */
    const val CORNER_RADIUS = 18f

    /** Box outline width, in texels. */
    const val BORDER_WIDTH = 6f

    /** Width of the soft (feathered) transition on the outer edge of the box, in texels. */
    const val EDGE_SOFTNESS = 2f

    const val TITLE_SIZE = 30f
    const val BODY_SIZE = 22f
    const val STATUS_SIZE = 19f

    /** Text inset from the texture edge, in texels. */
    const val TEXT_INSET = MARGIN + BORDER_WIDTH + 8f

    /** Approximate Oculus Go display density at the centre of the view. */
    const val DEFAULT_PX_PER_DEG = 14.0

    /**
     * Panel texels per physical display pixel for a panel of arc width [widthM] on a cylinder of
     * radius [radiusM]. Values above 1 mean the texture is minified, above about 2 it shimmers.
     */
    fun texelsPerDisplayPixel(
        widthM: Double,
        radiusM: Double,
        texW: Int,
        pxPerDeg: Double = DEFAULT_PX_PER_DEG,
    ): Double {
        val widthDeg = widthM / radiusM * 180.0 / PI
        return texW / (widthDeg * pxPerDeg)
    }
}
