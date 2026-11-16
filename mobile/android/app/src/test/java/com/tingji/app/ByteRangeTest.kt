package com.tingji.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class ByteRangeTest {
    @Test fun fullAndSuffixRanges() {
        assertEquals(ByteRange(0, 99), ByteRange.parse(null, 100))
        assertEquals(ByteRange(10, 99), ByteRange.parse("bytes=10-", 100))
        assertEquals(ByteRange(90, 99), ByteRange.parse("bytes=-10", 100))
        assertEquals(ByteRange(0, 99), ByteRange.parse("bytes=-1000", 100))
        assertEquals(ByteRange(10, 99), ByteRange.parse("bytes=10-1000", 100))
    }

    @Test fun rejectMalformedMultipleAndOutOfBoundsRanges() {
        for (header in listOf("bytes=100-", "bytes=5-4", "bytes=-0", "bytes=-", "bytes=0-5,10-20", "items=1-2", "bytes=999999999999999999999-")) {
            assertThrows(IllegalArgumentException::class.java) { ByteRange.parse(header, 100) }
        }
        assertThrows(IllegalArgumentException::class.java) { ByteRange.parse("bytes=0-", 0) }
        assertEquals(0L, ByteRange.parse(null, 0).length)
    }
}
