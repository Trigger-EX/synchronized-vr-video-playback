package com.syncvr.player

import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.util.Log

/**
 * Toggles kiosk mode from adb:
 *   adb shell am broadcast -n com.syncvr.player/.KioskReceiver --ez on true|false
 *
 * `on` enables the HOME activity-alias. `off` disables it and clears this app's saved
 * "Always" home choice (not `pm clear`, which would wipe config.json).
 * Protected by android.permission.DUMP in the manifest, which shell holds and apps do not.
 */
class KioskReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val on = intent.getBooleanExtra("on", false)
        val pm = context.packageManager
        val alias = ComponentName(context.packageName, "com.syncvr.player.HomeAlias")
        pm.setComponentEnabledSetting(
            alias,
            if (on) PackageManager.COMPONENT_ENABLED_STATE_ENABLED
            else PackageManager.COMPONENT_ENABLED_STATE_DISABLED,
            PackageManager.DONT_KILL_APP,
        )
        if (!on) pm.clearPackagePreferredActivities(context.packageName)
        Log.i("SyncVR", "kiosk ${if (on) "on" else "off"}")
    }
}
