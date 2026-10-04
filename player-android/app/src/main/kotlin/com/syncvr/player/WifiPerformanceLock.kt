package com.syncvr.player

import android.content.Context
import android.net.wifi.WifiManager

/**
 * Keeps Wi-Fi in high-performance mode while held. Power saving adds large, random latency that
 * hurts clock sync. Needs WAKE_LOCK and ACCESS_WIFI_STATE.
 */
@Suppress("DEPRECATION") // WIFI_MODE_FULL_HIGH_PERF is deprecated from API 34 but still honoured on the Go (API 25).
class WifiPerformanceLock(context: Context) {
    private val lock = (context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager)
        .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "syncvr-wifi").apply { setReferenceCounted(false) }

    fun acquire() {
        if (!lock.isHeld) lock.acquire()
    }

    fun release() {
        if (lock.isHeld) lock.release()
    }
}
