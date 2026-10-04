package com.syncvr.player.core.sync

/**
 * Protocol messages (see docs/PROTOCOL.md). Port of Messages.cs. Property names are camelCase;
 * the wire names (snake_case) are used by the parse and write functions.
 */

/** One entry of a `sync_content` file list. */
data class ContentFile(
    val name: String? = null,
    val size: Long = 0,
    val url: String? = null,
    /** Lowercase hex SHA-256; null while the server has not computed it yet. */
    val sha256: String? = null,
)

/** UDP discovery beacon. */
data class Beacon(
    val service: String? = null,
    val proto: Int = 0,
    val tcpPort: Int = 0,
    val httpPort: Int = 0,
    val serverName: String? = null,
) {
    companion object {
        fun parse(json: String): Beacon? {
            val o = Json.parseObject(json) ?: return null
            return Beacon(
                service = o.str("service"),
                proto = o.int("proto"),
                tcpPort = o.int("tcp_port"),
                httpPort = o.int("http_port"),
                serverName = o.str("server_name"),
            )
        }
    }
}

/** Sync tuning pushed by the server (see protocol.py DEFAULT_SYNC_SETTINGS). */
data class SyncSettings(
    var playLeadMs: Double = 1500.0,
    var seekLeadMs: Double = 1500.0,
    var pauseLeadMs: Double = 300.0,
    var correctionMode: String = "rate",
    var deadbandMs: Double = 20.0,
    var rateGain: Double = 0.8,
    var maxRateAdjust: Double = 0.05,
    var hardSeekMs: Double = 300.0,
    var seekModeThresholdMs: Double = 80.0,
    var seekCooldownMs: Double = 3000.0,
    var settleMs: Double = 750.0,
) {
    companion object {
        /** Fields missing from [o] keep their defaults. */
        fun from(o: Map<String, Any?>): SyncSettings {
            val d = SyncSettings()
            return SyncSettings(
                playLeadMs = o.dbl("play_lead_ms", d.playLeadMs),
                seekLeadMs = o.dbl("seek_lead_ms", d.seekLeadMs),
                pauseLeadMs = o.dbl("pause_lead_ms", d.pauseLeadMs),
                correctionMode = o.str("correction_mode") ?: d.correctionMode,
                deadbandMs = o.dbl("deadband_ms", d.deadbandMs),
                rateGain = o.dbl("rate_gain", d.rateGain),
                maxRateAdjust = o.dbl("max_rate_adjust", d.maxRateAdjust),
                hardSeekMs = o.dbl("hard_seek_ms", d.hardSeekMs),
                seekModeThresholdMs = o.dbl("seek_mode_threshold_ms", d.seekModeThresholdMs),
                seekCooldownMs = o.dbl("seek_cooldown_ms", d.seekCooldownMs),
                settleMs = o.dbl("settle_ms", d.settleMs),
            )
        }
    }
}

/**
 * Every message the server can send, flattened into one class. Fields a message does not carry
 * keep their defaults.
 */
data class ServerMessage(
    val type: String? = null,

    // time_pong
    val id: Long = 0,
    val t0: Double = 0.0,
    val ts: Double = 0.0,

    // welcome, device_info
    val serverName: String? = null,
    val deviceName: String? = null,
    val group: String? = null,
    val httpPort: Int = 0,
    val settings: SyncSettings? = null,

    // play, pause
    val video: String? = null,
    val projection: String? = null,
    val stereo: String? = null,
    val rotation: Double = 0.0,
    val duration: Double = 0.0,
    val pos: Double = 0.0,
    val at: Double = 0.0,
    val loop: Boolean = false,

    // volume
    val value: Double = 0.0,

    // message, identify
    val text: String? = null,
    val name: String? = null,
    val seconds: Double = 0.0,

    // sync_content, delete_content
    val files: List<ContentFile> = emptyList(),
    val deleteOthers: Boolean = false,
    val names: List<String> = emptyList(),
    /** `sync_content` job id as a JSON literal (string or integer), echoed back; null when absent. */
    val job: String? = null,
) {
    companion object {
        /** Parses one line from the server. Returns null for malformed JSON or a missing `type`. */
        fun parse(line: String): ServerMessage? {
            val o = Json.parseObject(line) ?: return null
            val type = o.str("type") ?: return null
            return ServerMessage(
                type = type,
                id = o.long("id"),
                t0 = o.dbl("t0"),
                ts = o.dbl("ts"),
                serverName = o.str("server_name"),
                deviceName = o.str("device_name"),
                group = o.str("group"),
                httpPort = o.int("http_port"),
                settings = o.obj("settings")?.let { SyncSettings.from(it) },
                video = o.str("video"),
                projection = o.str("projection"),
                stereo = o.str("stereo"),
                rotation = o.dbl("rotation"),
                duration = o.dbl("duration"),
                pos = o.dbl("pos"),
                at = o.dbl("at"),
                loop = o.bool("loop"),
                value = o.dbl("value"),
                text = o.str("text"),
                name = o.str("name"),
                seconds = o.dbl("seconds"),
                files = o.list("files").mapNotNull { f ->
                    @Suppress("UNCHECKED_CAST")
                    (f as? Map<String, Any?>)?.let {
                        ContentFile(it.str("name"), it.long("size"), it.str("url"), it.str("sha256"))
                    }
                },
                deleteOthers = o.bool("delete_others"),
                names = o.list("names").filterIsInstance<String>(),
                job = when (val j = o["job"]) {
                    is String -> JsonWriter.stringLiteral(j)
                    is Double -> if (j == Math.rint(j) && Math.abs(j) < 1e15) j.toLong().toString() else null
                    else -> null
                },
            )
        }
    }
}

/** A play/pause/load instruction for one video. */
data class VideoCommand(
    val video: String? = null,
    val projection: String = "360",
    val stereo: String = "mono",
    val rotation: Double = 0.0,
    val duration: Double = 0.0,
    val pos: Double = 0.0,
    val at: Double = 0.0,
    val loop: Boolean = false,
) {
    fun with(pos: Double, at: Double): VideoCommand = copy(pos = pos, at = at)

    companion object {
        fun from(m: ServerMessage) = VideoCommand(
            video = m.video,
            projection = if (m.projection.isNullOrEmpty()) "360" else m.projection,
            stereo = if (m.stereo.isNullOrEmpty()) "mono" else m.stereo,
            rotation = m.rotation,
            duration = m.duration,
            pos = m.pos,
            at = m.at,
            loop = m.loop,
        )
    }
}

/**
 * Minimal JSON object writer for outgoing messages. Numbers never depend on the locale.
 * Field order is insertion order, starting with `type`.
 */
class JsonWriter(type: String) {
    private val sb = StringBuilder(256)
    private var first = true

    init {
        sb.append('{')
        field("type", type)
    }

    private fun key(key: String) {
        if (!first) sb.append(',')
        first = false
        writeString(sb, key)
        sb.append(':')
    }

    fun field(key: String, value: String?): JsonWriter {
        key(key)
        if (value == null) sb.append("null") else writeString(sb, value)
        return this
    }

    fun field(key: String, value: Double?): JsonWriter {
        key(key)
        if (value == null) sb.append("null") else writeNumber(sb, value)
        return this
    }

    fun field(key: String, value: Long): JsonWriter {
        key(key)
        sb.append(value)
        return this
    }

    fun field(key: String, value: Boolean): JsonWriter {
        key(key)
        sb.append(if (value) "true" else "false")
        return this
    }

    /** Insert pre-serialized JSON (object, array or null). */
    fun raw(key: String, json: String?): JsonWriter {
        key(key)
        sb.append(if (json.isNullOrEmpty()) "null" else json)
        return this
    }

    override fun toString(): String = sb.toString() + "}"

    companion object {
        fun writeNumber(sb: StringBuilder, value: Double) {
            when {
                value.isNaN() || value.isInfinite() -> sb.append("null")
                value == Math.rint(value) && Math.abs(value) < 1e15 -> sb.append(value.toLong())
                else -> sb.append(value.toString())
            }
        }

        fun writeString(sb: StringBuilder, s: String) {
            sb.append('"')
            for (c in s) {
                when {
                    c == '"' -> sb.append("\\\"")
                    c == '\\' -> sb.append("\\\\")
                    c == '\n' -> sb.append("\\n")
                    c == '\r' -> sb.append("\\r")
                    c == '\t' -> sb.append("\\t")
                    c.code < 0x20 -> sb.append("\\u").append(c.code.toString(16).padStart(4, '0'))
                    else -> sb.append(c)
                }
            }
            sb.append('"')
        }

        fun stringLiteral(s: String): String = StringBuilder().also { writeString(it, s) }.toString()

        fun stringArray(items: Iterable<String>): String {
            val sb = StringBuilder("[")
            var firstItem = true
            for (item in items) {
                if (!firstItem) sb.append(',')
                firstItem = false
                writeString(sb, item)
            }
            return sb.append(']').toString()
        }
    }
}

// ------------------------------------------------------------------ JSON reading

private fun Map<String, Any?>.str(k: String): String? = this[k] as? String
private fun Map<String, Any?>.dbl(k: String, default: Double = 0.0): Double = (this[k] as? Double) ?: default
private fun Map<String, Any?>.long(k: String): Long = (this[k] as? Double)?.toLong() ?: 0L
private fun Map<String, Any?>.int(k: String): Int = (this[k] as? Double)?.toInt() ?: 0
private fun Map<String, Any?>.bool(k: String): Boolean = (this[k] as? Boolean) ?: false
private fun Map<String, Any?>.list(k: String): List<Any?> = (this[k] as? List<*>) ?: emptyList<Any?>()

@Suppress("UNCHECKED_CAST")
private fun Map<String, Any?>.obj(k: String): Map<String, Any?>? = this[k] as? Map<String, Any?>

/**
 * Small JSON reader: objects become Map, arrays List, numbers Double, plus String, Boolean, null.
 * Enough for the protocol, so core needs no JSON dependency.
 */
internal object Json {
    @Suppress("UNCHECKED_CAST")
    fun parseObject(text: String): Map<String, Any?>? =
        try {
            Reader(text).readDocument() as? Map<String, Any?>
        } catch (e: IllegalArgumentException) {
            null
        }

    private class Reader(private val s: String) {
        private var i = 0

        fun readDocument(): Any? {
            val v = readValue()
            skipWs()
            require(i == s.length) { "trailing data" }
            return v
        }

        private fun skipWs() {
            while (i < s.length && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) i++
        }

        private fun readValue(): Any? {
            skipWs()
            require(i < s.length) { "unexpected end" }
            return when (val c = s[i]) {
                '{' -> readObject()
                '[' -> readArray()
                '"' -> readString()
                't' -> literal("true", true)
                'f' -> literal("false", false)
                'n' -> literal("null", null)
                else -> {
                    require(c == '-' || c in '0'..'9') { "unexpected '$c'" }
                    readNumber()
                }
            }
        }

        private fun literal(word: String, value: Any?): Any? {
            require(s.startsWith(word, i)) { "bad literal" }
            i += word.length
            return value
        }

        private fun readNumber(): Double {
            val start = i
            while (i < s.length && (s[i] in '0'..'9' || s[i] in "+-.eE")) i++
            return s.substring(start, i).toDoubleOrNull() ?: throw IllegalArgumentException("bad number")
        }

        private fun readString(): String {
            i++ // opening quote
            val sb = StringBuilder()
            while (true) {
                require(i < s.length) { "unterminated string" }
                val c = s[i++]
                when (c) {
                    '"' -> return sb.toString()
                    '\\' -> {
                        require(i < s.length) { "bad escape" }
                        when (val e = s[i++]) {
                            '"', '\\', '/' -> sb.append(e)
                            'b' -> sb.append('\b')
                            'f' -> sb.append('\u000c')
                            'n' -> sb.append('\n')
                            'r' -> sb.append('\r')
                            't' -> sb.append('\t')
                            'u' -> {
                                require(i + 4 <= s.length) { "bad unicode escape" }
                                val code = s.substring(i, i + 4).toIntOrNull(16)
                                    ?: throw IllegalArgumentException("bad unicode escape")
                                sb.append(code.toChar())
                                i += 4
                            }
                            else -> throw IllegalArgumentException("bad escape")
                        }
                    }
                    else -> sb.append(c)
                }
            }
        }

        private fun readArray(): List<Any?> {
            i++
            val list = ArrayList<Any?>()
            skipWs()
            if (i < s.length && s[i] == ']') { i++; return list }
            while (true) {
                list.add(readValue())
                skipWs()
                require(i < s.length) { "unterminated array" }
                val c = s[i++]
                if (c == ']') return list
                require(c == ',') { "expected ','" }
            }
        }

        private fun readObject(): Map<String, Any?> {
            i++
            val map = LinkedHashMap<String, Any?>()
            skipWs()
            if (i < s.length && s[i] == '}') { i++; return map }
            while (true) {
                skipWs()
                require(i < s.length && s[i] == '"') { "expected key" }
                val key = readString()
                skipWs()
                require(i < s.length && s[i++] == ':') { "expected ':'" }
                map[key] = readValue()
                skipWs()
                require(i < s.length) { "unterminated object" }
                val c = s[i++]
                if (c == '}') return map
                require(c == ',') { "expected ','" }
            }
        }
    }
}
