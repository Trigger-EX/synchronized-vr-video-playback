package com.syncvr.operator

import android.content.Context
import android.net.wifi.WifiManager
import com.syncvr.player.core.net.DiscoveryGuard

/**
 * Holds a WifiManager multicast lock while the server is being discovered. Without it Android
 * may drop broadcast/multicast UDP to save power, so the beacon would never arrive.
 * Needs CHANGE_WIFI_MULTICAST_STATE.
 */
class MulticastLockGuard(context: Context) : DiscoveryGuard {
    private val lock = (context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager)
        .createMulticastLock("syncvr-discovery").apply { setReferenceCounted(false) }

    override fun acquire() {
        if (!lock.isHeld) lock.acquire()
    }

    override fun release() {
        if (lock.isHeld) lock.release()
    }
}
