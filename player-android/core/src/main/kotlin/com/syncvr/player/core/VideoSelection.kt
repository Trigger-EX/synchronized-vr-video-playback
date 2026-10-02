package com.syncvr.player.core

object VideoSelection {
    val EXTENSIONS = setOf("mp4", "mkv", "webm", "mov")

    fun isVideo(name: String): Boolean {
        val dot = name.lastIndexOf('.')
        if (dot <= 0 || dot == name.length - 1) return false
        return name.substring(dot + 1).lowercase() in EXTENSIONS
    }

    /** First video by case-insensitive alphabetical order, or null when there is none. */
    fun pick(names: List<String>): String? =
        names.filter { isVideo(it) && !it.startsWith(".") }
            .sortedWith(compareBy<String> { it.lowercase() }.thenBy { it })
            .firstOrNull()
}
