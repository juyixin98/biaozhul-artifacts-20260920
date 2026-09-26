package com.example.intervals.engine;

import com.example.intervals.error.IntervalException;
import org.junit.jupiter.api.Test;

import java.time.Instant;
import java.time.ZonedDateTime;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class TimeDomainTest {

    private final TimeDomain domain = new TimeDomain();

    @Test
    void parsesInstantOffsetAndZonedFormsToSameUtcInstant() {
        Instant fromInstant = domain.parseEndpoint("2024-03-31T22:30:00Z");
        Instant fromOffset = domain.parseEndpoint("2024-04-01T00:30:00+02:00");
        Instant fromZoned = domain.parseEndpoint("2024-04-01T00:30:00+02:00[Europe/Paris]");
        assertEquals(fromInstant, fromOffset);
        assertEquals(fromInstant, fromZoned);
    }

    @Test
    void zonedOrderingIsOnUtcTimeLine() {
        // 00:30 in Paris (UTC+2) is earlier than 23:30 in London (UTC+1) same date.
        Instant paris = domain.parseEndpoint("2024-03-31T00:30:00+02:00[Europe/Paris]");
        Instant london = domain.parseEndpoint("2024-03-31T23:30:00+01:00[Europe/London]");
        assertTrue(paris.isBefore(london));
    }

    @Test
    void canonicalOutputIsUtcWithTrailingZ() {
        Instant instant = ZonedDateTime.parse("2024-04-01T00:30:00+02:00[Europe/Paris]").toInstant();
        assertEquals("2024-03-31T22:30:00Z", domain.formatEndpoint(instant));
    }

    @Test
    void rejectsMalformedTokens() {
        IntervalException e = assertThrows(IntervalException.class,
                () -> domain.parseEndpoint("not-a-date"));
        assertTrue(e.getMessage().contains("cannot parse time endpoint"));
        assertThrows(IntervalException.class, () -> domain.parseEndpoint(""));
        assertThrows(IntervalException.class, () -> domain.parseEndpoint(null));
    }
}
