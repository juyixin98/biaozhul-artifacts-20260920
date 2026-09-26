package com.example.recurrence.core;

import java.time.LocalDate;
import java.util.List;
import java.util.Optional;

/**
 * An immutable, validated recurrence rule for the supported subset.
 *
 * <p>Supported subset (intentionally NOT a full RFC 5545 implementation):
 * <ul>
 *   <li>freq: DAILY | WEEKLY | MONTHLY</li>
 *   <li>interval: positive integer (default 1)</li>
 *   <li>count: optional positive integer, maximum number of occurrences</li>
 *   <li>until: optional inclusive cutoff date</li>
 *   <li>byDay: WEEKLY only, subset of
 *       MO,TU,WE,TH,FR,SA,SU (ISO-8601 week order); ignored for other freqs</li>
 *   <li>monthEndPolicy: MONTHLY only, SKIP (default) or CLAMP</li>
 * </ul>
 *
 * @param dtStart        first date of the recurrence (MONDAY for WEEKLY rules)
 * @param freq           frequency
 * @param interval       spacing between periods, &gt;= 1
 * @param count          optional occurrence cap
 * @param until          optional inclusive cutoff
 * @param byDay          WEEKLY only weekday set (ISO order), empty means "the
 *                       dtStart weekday only"
 * @param monthEndPolicy MONTHLY only policy for nonexistent dates
 */
public record RecurrenceRule(
        LocalDate dtStart,
        Frequency freq,
        int interval,
        Integer count,
        LocalDate until,
        List<java.time.DayOfWeek> byDay,
        MonthEndPolicy monthEndPolicy) {

    public RecurrenceRule {
        validate(dtStart, freq, interval, count, until, byDay, monthEndPolicy);
        // Defensive copies keep the record effectively immutable.
        byDay = byDay == null ? List.of() : List.copyOf(byDay);
        monthEndPolicy = monthEndPolicy == null ? MonthEndPolicy.SKIP : monthEndPolicy;
    }

    private static void validate(
            LocalDate dtStart,
            Frequency freq,
            int interval,
            Integer count,
            LocalDate until,
            List<java.time.DayOfWeek> byDay,
            MonthEndPolicy monthEndPolicy) {
        if (dtStart == null) {
            throw new ValidationException("MISSING_DTSTART", "dtStart is required (YYYY-MM-DD)");
        }
        if (freq == null) {
            throw new ValidationException("MISSING_FREQ", "freq is required");
        }
        if (interval < 1) {
            throw new ValidationException("INVALID_INTERVAL", "interval must be >= 1, got " + interval);
        }
        if (count != null && count < 1) {
            throw new ValidationException("INVALID_COUNT", "count must be >= 1, got " + count);
        }
        if (until != null && until.isBefore(dtStart)) {
            throw new ValidationException("UNTIL_BEFORE_DTSTART",
                    "until (" + until + ") must not be before dtStart (" + dtStart + ")");
        }
        if (byDay != null && !byDay.isEmpty() && freq != Frequency.WEEKLY) {
            throw new ValidationException("BYDAY_WEEKLY_ONLY",
                    "byDay is only supported with WEEKLY freq");
        }
        if (freq == Frequency.WEEKLY && byDay != null && !byDay.isEmpty()
                && !byDay.contains(dtStart.getDayOfWeek())) {
            throw new ValidationException("DTSTART_NOT_IN_BYDAY",
                    "dtStart weekday (" + dtStart.getDayOfWeek()
                            + ") must be one of byDay, otherwise the first occurrence is undefined");
        }
        if (monthEndPolicy != null && freq != Frequency.MONTHLY
                && monthEndPolicy != MonthEndPolicy.SKIP) {
            throw new ValidationException("MONTH_END_POLICY_MONTHLY_ONLY",
                    "monthEndPolicy is only meaningful with MONTHLY freq");
        }
    }

    public Optional<Integer> countOpt() {
        return Optional.ofNullable(count);
    }

    public Optional<LocalDate> untilOpt() {
        return Optional.ofNullable(until);
    }
}
