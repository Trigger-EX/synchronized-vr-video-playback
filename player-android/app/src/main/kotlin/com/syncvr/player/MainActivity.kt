package com.syncvr.player

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.widget.TextView
import com.syncvr.player.core.PlayerInfo

/** Placeholder until the VrApi loop lands (Phase 1A step 3). */
class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Log.i(TAG, "SyncVR player ${BuildConfig.VERSION_NAME} (${PlayerInfo.PLAYER}), videos: ${PlayerInfo.VIDEO_DIR}")
        Log.i(TAG, "VrApi ${NativeBridge.vrApiVersion()}")
        setContentView(TextView(this).apply { text = "SyncVR Player ${BuildConfig.VERSION_NAME}" })
    }

    private companion object {
        const val TAG = "SyncVR"
    }
}
