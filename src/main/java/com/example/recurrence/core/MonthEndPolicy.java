package com.example.recurrence.core;

/**
 * Explicit strategy for MONTHLY rules whose day-of-month does not exist in a
 * given month (e.g. the 31st in February, or Feb 29 in a non-leap year).
 *
 * SKIP  - the month is skipped entirely and produces no occurrence.
 * CLAMP - the occurrence is moved to the last day of that month.
 */
public enum MonthEndPolicy {
    SKIP,
    CLAMP;

    public static MonthEndPolicy fromString(String value) {
        for (MonthEndPolicy p : values()) {
            if (p.name().equals(value)) {
                return p;
            }
        }
        throw new ValidationException(
                "INVALID_MONTH_END_POLICY", "monthEndPolicy must be SKIP or CLAMP, got: " + value);
    }
}
