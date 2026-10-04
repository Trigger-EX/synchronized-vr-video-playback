package com.syncvr.player.core

/** What the app should draw on the panel this frame. [prominent] asks for the large placement. */
data class PanelContent(
    val line1: String,
    val line2: String,
    val line3: String,
    val prominent: Boolean = false,
)

/** Everything the idle/status screen depends on; see [Overlay.compose]. */
data class OverlayInput(
    val deviceName: String,
    val serverName: String,
    val connected: Boolean,
    /** ServerConnection.status, shown while not connected. */
    val connectionStatus: String,
    /** Engine state: idle|loading|ready|playing|paused|ended|error. */
    val state: String,
    val playerError: String?,
    val receivingContent: Boolean,
    val modeLabel: String,
    /** Video line ("name  0:12 / 3:45") while a video is loaded. */
    val videoLine: String?,
    val nativeStatus: String,
)

/**
 * Operator messages, the identify banner and the idle screen text. Pure logic; ports Overlay.cs
 * and SyncVRApp.StatusText. Main thread only.
 */
class Overlay {
    private var text = ""
    private var until = 0.0
    private var identifying = false

    /** An empty [message] or non-positive [seconds] clears the message. */
    fun showMessage(message: String?, seconds: Double, now: Double) {
        identifying = false
        if (message.isNullOrEmpty() || seconds <= 0) {
            text = ""
            until = 0.0
        } else {
            text = message
            until = now + seconds
        }
    }

    fun identify(name: String, seconds: Double, now: Double) {
        showMessage(name, seconds, now)
        identifying = text.isNotEmpty()
    }

    fun messageActive(now: Double): Boolean = text.isNotEmpty() && now < until

    fun compose(now: Double, i: OverlayInput): PanelContent {
        val title = i.deviceName.ifEmpty { "SyncVR" }
        if (messageActive(now)) {
            val wrapped = wordWrap(text, MESSAGE_WIDTH)
            return if (identifying) {
                PanelContent(text, "This headset is: $title", "", prominent = true)
            } else {
                PanelContent(wrapped.firstOrNull() ?: "", wrapped.drop(1).joinToString(" "), "", prominent = true)
            }
        }
        val line2 = when {
            i.videoLine != null -> "${i.videoLine}  [${i.state}]"
            else -> idleLine(i)
        }
        val line3 = i.playerError?.let { "Player error: $it" } ?: i.nativeStatus
        return PanelContent("$title  ${i.modeLabel}".trim(), line2, line3)
    }

    private fun idleLine(i: OverlayInput): String {
        val base = when {
            !i.connected -> i.connectionStatus
            i.state == "loading" -> "Loading..."
            i.state == "error" -> "Cannot play this video"
            i.serverName.isEmpty() -> "Connected, waiting for the operator"
            else -> "Connected to ${i.serverName}, waiting for the operator"
        }
        return if (i.receivingContent) "$base (receiving content)" else base
    }

    companion object {
        const val MESSAGE_WIDTH = 32
        /** Pip start times (ms) for the identify beep: three short pips, as in Overlay.cs. */
        fun beepOffsetsMs(): List<Long> = listOf(0L, 330L, 660L)

        fun wordWrap(text: String, width: Int): List<String> {
            val lines = ArrayList<String>()
            val sb = StringBuilder()
            for (word in text.split(' ')) {
                if (sb.isNotEmpty() && sb.length + word.length + 1 > width) {
                    lines.add(sb.toString())
                    sb.setLength(0)
                }
                if (sb.isNotEmpty()) sb.append(' ')
                sb.append(word)
            }
            if (sb.isNotEmpty()) lines.add(sb.toString())
            return lines
        }
    }
}
