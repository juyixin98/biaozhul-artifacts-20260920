package com.dstexp.model;

import java.util.Locale;

/**
 * Strategy for a local date-time that occurs twice during a fall-back overlap.
 *
 * <p>EARLIER/LATER refer to the resulting instant on the UTC timeline:
 * <ul>
 *   <li>EARLIER - the first pass of the repeated wall-clock time, using the offset
 *       <em>before</em> the transition (daylight/summer offset).</li>
 *   <li>LATER - the second pass, using the offset <em>after</em> the transition
 *       (standard/winter offset).</li>
 *   <li>SKIP - the occurrence is not produced; it is reported in the skipped list.</li>
 *   <li>ERROR - the request fails with error code OVERLAP_ENCOUNTERED.</li>
 * </ul>
 */
public enum OverlapStrategy {
    EARLIER,
    LATER,
    SKIP,
    ERROR;

    public static OverlapStrategy fromJson(String value) {
        if (value == null || value.isBlank()) {
            return LATER;
        }
        try {
            return valueOf(value.trim().toUpperCase(Locale.ROOT));
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException(
                    "Invalid overlap strategy '" + value + "', allowed: earlier, later, skip, error", e);
        }
    }
}
