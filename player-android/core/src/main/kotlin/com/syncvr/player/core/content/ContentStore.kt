package com.syncvr.player.core.content

import com.syncvr.player.core.sync.ContentFile
import java.io.File
import java.io.IOException
import java.security.MessageDigest

data class InventoryEntry(val name: String, val size: Long)

class DownloadCancelled : IOException("cancelled")

data class SyncResult(val ok: List<String>, val failed: List<String>)

/**
 * The on-device video folder (same as the Unity app: `<external files dir>/videos`, see
 * PlayerInfo.VIDEO_DIR) so `adb push` keeps working. Port of the file logic in ContentManager.cs,
 * plus SHA-256 verification (docs/PROTOCOL.md).
 */
class ContentStore(
    val folder: File,
    private val http: HttpFetcher = JavaHttpFetcher,
    private val log: (String) -> Unit = {},
) {
    @Volatile private var active: java.io.Closeable? = null

    init {
        folder.mkdirs()
    }

    /** Closes the in-flight HTTP response (if any) so a blocked read fails immediately. */
    fun abort() {
        try { active?.close() } catch (_: Exception) {}
    }

    fun pathFor(name: String): File? {
        if (!isSafeName(name)) return null
        val f = File(folder, name)
        return if (f.isFile) f else null
    }

    fun inventory(): List<InventoryEntry> =
        folder.listFiles().orEmpty()
            .filter { it.isFile && isSafeName(it.name) && it.extension.lowercase() in VIDEO_EXTENSIONS }
            .sortedBy { it.name }
            .map { InventoryEntry(it.name, it.length()) }

    /** Bytes still to fetch for [files], counting existing `.part` files as progress. */
    fun bytesMissing(files: List<ContentFile>): Long {
        var missing = 0L
        for (f in files) {
            val name = f.name ?: continue
            if (!isSafeName(name)) continue
            val dest = File(folder, name)
            if (dest.isFile && dest.length() == f.size) continue
            val part = File(folder, name + PART)
            missing += maxOf(0L, f.size - (if (part.isFile) part.length() else 0L))
        }
        return missing
    }

    /** Deletes [names] (and their partial files); returns the ones skipped because [inUse]. */
    fun delete(names: List<String>, inUse: String?): List<String> {
        val skipped = ArrayList<String>()
        for (name in names) {
            if (!isSafeName(name)) continue
            if (name == inUse) {
                skipped.add(name)
                continue
            }
            tryDelete(File(folder, name))
            tryDelete(File(folder, name + PART))
        }
        return skipped
    }

    /**
     * Makes the folder match [files]: downloads what is missing, verifies checksums when given and,
     * if [deleteOthers], removes every other file except [inUse]. [progress] gets (name, received,
     * total). Throws nothing for per-file failures; they are listed in [SyncResult.failed].
     */
    fun sync(
        files: List<ContentFile>,
        deleteOthers: Boolean,
        inUse: String?,
        cancelled: () -> Boolean = { false },
        progress: (String, Long, Long) -> Unit = { _, _, _ -> },
    ): SyncResult {
        if (deleteOthers) {
            val wanted = files.mapNotNull { it.name }.toSet()
            for (f in folder.listFiles().orEmpty()) {
                val name = f.name.removeSuffix(PART)
                if (name !in wanted && name != inUse) tryDelete(f)
            }
        }
        val ok = ArrayList<String>()
        val failed = ArrayList<String>()
        for (f in files) {
            if (cancelled()) break
            val name = f.name
            if (name == null || !isSafeName(name)) {
                failed.add(name ?: "?")
                continue
            }
            try {
                ensure(f, name, cancelled, progress)
                ok.add(name)
            } catch (e: DownloadCancelled) {
                break
            } catch (e: IOException) {
                log("download of $name failed: ${e.message}")
                failed.add(name)
            }
        }
        return SyncResult(ok, failed)
    }

    private fun ensure(f: ContentFile, name: String, cancelled: () -> Boolean, progress: (String, Long, Long) -> Unit) {
        val dest = File(folder, name)
        val sha = f.sha256?.lowercase()?.takeIf { it.isNotEmpty() }
        if (dest.isFile && dest.length() == f.size) {
            if (sha == null || sha256Of(dest) == sha) return
            log("$name has the wrong checksum, downloading again")
            tryDelete(dest)
        }
        for (attempt in 1..2) {
            download(f, name, dest, cancelled, progress)
            if (sha == null || sha256Of(dest) == sha) return
            tryDelete(dest)
            tryDelete(File(folder, name + PART))
        }
        throw IOException("checksum mismatch")
    }

    private fun download(f: ContentFile, name: String, dest: File, cancelled: () -> Boolean, progress: (String, Long, Long) -> Unit) {
        val url = f.url ?: throw IOException("no url")
        val part = File(folder, name + PART)
        var have = if (part.isFile) part.length() else 0L
        if (have > f.size) {
            tryDelete(part)
            have = 0
        }
        progress(name, have, f.size)
        if (have < f.size) {
            val resp0 = http.get(url, have)
            active = resp0
            try { resp0.use { resp ->
                val partial = resp.status == 206
                if (resp.status != 200 && !partial) {
                    if (resp.status == 416) tryDelete(part)
                    throw IOException("HTTP ${resp.status}")
                }
                if (!partial) have = 0
                resp.body.use { input ->
                    java.io.FileOutputStream(part, partial).use { out ->
                        val buf = ByteArray(256 * 1024)
                        while (true) {
                            val n = try {
                                input.read(buf)
                            } catch (e: IOException) {
                                if (cancelled()) throw DownloadCancelled()
                                throw e
                            }
                            if (n <= 0) break
                            if (cancelled()) throw DownloadCancelled()
                            out.write(buf, 0, n)
                            have += n
                            progress(name, have, f.size)
                        }
                    }
                }
            } } finally {
                active = null
            }
        }
        if (have != f.size) throw IOException("got $have bytes, expected ${f.size}")
        tryDelete(dest)
        if (!part.renameTo(dest)) throw IOException("could not rename $part")
    }

    private fun tryDelete(f: File) {
        if (f.exists() && !f.delete()) log("could not delete $f")
    }

    companion object {
        const val PART = ".part"
        val VIDEO_EXTENSIONS = setOf("mp4", "m4v", "mov", "mkv", "webm")

        fun isSafeName(name: String?): Boolean =
            !name.isNullOrEmpty() && name.length <= 200 && !name.startsWith(".") &&
                name.none { it == '/' || it == '\\' || it == '\u0000' } && !name.endsWith(PART)

        fun sha256Of(file: File): String {
            val md = MessageDigest.getInstance("SHA-256")
            file.inputStream().use { s ->
                val buf = ByteArray(256 * 1024)
                while (true) {
                    val n = s.read(buf)
                    if (n < 0) break
                    md.update(buf, 0, n)
                }
            }
            return md.digest().joinToString("") { "%02x".format(it) }
        }
    }
}
