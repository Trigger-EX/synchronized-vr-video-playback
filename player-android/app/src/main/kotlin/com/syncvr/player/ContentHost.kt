package com.syncvr.player

import android.content.Context
import android.os.StatFs
import android.util.Log
import com.syncvr.player.core.content.ContentManager
import com.syncvr.player.core.content.ContentStore
import java.io.File

/**
 * Android wiring for content management: the `<external files dir>/videos` folder.
 * PlayerController feeds it server messages and flushes its outbox.
 */
class ContentHost(
    context: Context,
    private val inUse: () -> String?,
    private val onWarning: (String) -> Unit = {},
) {
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
        onWarning = { Log.w(TAG, it); onWarning(it) },
    )

    private companion object {
        const val TAG = "SyncVR"
    }
}
