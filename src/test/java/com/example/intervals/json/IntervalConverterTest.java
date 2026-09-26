package com.example.intervals.json;

import com.example.intervals.engine.TimeDomain;
import com.example.intervals.engine.VersionDomain;
import com.example.intervals.model.Cut;
import com.example.intervals.model.Interval;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class IntervalConverterTest {

    @Test
    void roundTripsTimeOpenClosedAndInfiniteEnds() {
        IntervalConverter<java.time.Instant> c = new IntervalConverter<>(new TimeDomain());

        IntervalDto dto = new IntervalDto(
                "2024-01-01T00:00:00Z", true,
                "2024-06-30T23:59:59Z", false);
        Interval<java.time.Instant> iv = c.toModel(dto);
        assertEquals(Cut.Kind.ABOVE, iv.lowerCut().kind());
        assertEquals(Cut.Kind.ABOVE, iv.upperCut().kind()); // closed upper

        IntervalDto back = c.toDto(iv);
        assertEquals("2024-01-01T00:00:00Z", back.lower());
        assertTrue(back.lowerOpen());
        assertEquals("2024-06-30T23:59:59Z", back.upper());
        assertFalse(back.upperOpen());
    }

    @Test
    void nullEndpointsMapToInfiniteCuts() {
        IntervalConverter<java.time.Instant> c = new IntervalConverter<>(new TimeDomain());
        Interval<java.time.Instant> iv = c.toModel(new IntervalDto(null, false, null, false));
        assertTrue(iv.lowerCut().isInfinite());
        assertTrue(iv.upperCut().isInfinite());

        IntervalDto back = c.toDto(iv);
        assertNull(back.lower());
        assertNull(back.upper());
        assertFalse(back.lowerOpen());
        assertFalse(back.upperOpen());
    }

    @Test
    void roundTripsVersionTokens() {
        IntervalConverter<String> c = new IntervalConverter<>(new VersionDomain());
        Interval<String> iv = c.toModel(new IntervalDto("v01", false, "v05", true));
        IntervalDto back = c.toDto(iv);
        assertEquals("v01", back.lower());
        assertEquals("v05", back.upper());
        assertFalse(back.lowerOpen());
        assertTrue(back.upperOpen());
    }
}
