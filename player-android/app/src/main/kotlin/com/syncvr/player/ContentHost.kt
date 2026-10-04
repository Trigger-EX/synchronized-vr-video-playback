package com.syncvr.player

import android.content.Context
import android.os.StatFs
import android.util.Log
import com.syncvr.player.core.content.ContentManager
import com.syncvr.player.core.content.ContentStore
import com.syncvr.player.core.net.ServerConnection
import com.syncvr.player.core.sync.ServerMessage
import java.io.File

/**
 * Android wiring for content management: the same `<external files dir>/videos` folder as the
 * Unity app. Feed server messages to [handle] and call [flush] to send queued replies; the
 * message loop that owns ServerConnection.inbox calls both.
 */
class ContentHost(context: Context, private val inUse: () -> String?) {
    private val dir = File(context.getExternalFilesDir(null), "videos")
    val manager = ContentManager(
        ContentStore(dir) { Log.w(TAG, it) },
        freeBytes = {
            try {
                StatFs(dir.absolutePath).availableBytes
            } catch (e: IllegalArgumentException) {
                -1L
            }
        },
        inUse = inUse,
        onWarning = { Log.w(TAG, it) },
    )

    /** True when [m] was a content message (`sync_content`, `cancel_downloads`, `delete_content`). */
    fun handle(m: ServerMessage): Boolean = manager.handle(m)

    /** Sends `inventory` / `downloads_finished` replies queued by background jobs. */
    fun flush(connection: ServerConnection) {
        while (true) connection.send(manager.outbox.poll() ?: break)
    }

    /** Sent right after connecting. */
    fun sendInventory(connection: ServerConnection) = connection.send(manager.inventoryJson())

    private companion object {
        const val TAG = "SyncVR"
    }
}
