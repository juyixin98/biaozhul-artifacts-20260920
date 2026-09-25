package com.eventorder.engine;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;

/**
 * Parses ISO-8601 timestamps. Both offset forms ({@code 2026-01-01T09:00:00+08:00})
 * and named-zone forms ({@code 2026-01-01T09:00:00+08:00[Asia/Shanghai]}) are accepted;
 * everything is normalized to an {@link Instant} so cross-timezone comparison is exact.
 */
public final class TimeParser {

    private TimeParser() {
    }

    public static Instant parse(String text) {
        if (text == null || text.isBlank()) {
            throw new IllegalArgumentException("timestamp is blank");
        }
        try {
            if (text.contains("[")) {
                return ZonedDateTime.parse(text, DateTimeFormatter.ISO_ZONED_DATE_TIME).toInstant();
            }
            return OffsetDateTime.parse(text, DateTimeFormatter.ISO_OFFSET_DATE_TIME).toInstant();
        } catch (DateTimeParseException e) {
            throw new IllegalArgumentException(
                    "unparseable timestamp '" + text + "' (expected ISO-8601 with offset, e.g. 2026-01-01T09:00:00+08:00)", e);
        }
    }

    /** Formats an instant in UTC for stable, timezone-independent output. */
    public static String formatUtc(Instant instant) {
        return DateTimeFormatter.ISO_INSTANT.format(instant);
    }

    public static boolean isValidZone(String zoneId) {
        try {
            ZoneId.of(zoneId);
            return true;
        } catch (RuntimeException e) {
            return false;
        }
    }
}
