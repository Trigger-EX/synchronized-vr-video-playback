package com.syncvr.player.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class CoreLogicTest {
    @Test fun modeOrdinalsMatchNativeNumbering() {
        assertEquals(
            listOf("EQUIRECT_MONO", "SPHERE_FALLBACK", "CYLINDER_FLAT", "EQUIRECT_STEREO_TB"),
            DisplayMode.values().map { it.name },
        )
    }

    @Test fun labelsAreNumbered() {
        assertEquals("1/4 Equirect layer (mono)", DisplayMode.EQUIRECT_MONO.label())
        assertEquals("4/4 Equirect layer (stereo top/bottom)", DisplayMode.EQUIRECT_STEREO_TB.label())
    }

    @Test fun onlySphereSkipsCompositorSurface() {
        assertEquals(listOf(true, false, true, true), DisplayMode.values().map { it.usesCompositorSurface })
    }

    @Test fun fromIndexWraps() {
        assertEquals(DisplayMode.EQUIRECT_MONO, DisplayMode.fromIndex(4))
        assertEquals(DisplayMode.EQUIRECT_STEREO_TB, DisplayMode.fromIndex(-1))
    }

    @Test fun cycleSwitchesEveryInterval() {
        val c = ModeCycle()
        assertEquals(DisplayMode.EQUIRECT_MONO, c.modeAt(0))
        assertEquals(DisplayMode.EQUIRECT_MONO, c.modeAt(14_999))
        assertEquals(DisplayMode.SPHERE_FALLBACK, c.modeAt(15_000))
        assertEquals(DisplayMode.CYLINDER_FLAT, c.modeAt(30_000))
        assertEquals(DisplayMode.EQUIRECT_STEREO_TB, c.modeAt(45_000))
        assertEquals(DisplayMode.EQUIRECT_MONO, c.modeAt(60_000))
        assertEquals(DisplayMode.EQUIRECT_MONO, c.modeAt(-5))
    }

    @Test fun nextWraps() {
        val c = ModeCycle()
        assertEquals(DisplayMode.SPHERE_FALLBACK, c.next(DisplayMode.EQUIRECT_MONO))
        assertEquals(DisplayMode.EQUIRECT_MONO, c.next(DisplayMode.EQUIRECT_STEREO_TB))
    }

    @Test fun videoSelectionPicksFirstAlphabetical() {
        assertEquals("a.mp4", VideoSelection.pick(listOf("b.mkv", "notes.txt", "a.mp4")))
        assertEquals("Apple.MOV", VideoSelection.pick(listOf("banana.webm", "Apple.MOV")))
    }

    @Test fun videoSelectionIgnoresNonVideos() {
        assertNull(VideoSelection.pick(emptyList()))
        assertNull(VideoSelection.pick(listOf("readme.txt", "mp4", ".mp4", "clip.mp4.part", "x.")))
        assertTrue(VideoSelection.isVideo("clip.WebM"))
        assertFalse(VideoSelection.isVideo("clip.avi"))
    }

    @Test fun timeFormatting() {
        assertEquals("0:00", PanelText.formatTime(-1))
        assertEquals("0:09", PanelText.formatTime(9_999))
        assertEquals("1:05", PanelText.formatTime(65_000))
        assertEquals("1:01:01", PanelText.formatTime(3_661_000))
    }

    @Test fun videoLine() {
        assertEquals("No video", PanelText.videoLine(null, 0, 0))
        assertEquals("a.mp4  0:12 / 3:45", PanelText.videoLine("a.mp4", 12_000, 225_000))
        assertEquals("a.mp4  0:00 / --:--", PanelText.videoLine("a.mp4", 0, -1))
    }
}
