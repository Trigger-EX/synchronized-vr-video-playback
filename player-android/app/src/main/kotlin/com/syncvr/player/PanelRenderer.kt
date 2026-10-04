package com.syncvr.player

import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.util.Log
import android.view.Surface

/**
 * Draws the status panel with the software canvas into the compositor's panel swapchain surface.
 * Main thread only.
 */
class PanelRenderer {
    private var surface: Surface? = null
    private var loggedFailure = false

    private val background = Paint().apply { color = Color.rgb(16, 18, 24) }
    private val border = Paint().apply {
        color = Color.rgb(70, 80, 100)
        style = Paint.Style.STROKE
        strokeWidth = 4f
    }
    private val title = textPaint(52f, bold = true)
    private val body = textPaint(38f, bold = false)
    private val status = textPaint(32f, bold = false).apply { color = Color.rgb(170, 200, 255) }

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
            canvas.drawRect(0f, 0f, w, h, background)
            canvas.drawRect(2f, 2f, w - 2f, h - 2f, border)
            canvas.drawText(fit(line1, title, w - 48f), 24f, h * 0.30f, title)
            canvas.drawText(fit(line2, body, w - 48f), 24f, h * 0.60f, body)
            canvas.drawText(fit(line3, status, w - 48f), 24f, h * 0.88f, status)
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
