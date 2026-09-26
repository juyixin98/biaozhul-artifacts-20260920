package com.dstexp.model;

import java.util.Locale;

/**
 * Strategy for a local date-time that falls into a spring-forward gap
 * (the local wall-clock time does not exist on that date).
 *
 * <p>EARLIER/LATER refer to the resulting instant on the UTC timeline:
 * <ul>
 *   <li>EARLIER - map as if the offset <em>after</em> the gap applied, yielding the earlier
 *       UTC instant (wall clock pushed back to just before the gap).</li>
 *   <li>LATER - map as if the offset <em>before</em> the gap applied, yielding the later
 *       UTC instant (wall clock pushed forward to just after the gap).</li>
 *   <li>SKIP - the occurrence is not produced; it is reported in the skipped list.</li>
 *   <li>ERROR - the request fails with error code GAP_ENCOUNTERED.</li>
 * </ul>
 */
public enum GapStrategy {
    EARLIER,
    LATER,
    SKIP,
    ERROR;

    public static GapStrategy fromJson(String value) {
        if (value == null || value.isBlank()) {
            return EARLIER;
        }
        try {
            return valueOf(value.trim().toUpperCase(Locale.ROOT));
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException(
                    "Invalid gap strategy '" + value + "', allowed: earlier, later, skip, error", e);
        }
    }
}
