package com.syncvr.player.core

import com.syncvr.player.core.sync.Json
import com.syncvr.player.core.sync.JsonWriter
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class TelemetryTest {
    private fun json(t: Telemetry) = Json.parseObject(JsonWriter("status").also { t.writeTo(it) }.toString())!!

    @Test fun writesServerFieldNames() {
        val o = json(Telemetry(0.85, true, 31.5, 20L * 1024 * 1024 * 1024, -58, true))
        assertEquals(0.85, o["battery"])
        assertEquals(true, o["charging"])
        assertEquals(31.5, o["temp_c"])
        assertEquals(20.0 * 1024 * 1024 * 1024, o["storage_free"])
        assertEquals(-58.0, o["wifi_rssi"])
        assertEquals(true, o["worn"])
    }

    @Test fun unknownValues() {
        val o = json(Telemetry())
        assertEquals(-1.0, o["battery"])
        assertTrue(o.containsKey("temp_c") && o["temp_c"] == null)
        assertEquals(-1.0, o["storage_free"])
        assertEquals(0.0, o["wifi_rssi"])
        assertFalse(o.containsKey("worn"))
    }

    @Test fun batteryConversions() {
        assertEquals(0.5, TelemetryMath.batteryFraction(50, 100))
        assertEquals(1.0, TelemetryMath.batteryFraction(120, 100))
        assertEquals(-1.0, TelemetryMath.batteryFraction(-1, 100))
        assertEquals(-1.0, TelemetryMath.batteryFraction(50, 0))
        assertTrue(TelemetryMath.isCharging(2))
        assertTrue(TelemetryMath.isCharging(5))
        assertFalse(TelemetryMath.isCharging(3))
        assertEquals(31.5, TelemetryMath.temperatureC(315))
        assertNull(TelemetryMath.temperatureC(Int.MIN_VALUE))
    }

    @Test fun wifiAndStorageAndWorn() {
        assertEquals(-60, TelemetryMath.wifiRssi(-60))
        assertEquals(0, TelemetryMath.wifiRssi(-127))
        assertEquals(0, TelemetryMath.wifiRssi(0))
        assertEquals(-1L, TelemetryMath.storageFree(-5))
        assertEquals(10L, TelemetryMath.storageFree(10))
        assertTrue(TelemetryMath.isWorn(0f, 5f))
        assertFalse(TelemetryMath.isWorn(5f, 5f))
    }

    @Test fun parsesNativeFps() {
        assertEquals(71.9, FrameRate.parse("VR on, 71.9 fps"))
        assertEquals(72.0, FrameRate.parse("VR on, 72.0 fps"))
        assertNull(FrameRate.parse("VR mode off"))
        assertNull(FrameRate.parse(null))
    }

    @Test fun cacheSamplesOncePerInterval() {
        var t = 0.0
        var n = 0
        val c = TelemetryCache(5.0, { t }) { n++; Telemetry(battery = n / 10.0) }
        assertEquals(0.1, c.get().battery)
        t = 4.9
        assertEquals(0.1, c.get().battery)
        t = 5.0
        assertEquals(0.2, c.get().battery)
        assertEquals(2, n)
    }
}
