package com.syncvr.player.core

import com.syncvr.player.core.sync.ServerMessage
import com.syncvr.player.core.sync.VideoCommand
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull

class ViewSpecTest {
    private fun spec(p: String?, s: String?) = ViewSpec.from(p, s)

    @Test
    fun mappingTable() {
        val e = ViewLayer.EQUIRECT
        val c = ViewLayer.CYLINDER
        val table = listOf(
            Triple("360", "mono", ViewSpec(e, ViewStereo.MONO, false)),
            Triple("360", "tb", ViewSpec(e, ViewStereo.TOP_BOTTOM, false)),
            Triple("360", "sbs", ViewSpec(e, ViewStereo.SIDE_BY_SIDE, false)),
            Triple("180", "mono", ViewSpec(e, ViewStereo.MONO, true)),
            Triple("180", "tb", ViewSpec(e, ViewStereo.TOP_BOTTOM, true)),
            Triple("180", "sbs", ViewSpec(e, ViewStereo.SIDE_BY_SIDE, true)),
            Triple("flat", "mono", ViewSpec(c, ViewStereo.MONO, false)),
            Triple("flat", "tb", ViewSpec(c, ViewStereo.TOP_BOTTOM, false)),
            Triple("flat", "sbs", ViewSpec(c, ViewStereo.SIDE_BY_SIDE, false)),
        )
        for ((p, s, want) in table) assertEquals(want, spec(p, s), "$p/$s")
    }

    @Test
    fun caseAndWhitespaceAreIgnored() {
        assertEquals(spec("flat", "sbs"), spec(" FLAT ", "SBS"))
    }

    @Test
    fun unknownValuesFallBackTo360Mono() {
        assertEquals(ViewSpec.DEFAULT, spec("fisheye", "weird"))
        assertEquals(ViewSpec.DEFAULT, spec(null, null))
        assertEquals(ViewSpec.DEFAULT, spec("", ""))
        assertEquals(ViewSpec(ViewLayer.CYLINDER, ViewStereo.MONO, false), spec("flat", "x"))
        assertEquals(ViewSpec(ViewLayer.EQUIRECT, ViewStereo.SIDE_BY_SIDE, false), spec("x", "sbs"))
    }

    @Test
    fun displayModeAndLabel() {
        assertEquals(DisplayMode.CYLINDER_FLAT, spec("flat", "mono").displayMode)
        assertEquals(DisplayMode.EQUIRECT_MONO, spec("180", "tb").displayMode)
        assertEquals("180 sbs", spec("180", "sbs").label())
        assertEquals("flat tb", spec("flat", "tb").label())
        assertEquals("360 mono", ViewSpec.DEFAULT.label())
    }

    @Test
    fun stereoCodesMatchNative() {
        assertEquals(listOf(0, 1, 2), ViewStereo.values().map { it.code })
    }

    @Test
    fun parsesViewMessage() {
        val m = ServerMessage.parse(
            """{"type":"view","video":"a.mp4","projection":"180","stereo":"sbs","rotation":15.5}""",
        )
        assertNotNull(m)
        assertEquals("view", m.type)
        val c = VideoCommand.from(m)
        assertEquals("a.mp4", c.video)
        assertEquals("180", c.projection)
        assertEquals("sbs", c.stereo)
        assertEquals(15.5, c.rotation)
    }

    @Test
    fun viewMessageWithMissingFieldsUsesDefaults() {
        val c = VideoCommand.from(ServerMessage.parse("""{"type":"view","video":"a.mp4"}""")!!)
        assertEquals(ViewSpec.DEFAULT, ViewSpec.from(c.projection, c.stereo))
    }
}
