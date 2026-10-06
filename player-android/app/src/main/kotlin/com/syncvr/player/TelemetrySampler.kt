package com.syncvr.player

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.StatFs
import com.syncvr.player.core.Telemetry
import com.syncvr.player.core.TelemetryMath
import java.io.File

/**
 * Reads battery, battery temperature, free storage on the video volume, Wi-Fi signal and whether
 * the headset is worn (proximity sensor, "near" = on a face) for the `status` message; the
 * counterpart of DeviceInfo.cs. Call [start]/[stop] around the time the activity is resumed so the
 * sensor is not left running; [sample] is cheap enough for the main thread at a few-second interval.
 */
class TelemetrySampler(context: Context, private val storageDir: File) : SensorEventListener {
    private val app = context.applicationContext
    private val wifi = app.getSystemService(Context.WIFI_SERVICE) as WifiManager
    private val batteryManager = app.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
    private val sensors = app.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private val proximity: Sensor? = sensors.getDefaultSensor(Sensor.TYPE_PROXIMITY)

    @Volatile private var worn: Boolean? = null

    fun start() {
        proximity?.let { sensors.registerListener(this, it, SensorManager.SENSOR_DELAY_NORMAL) }
    }

    fun stop() {
        sensors.unregisterListener(this)
    }

    override fun onSensorChanged(event: SensorEvent) {
        val p = proximity ?: return
        if (event.values.isNotEmpty()) worn = TelemetryMath.isWorn(event.values[0], p.maximumRange)
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    fun sample(): Telemetry {
        // ACTION_BATTERY_CHANGED is sticky: registerReceiver(null, ...) just returns the last value.
        val battery: Intent? = app.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val charging = battery?.let { TelemetryMath.isCharging(it.getIntExtra("status", -1)) } ?: false
        return Telemetry(
            battery = battery?.let {
                TelemetryMath.batteryFraction(it.getIntExtra("level", -1), it.getIntExtra("scale", -1))
            } ?: -1.0,
            charging = charging,
            tempC = battery?.let { TelemetryMath.temperatureC(it.getIntExtra("temperature", Int.MIN_VALUE)) },
            storageFree = freeBytes(),
            wifiRssi = rssi(),
            worn = worn,
            batteryCurrentA = currentA(charging),
        )
    }

    private fun currentA(charging: Boolean): Double? = try {
        TelemetryMath.batteryCurrentA(batteryManager.getLongProperty(BatteryManager.BATTERY_PROPERTY_CURRENT_NOW), charging)
    } catch (e: Exception) {
        null
    }

    private fun freeBytes(): Long = try {
        TelemetryMath.storageFree(StatFs(storageDir.absolutePath).availableBytes)
    } catch (e: IllegalArgumentException) {
        -1L
    }

    @Suppress("DEPRECATION")
    private fun rssi(): Int = try {
        TelemetryMath.wifiRssi(wifi.connectionInfo?.rssi ?: 0)
    } catch (e: SecurityException) {
        0
    }
}
