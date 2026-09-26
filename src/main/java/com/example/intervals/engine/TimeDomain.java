package com.example.intervals.engine;

import com.example.intervals.error.ErrorCode;
import com.example.intervals.error.IntervalException;

import java.time.Instant;
import java.time.format.DateTimeParseException;

/**
 * The absolute-time domain.
 *
 * <p>Endpoint tokens are ISO-8601 date-time strings accepted in any of these
 * forms:
 * <ul>
 *   <li>instant: {@code 2024-03-31T22:00:00Z};</li>
 *   <li>offset date-time: {@code 2024-04-01T00:00:00+02:00};</li>
 *   <li>zoned date-time: {@code 2024-04-01T00:00:00+02:00[Europe/Paris]}.</li>
 * </ul>
 * All forms are normalized to a {@link Instant} on the UTC time line, so the
 * ordering is independent of the zone in which a boundary was written. Output
 * is canonical: {@link Instant#toString()} (UTC, trailing {@code Z}).
 */
public final class TimeDomain implements Domain<Instant> {

    public static final String ID = "time";

    @Override
    public String id() {
        return ID;
    }

    @Override
    public String description() {
        return "ISO-8601 instants / offset / zoned date-times, ordered on the UTC time line";
    }

    @Override
    public Instant parseEndpoint(String raw) {
        if (raw == null || raw.isBlank()) {
            throw new IntervalException(ErrorCode.INVALID_ENDPOINT,
                    "time endpoint must be a non-empty ISO-8601 date-time string");
        }
        String text = raw.trim();
        try {
            return Instant.parse(text);
        } catch (DateTimeParseException ignored) {
            // fall through to richer forms
        }
        try {
            return java.time.OffsetDateTime.parse(text).toInstant();
        } catch (DateTimeParseException ignored) {
            // fall through
        }
        try {
            return java.time.ZonedDateTime.parse(text).toInstant();
        } catch (DateTimeParseException ignored) {
            // fall through
        }
        throw new IntervalException(ErrorCode.INVALID_ENDPOINT,
                "cannot parse time endpoint (expected ISO-8601, e.g. 2024-03-31T22:00:00Z): " + raw);
    }

    @Override
    public String formatEndpoint(Instant value) {
        return value.toString();
    }
}
