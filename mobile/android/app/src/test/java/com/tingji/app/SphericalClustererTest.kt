package com.tingji.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import java.util.Random

class SphericalClustererTest {
    private fun points(counts: IntArray): List<FloatArray> {
        val random = Random(91)
        return counts.flatMapIndexed { group, count ->
            List(count) { SphericalClusterer.normalize(FloatArray(8) { d -> (if (d == group) 1f else 0f) + (random.nextGaussian() * .02).toFloat() }) }
        }
    }
    @Test fun keepsSmallSpeakerInUnevenMeeting() {
        val data = points(intArrayOf(120, 8))
        val result = SphericalClusterer.fit(data, 2)
        val groups = data.map { v -> result.centers.indices.maxBy { SphericalClusterer.dot(v, result.centers[it]) } }
        assertNotEquals(groups.first(), groups.last())
        assertEquals(120, groups.count { it == groups.first() })
        assertEquals(8, groups.count { it == groups.last() })
    }
    @Test fun automaticCountAndIdenticalVoiceSafeguard() {
        assertEquals(1, SphericalClusterer.fit(points(intArrayOf(60)), 0).centers.size)
        assertEquals(2, SphericalClusterer.fit(points(intArrayOf(60, 20)), 0).centers.size)
        val identical = List(8) { floatArrayOf(1f, 0f) }
        assertThrows(IllegalArgumentException::class.java) { SphericalClusterer.fit(identical, 2) }
    }
    @Test fun cancellationAndInvalidVectors() {
        assertThrows(com.tingji.app.engine.TaskCancelled::class.java) { SphericalClusterer.fit(points(intArrayOf(20, 20)), 2) { true } }
        assertThrows(IllegalArgumentException::class.java) { SphericalClusterer.normalize(floatArrayOf(0f, 0f)) }
    }
}
