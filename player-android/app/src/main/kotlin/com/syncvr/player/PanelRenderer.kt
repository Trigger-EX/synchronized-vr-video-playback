package com.syncvr.player

import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.PorterDuff
import android.graphics.RectF
import android.graphics.Typeface
import android.util.Log
import android.view.Surface
import com.syncvr.player.core.PanelStyle

/**
 * Draws the status panel with the software canvas into the compositor's panel swapchain surface.
 * Main thread only.
 */
class PanelRenderer {
    private var surface: Surface? = null
    private var loggedFailure = false

    private val background = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.rgb(16, 18, 24) }
    private val border = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.rgb(70, 80, 100)
        style = Paint.Style.STROKE
        strokeWidth = PanelStyle.BORDER_WIDTH
    }
    private val title = textPaint(PanelStyle.TITLE_SIZE, bold = true)
    private val body = textPaint(PanelStyle.BODY_SIZE, bold = false)
    private val status = textPaint(PanelStyle.STATUS_SIZE, bold = false).apply {
        color = Color.rgb(170, 200, 255)
    }
    private val box = RectF()

    fun setSurface(s: Surface?) {
        surface = s
    }

    fun draw(line1: String, line2: String, line3: String) {
        val s = surface
        if (s == null || !s.isValid) return
        val canvas: Canvas
        try {
            canvas = s.lockCanvas(null)
        } catch (e: Exception) {
            // IllegalArgumentException / OutOfResourcesException when the surface is going away.
            logFailure("lockCanvas", e)
            return
        }
        try {
            val w = canvas.width.toFloat()
            val h = canvas.height.toFloat()
            // Transparent margin; the box edge is an anti-aliased rounded rect inside it.
            canvas.drawColor(Color.TRANSPARENT, PorterDuff.Mode.CLEAR)
            val m = PanelStyle.MARGIN
            val r = PanelStyle.CORNER_RADIUS
            box.set(m, m, w - m, h - m)
            canvas.drawRoundRect(box, r, r, background)
            // Stroke is centred on its path: inset by half the width so it stays inside the box,
            // and let the outer half-texel-pair feather via anti-aliasing.
            val half = PanelStyle.BORDER_WIDTH / 2f
            box.set(m + half, m + half, w - m - half, h - m - half)
            canvas.drawRoundRect(box, r - half, r - half, border)
            val x = PanelStyle.TEXT_INSET
            val maxText = w - 2f * x
            canvas.drawText(fit(line1, title, maxText), x, h * 0.36f, title)
            canvas.drawText(fit(line2, body, maxText), x, h * 0.60f, body)
            canvas.drawText(fit(line3, status, maxText), x, h * 0.80f, status)
        } catch (e: Exception) {
            logFailure("draw", e)
        }
        try {
            s.unlockCanvasAndPost(canvas)
            if (loggedFailure) {
                loggedFailure = false
                Log.i(TAG, "Panel drawing recovered")
            }
        } catch (e: Exception) {
            logFailure("unlockCanvasAndPost", e)
        }
    }

    private fun logFailure(what: String, e: Exception) {
        if (loggedFailure) return
        loggedFailure = true
        Log.w(TAG, "Panel $what failed: $e")
    }

    private fun fit(text: String, paint: Paint, maxWidth: Float): String {
        if (paint.measureText(text) <= maxWidth) return text
        val n = paint.breakText(text, true, maxWidth - paint.measureText("..."), null)
        return text.substring(0, n.coerceAtLeast(0)) + "..."
    }

    private fun textPaint(size: Float, bold: Boolean) = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        textSize = size
        typeface = if (bold) Typeface.DEFAULT_BOLD else Typeface.DEFAULT
    }

    private companion object {
        const val TAG = "SyncVR"
    }
}
