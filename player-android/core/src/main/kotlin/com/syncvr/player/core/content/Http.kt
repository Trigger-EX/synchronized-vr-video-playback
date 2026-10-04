package com.syncvr.player.core.content

import java.io.Closeable
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL

/** An open HTTP response body. Close it to release the connection. */
interface HttpResponse : Closeable {
    val status: Int
    val body: InputStream
}

/** HTTP behind an interface so the download logic is testable with fakes. */
interface HttpFetcher {
    /** GET [url]; when [rangeStart] > 0 send `Range: bytes=<rangeStart>-`. */
    @Throws(IOException::class)
    fun get(url: String, rangeStart: Long): HttpResponse
}

object JavaHttpFetcher : HttpFetcher {
    override fun get(url: String, rangeStart: Long): HttpResponse {
        val c = URL(url).openConnection() as HttpURLConnection
        c.connectTimeout = 15000
        c.readTimeout = 10000
        if (rangeStart > 0) c.setRequestProperty("Range", "bytes=$rangeStart-")
        val code = c.responseCode
        return object : HttpResponse {
            override val status = code
            override val body: InputStream
                get() = if (code in 200..299) c.inputStream else throw IOException("HTTP $code")
            override fun close() = c.disconnect()
        }
    }
}
