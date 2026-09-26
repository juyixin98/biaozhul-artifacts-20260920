package com.example.recurrence.core;

import java.time.DayOfWeek;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.EnumSet;
import java.util.List;
import java.util.Set;

/**
 * Expands a {@link RecurrenceRule} into concrete dates inside a window.
 *
 * <p>The expansion is always <b>bounded</b>: dates outside
 * {@code [windowStart, windowEnd]} are not returned, and production stops once
 * more than {@code maxExpansions} in-window occurrences exist (raising
 * {@link ExpansionLimitExceededException}). Out-of-window occurrences before
 * {@code count} still consume count: the n-th occurrence is the n-th from
 * dtStart, not the n-th visible inside the window.
 *
 * <p>MONTHLY anchoring uses the first day of each month (never clamped by
 * {@code plusMonths}), so a 31st-start rule does not drift after a short
 * month: anchors are dtStart's month + k*interval months, and the target
 * day-of-month is resolved afresh each month.
 */
public final class RecurrenceExpander {

    /** Hard ceiling; requests above it are rejected at parsing time. */
    public static final int HARD_MAX_EXPANSIONS = 10_000;
    /** Used when the request omits maxExpansions. */
    public static final int DEFAULT_MAX_EXPANSIONS = 1_000;
    /**
     * Safety bound on candidate periods examined in one expansion. Guards
     * against pathological requests (e.g. dtStart near LocalDate.MIN with a
     * far-future window) that would otherwise iterate for effectively ever.
     */
    static final long MAX_PERIODS = 5_000_000L;

    /**
     * @param rule           validated rule
     * @param windowStart    inclusive start of the reporting window
     * @param windowEnd      inclusive end of the reporting window
     * @param maxExpansions  safety cap on in-window occurrences, 1..HARD_MAX_EXPANSIONS
     * @return in-window occurrence dates in ascending order
     */
    public List<LocalDate> expand(
            RecurrenceRule rule, LocalDate windowStart, LocalDate windowEnd, int maxExpansions) {
        if (windowStart == null || windowEnd == null) {
            throw new ValidationException("MISSING_WINDOW", "windowStart and windowEnd are required");
        }
        if (windowEnd.isBefore(windowStart)) {
            throw new ValidationException("INVALID_WINDOW",
                    "windowEnd (" + windowEnd + ") must not be before windowStart (" + windowStart + ")");
        }
        if (maxExpansions < 1 || maxExpansions > HARD_MAX_EXPANSIONS) {
            throw new ValidationException("INVALID_MAX_EXPANSIONS",
                    "maxExpansions must be between 1 and " + HARD_MAX_EXPANSIONS);
        }

        List<LocalDate> result = new ArrayList<>();
        int produced = 0; // total occurrences produced from dtStart (drives COUNT)
        int emitted = 0;  // occurrences inside the window (drives the limit)

        // Stop iterating once candidates cannot fall in the window or before UNTIL.
        LocalDate hardEnd = rule.until() != null && rule.until().isAfter(windowEnd)
                ? rule.until() : windowEnd;

        LocalDate cursor = firstAnchor(rule);
        long periods = 0;

        while (!cursor.isAfter(hardEnd)) {
            if (++periods > MAX_PERIODS) {
                throw new ValidationException("EXPANSION_TOO_LARGE",
                        "rule spans more than " + MAX_PERIODS
                                + " periods before reaching the window; narrow the window or add count/until");
            }
            List<LocalDate> candidates = candidatesForPeriod(rule, cursor);
            for (LocalDate date : candidates) {
                if (rule.until() != null && date.isAfter(rule.until())) {
                    return result; // UNTIL is inclusive; everything later is out
                }
                if (date.isAfter(hardEnd)) {
                    return result;
                }
                produced++;
                if (rule.count() != null && produced > rule.count()) {
                    return result;
                }
                if (!date.isBefore(windowStart) && !date.isAfter(windowEnd)) {
                    if (emitted >= maxExpansions) {
                        throw new ExpansionLimitExceededException(maxExpansions);
                    }
                    result.add(date);
                    emitted++;
                }
            }
            LocalDate next = advanceAnchor(rule, cursor);
            if (!next.isAfter(cursor)) {
                throw new IllegalStateException("expander did not advance at " + cursor);
            }
            cursor = next;
        }
        return result;
    }

    /** First period anchor. Always the 1st for MONTHLY to avoid day-clamping drift. */
    private LocalDate firstAnchor(RecurrenceRule rule) {
        return switch (rule.freq()) {
            case DAILY -> rule.dtStart();
            case WEEKLY -> rule.dtStart().with(DayOfWeek.MONDAY);
            case MONTHLY -> rule.dtStart().withDayOfMonth(1);
        };
    }

    /** Advance the period anchor by one interval. */
    private LocalDate advanceAnchor(RecurrenceRule rule, LocalDate anchor) {
        return switch (rule.freq()) {
            case DAILY -> anchor.plusDays(rule.interval());
            case WEEKLY -> anchor.plusWeeks(rule.interval());
            // Day 1 + k months never clamps, so no drift across short months.
            case MONTHLY -> anchor.plusMonths(rule.interval());
        };
    }

    /**
     * All candidate dates in the period anchored at {@code anchor}.
     * At most 7 (WEEKLY); DAILY yields the anchor; MONTHLY yields one date or
     * none (SKIP), or the month's last day (CLAMP).
     */
    private List<LocalDate> candidatesForPeriod(RecurrenceRule rule, LocalDate anchor) {
        return switch (rule.freq()) {
            case DAILY -> List.of(anchor);
            case WEEKLY -> weeklyCandidates(rule, anchor);
            case MONTHLY -> monthlyCandidate(rule, anchor);
        };
    }

    private List<LocalDate> weeklyCandidates(RecurrenceRule rule, LocalDate weekMonday) {
        Set<DayOfWeek> days = rule.byDay().isEmpty()
                ? EnumSet.of(rule.dtStart().getDayOfWeek())
                : EnumSet.copyOf(rule.byDay());
        List<LocalDate> out = new ArrayList<>(days.size());
        for (DayOfWeek dow : days) { // EnumSet iterates in ISO order Mon..Sun
            LocalDate date = weekMonday.with(dow);
            if (!date.isBefore(rule.dtStart())) {
                out.add(date);
            }
        }
        return out;
    }

    private List<LocalDate> monthlyCandidate(RecurrenceRule rule, LocalDate monthAnchor) {
        int wantedDay = rule.dtStart().getDayOfMonth();
        int monthLength = monthAnchor.lengthOfMonth();
        if (wantedDay <= monthLength) {
            return List.of(monthAnchor.withDayOfMonth(wantedDay));
        }
        if (rule.monthEndPolicy() == MonthEndPolicy.CLAMP) {
            return List.of(monthAnchor.withDayOfMonth(monthLength));
        }
        return List.of(); // SKIP: short month produces no occurrence
    }
}
