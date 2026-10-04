package com.syncvr.player.core

import com.syncvr.player.core.sync.FakePlayer
import com.syncvr.player.core.sync.SyncEngine
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

private class MemStore : CalibrationStore {
    val map = HashMap<String, Float>()
    var writes = 0
    override fun getFloat(key: String, default: Float) = map[key] ?: default
    override fun putFloats(values: Map<String, Float>) { map.putAll(values); writes++ }
}

class LifecycleTest {
    private val engine = SyncEngine(FakePlayer())
    private val store = MemStore()
    private var t = 0.0
    private val cal = CalibrationPersistence(engine, store, { t })

    @Test
    fun loadsSavedValuesAndKeepsDefaultsWhenMissing() {
        cal.load()
        assertEquals(0.1, engine.startLatency, 1e-6)
        store.map[CalibrationPersistence.KEY_START_LATENCY] = 0.25f
        store.map[CalibrationPersistence.KEY_SEEK_TIME] = 0.8f
        cal.load()
        assertEquals(0.25, engine.startLatency, 1e-6)
        assertEquals(0.8, engine.seekTime, 1e-6)
    }

    @Test
    fun ignoresInsaneValues() {
        store.map[CalibrationPersistence.KEY_START_LATENCY] = -3f
        store.map[CalibrationPersistence.KEY_SEEK_TIME] = 999f
        cal.load()
        assertEquals(0.1, engine.startLatency, 1e-6)
        assertEquals(0.3, engine.seekTime, 1e-6)
    }

    @Test
    fun savesOnlyWhenChangedAndRateLimited() {
        cal.load()
        assertFalse(cal.save())
        engine.startLatency = 0.2
        assertTrue(cal.save())
        assertEquals(0.2f, store.map[CalibrationPersistence.KEY_START_LATENCY])
        assertFalse(cal.save())
        engine.seekTime = 0.5
        cal.tick() // first tick saves immediately
        assertEquals(2, store.writes)
        engine.seekTime = 0.6
        t = 10.0
        cal.tick()
        assertEquals(2, store.writes)
        t = 31.0
        cal.tick()
        assertEquals(3, store.writes)
    }

    @Test
    fun suspendSavesAndResumeResyncsInOrder() {
        val log = ArrayList<String>()
        cal.load()
        val life = AppLifecycle(cal, { log.add("leave") }, { log.add("enter") }, { log.add("pause") },
            { log.add("clock") }, { log.add("cue") })
        engine.startLatency = 0.3
        life.onPause()
        assertEquals(listOf("pause", "leave"), log)
        assertEquals(0.3f, store.map[CalibrationPersistence.KEY_START_LATENCY])
        assertFalse(life.resumed)
        log.clear()
        life.onResume()
        assertEquals(listOf("enter", "clock", "cue"), log)
        assertTrue(life.resumed)
    }
}
