package com.syncvr.player.core.operator

import com.syncvr.player.core.sync.Json
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

const val DEFAULT_HTTP_PORT = 8080

/** Where the operator app talks to: the server's dashboard port, plus the optional password. */
data class ServerEndpoint(val host: String, val httpPort: Int = DEFAULT_HTTP_PORT, val password: String = "") {
    val baseUrl: String get() = "http://${if (host.contains(':') && !host.startsWith("[")) "[$host]" else host}:$httpPort"
    val label: String get() = "$host:$httpPort"

    companion object {
        /**
         * Parses what an operator types: `192.168.1.5`, `192.168.1.5:8080`, `http://host:8080/`.
         * Returns null for an empty or malformed address.
         */
        fun parse(input: String, password: String = ""): ServerEndpoint? {
            var s = input.trim().removePrefix("http://").removePrefix("https://").trimEnd('/')
            if (s.isEmpty() || s.any { it.isWhitespace() || it == '/' }) return null
            var port = DEFAULT_HTTP_PORT
            val colon = s.lastIndexOf(':')
            if (colon >= 0 && s.count { it == ':' } == 1) {
                port = s.substring(colon + 1).toIntOrNull() ?: return null
                s = s.substring(0, colon)
            }
            if (s.isEmpty() || port !in 1..65535) return null
            return ServerEndpoint(s, port, password)
        }
    }
}

class HttpReply(val status: Int, val body: String)

/** One HTTP round trip, behind an interface so the API logic is testable without a network. */
interface HttpTransport {
    /** Throws IOException when the server cannot be reached. */
    @Throws(IOException::class)
    fun request(method: String, url: String, headers: Map<String, String>, body: String?): HttpReply
}

object UrlConnectionTransport : HttpTransport {
    override fun request(method: String, url: String, headers: Map<String, String>, body: String?): HttpReply {
        val c = URL(url).openConnection() as HttpURLConnection
        try {
            c.requestMethod = method
            c.connectTimeout = 4000
            c.readTimeout = 6000
            for ((k, v) in headers) c.setRequestProperty(k, v)
            if (body != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            }
            val code = c.responseCode
            val stream = if (code in 200..399) c.inputStream else c.errorStream
            val text = stream?.use { String(it.readBytes(), Charsets.UTF_8) } ?: ""
            return HttpReply(code, text)
        } finally {
            c.disconnect()
        }
    }
}

/** The server refused or failed a request. [unauthorized] means the password is missing or wrong. */
class ApiException(val status: Int, message: String) : IOException(message) {
    val unauthorized: Boolean get() = status == 401
}

fun basicAuthHeader(password: String): String =
    "Basic " + base64(":$password".toByteArray(Charsets.UTF_8))

// java.util.Base64 needs API 26 and the app's minSdk is 24, so encode by hand.
private fun base64(data: ByteArray): String {
    val table = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    val sb = StringBuilder()
    var i = 0
    while (i < data.size) {
        val b0 = data[i].toInt() and 0xff
        val b1 = if (i + 1 < data.size) data[i + 1].toInt() and 0xff else -1
        val b2 = if (i + 2 < data.size) data[i + 2].toInt() and 0xff else -1
        sb.append(table[b0 shr 2])
        sb.append(table[((b0 and 3) shl 4) or (if (b1 < 0) 0 else b1 shr 4)])
        sb.append(if (b1 < 0) '=' else table[((b1 and 15) shl 2) or (if (b2 < 0) 0 else b2 shr 6)])
        sb.append(if (b2 < 0) '=' else table[b2 and 63])
        i += 3
    }
    return sb.toString()
}

/** Client for the dashboard API in server/syncvr/web.py. Blocking; call it off the UI thread. */
class OperatorApi(val endpoint: ServerEndpoint, private val transport: HttpTransport = UrlConnectionTransport) {

    fun state(): Snapshot {
        val reply = send("GET", "/api/state", null)
        return Snapshot.parse(reply.body) ?: throw ApiException(reply.status, "unexpected reply from the server")
    }

    /** Sends a command; returns the server's warning (e.g. no headset online), if any. */
    fun command(action: String, targets: Targets, params: Map<String, Any?> = emptyMap()): CommandResult {
        val reply = send("POST", "/api/command", JsonWriter.write(commandBody(action, targets, params)))
        val result = Json.parseObject(reply.body)?.get("result") as? Map<*, *>
        return CommandResult(
            targets = (result?.get("targets") as? Double)?.toInt() ?: 0,
            online = (result?.get("online") as? Double)?.toInt() ?: 0,
            warning = result?.get("warning") as? String,
        )
    }

    fun updateDevice(id: String, name: String? = null, group: String? = null) {
        val body = LinkedHashMap<String, Any?>()
        if (name != null) body["name"] = name
        if (group != null) body["group"] = group
        send("POST", "/api/devices/${pathSegment(id)}", JsonWriter.write(body))
    }

    fun forgetDevice(id: String) {
        send("DELETE", "/api/devices/${pathSegment(id)}", null)
    }

    fun rescanLibrary() {
        send("POST", "/api/library/rescan", "{}")
    }

    private fun send(method: String, path: String, body: String?): HttpReply {
        val headers = LinkedHashMap<String, String>()
        if (endpoint.password.isNotEmpty()) headers["Authorization"] = basicAuthHeader(endpoint.password)
        val reply = transport.request(method, endpoint.baseUrl + path, headers, body)
        if (reply.status !in 200..299) throw ApiException(reply.status, errorText(reply))
        return reply
    }

    private fun errorText(reply: HttpReply): String {
        if (reply.status == 401) return "Wrong or missing password"
        val msg = Json.parseObject(reply.body)?.get("error") as? String
        return msg ?: "HTTP ${reply.status}"
    }

    private fun pathSegment(s: String): String = java.net.URLEncoder.encode(s, "UTF-8").replace("+", "%20")

    companion object {
        fun commandBody(action: String, targets: Targets, params: Map<String, Any?>): Map<String, Any?> {
            val body = LinkedHashMap<String, Any?>()
            body["action"] = action
            body["targets"] = when (targets) {
                Targets.All -> "all"
                is Targets.Ids -> targets.ids
            }
            body.putAll(params)
            return body
        }
    }
}

/** Which headsets a command applies to. */
sealed class Targets {
    object All : Targets()
    data class Ids(val ids: List<String>) : Targets()
}

data class CommandResult(val targets: Int, val online: Int, val warning: String?)

/** Minimal JSON encoder for request bodies (maps, lists, strings, numbers, booleans, null). */
internal object JsonWriter {
    fun write(v: Any?): String = StringBuilder().also { append(it, v) }.toString()

    private fun append(sb: StringBuilder, v: Any?) {
        when (v) {
            null -> sb.append("null")
            is String -> quote(sb, v)
            is Boolean -> sb.append(v)
            is Int, is Long -> sb.append(v)
            is Number -> {
                val d = v.toDouble()
                require(d.isFinite()) { "non-finite number" }
                sb.append(if (d == Math.rint(d) && Math.abs(d) < 1e15) d.toLong().toString() else d.toString())
            }
            is Map<*, *> -> {
                sb.append('{')
                var first = true
                for ((k, x) in v) {
                    if (!first) sb.append(',')
                    first = false
                    quote(sb, k.toString()); sb.append(':'); append(sb, x)
                }
                sb.append('}')
            }
            is Iterable<*> -> {
                sb.append('[')
                var first = true
                for (x in v) {
                    if (!first) sb.append(',')
                    first = false
                    append(sb, x)
                }
                sb.append(']')
            }
            else -> quote(sb, v.toString())
        }
    }

    private fun quote(sb: StringBuilder, s: String) {
        sb.append('"')
        for (c in s) {
            when {
                c == '"' -> sb.append("\\\"")
                c == '\\' -> sb.append("\\\\")
                c == '\n' -> sb.append("\\n")
                c == '\r' -> sb.append("\\r")
                c == '\t' -> sb.append("\\t")
                c < ' ' -> sb.append("\\u%04x".format(c.code))
                else -> sb.append(c)
            }
        }
        sb.append('"')
    }
}
