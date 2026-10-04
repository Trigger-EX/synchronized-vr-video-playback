package com.syncvr.player

import android.content.Context
import com.syncvr.player.core.CalibrationStore

/** Calibration values in SharedPreferences. */
class PrefsCalibrationStore(context: Context) : CalibrationStore {
    private val prefs = context.getSharedPreferences("syncvr", Context.MODE_PRIVATE)

    override fun getFloat(key: String, default: Float): Float = prefs.getFloat(key, default)

    override fun putFloats(values: Map<String, Float>) {
        val edit = prefs.edit()
        for ((k, v) in values) edit.putFloat(k, v)
        edit.apply()
    }
}
