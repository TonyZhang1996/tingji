package com.tingji.app.engine

import java.io.File

data class SpeakerTurn(val start: Double, val end: Double, val speaker: String, val uncertain: Boolean = false)

/** Implemented by the Android shell using bundled, offline models. */
interface SpeakerAnalyzer {
    fun analyze(
        wavFile: File, workDir: File, numSpeakers: Int,
        cancelled: () -> Boolean, onProgress: (Int, Int, String) -> Unit
    ): List<SpeakerTurn>
}

class TaskCancelled : RuntimeException("已停止，已完成的进度已保存。")
