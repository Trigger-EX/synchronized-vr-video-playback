package com.syncvr.player

import android.app.Activity
import android.os.Build
import android.os.Bundle
import android.util.Log
import com.syncvr.core.PlayerInfo

/** Placeholder until the native VR loop lands (Phase 1A, step 3). */
class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Log.i(TAG, "started; player=${PlayerInfo.PLAYER} android=${Build.VERSION.RELEASE} " +
            "model=${Build.MODEL} videos=${getExternalFilesDir("videos")}")
    }

    companion object {
        const val TAG = "SyncVR"
    }
}
