package com.syncvr.player.core

import com.syncvr.player.core.sync.JsonWriter

/**
 * Headset telemetry reported in `status` (fields and units are in docs/PROTOCOL.md).
 */
data class Telemetry(
    /** 0 to 1, -1 when unknown. */
    val battery: Double = -1.0,
    val charging: Boolean = false,
    /** Battery temperature in degrees Celsius, null when unknown. */
    val tempC: Double? = null,
    /** Free bytes on the video volume, -1 when unknown. */
    val storageFree: Long = -1,
    /** dBm; 0 when unknown (the dashboard hides it). */
    val wifiRssi: Int = 0,
    /** Headset is on a head (proximity sensor); null when the device cannot tell, which omits `worn`. */
    val worn: Boolean? = null,
) {
    fun writeTo(w: JsonWriter) {
        w.field("battery", battery)
            .field("charging", charging)
            .field("temp_c", tempC)
            .field("storage_free", storageFree)
            .field("wifi_rssi", wifiRssi.toLong())
        worn?.let { w.field("worn", it) }
    }
}

/** Conversions from Android's raw values (BatteryManager, WifiInfo, Sensor) to [Telemetry] fields. */
object TelemetryMath {
    const val BATTERY_STATUS_CHARGING = 2 // BatteryManager.BATTERY_STATUS_CHARGING
    const val BATTERY_STATUS_FULL = 5 // BatteryManager.BATTERY_STATUS_FULL
    const val WIFI_INVALID_RSSI = -127 // WifiManager.INVALID_RSSI

    /** ACTION_BATTERY_CHANGED `level` and `scale` extras to 0..1; -1 when either is missing or invalid. */
    fun batteryFraction(level: Int, scale: Int): Double =
        if (level < 0 || scale <= 0) -1.0 else (level.toDouble() / scale).coerceIn(0.0, 1.0)

    fun isCharging(status: Int): Boolean = status == BATTERY_STATUS_CHARGING || status == BATTERY_STATUS_FULL

    /** The `temperature` extra is in tenths of a degree; Int.MIN_VALUE or a missing extra gives null. */
    fun temperatureC(tenths: Int): Double? = if (tenths == Int.MIN_VALUE || tenths <= -1000) null else tenths / 10.0

    /** Android reports -127 (or nothing sensible) when not associated. */
    fun wifiRssi(raw: Int): Int = if (raw >= 0 || raw <= WIFI_INVALID_RSSI) 0 else raw

    /** A proximity sensor reads "near" (below its maximum range) when the headset is on a face. */
    fun isWorn(distance: Float, maxRange: Float): Boolean = distance < maxRange

    /** Bytes free, or -1 for the "unknown" cases (negative values). */
    fun storageFree(bytes: Long): Long = if (bytes < 0) -1 else bytes
}

/** Frame rate from the native render loop's status text ("VR on, 71.9 fps"). */
object FrameRate {
    private val PATTERN = Regex("""(\d+(?:\.\d+)?)\s*fps""")

    fun parse(nativeStatus: String?): Double? =
        nativeStatus?.let { PATTERN.find(it)?.groupValues?.get(1)?.toDoubleOrNull() }
}

/** Samples at most once per [intervalSeconds]; reading battery and Wi-Fi state on every status would be wasteful. */
class TelemetryCache(
    private val intervalSeconds: Double = 5.0,
    private val now: () -> Double,
    private val source: () -> Telemetry,
) {
    private var last: Telemetry? = null
    private var lastAt = Double.NEGATIVE_INFINITY

    fun get(): Telemetry {
        val t = now()
        val cached = last
        if (cached != null && t - lastAt < intervalSeconds) return cached
        val fresh = source()
        last = fresh
        lastAt = t
        return fresh
    }
}
