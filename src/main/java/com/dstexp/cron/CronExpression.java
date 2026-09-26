package com.dstexp.cron;

import java.time.DayOfWeek;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.BitSet;
import java.util.List;

/**
 * A five-field cron expression with minute granularity:
 * <pre>minute hour day-of-month month day-of-week</pre>
 *
 * <p>Semantics (Vixie-cron compatible for the supported subset):
 * <ul>
 *   <li>numeric fields with {@code *}, {@code ,}, {@code -}, {@code /};</li>
 *   <li>month names JAN..DEC and weekday names SUN..SAT;</li>
 *   <li>day-of-week {@code 0} or {@code 7} means Sunday;</li>
 *   <li>when both day-of-month and day-of-week are restricted (not {@code *}), a day matches
 *       when <b>either</b> field matches (OR semantics); otherwise the non-wildcard field gates.</li>
 * </ul>
 *
 * <p>This class produces <em>local</em> date-times; time-zone and DST resolution happen
 * downstream, so cron expansion is independent of zone rules.
 */
public final class CronExpression {

    private final CronField minute;
    private final CronField hour;
    private final CronField dayOfMonth;
    private final CronField month;
    private final CronField dayOfWeek;

    private CronExpression(CronField minute, CronField hour, CronField dayOfMonth,
                           CronField month, CronField dayOfWeek) {
        this.minute = minute;
        this.hour = hour;
        this.dayOfMonth = dayOfMonth;
        this.month = month;
        this.dayOfWeek = dayOfWeek;
    }

    public static CronExpression parse(String expression) {
        if (expression == null || expression.isBlank()) {
            throw new IllegalArgumentException("Cron expression is empty");
        }
        String[] parts = expression.trim().split("\\s+");
        if (parts.length != 5) {
            throw new IllegalArgumentException(
                    "Cron expression must have exactly 5 fields (minute hour dom month dow), got "
                            + parts.length + ": '" + expression + "'");
        }
        // Day-of-week uses a 0..7 parse window; normalise 7 (Sunday) onto 0 below.
        CronField rawDow = CronField.parse(parts[4], "dayOfWeek", 0, 7, CronField.DOW_NAMES);
        BitSet dowBits = new BitSet(7);
        for (int d = 0; d <= 7; d++) {
            if (rawDow.matches(d)) {
                dowBits.set(d == 7 ? 0 : d);
            }
        }
        CronField dow = CronField.fromBits("dayOfWeek", 0, 6, dowBits, rawDow.isWildcard());

        return new CronExpression(
                CronField.parse(parts[0], "minute", 0, 59, CronField.NO_NAMES),
                CronField.parse(parts[1], "hour", 0, 23, CronField.NO_NAMES),
                CronField.parse(parts[2], "dayOfMonth", 1, 31, CronField.NO_NAMES),
                CronField.parse(parts[3], "month", 1, 12, CronField.MONTH_NAMES),
                dow
        );
    }

    /**
     * Enumerates every local date-time matched by this expression on dates in the closed
     * interval {@code [from, to]}, in chronological order.
     */
    public List<LocalDateTime> occurrencesBetween(LocalDate from, LocalDate to) {
        if (from.isAfter(to)) {
            return List.of();
        }
        List<LocalDateTime> out = new ArrayList<>();
        int[] hours = hour.valuesBetween(0, 23);
        int[] minutes = minute.valuesBetween(0, 59);

        for (LocalDate date = from; !date.isAfter(to); date = date.plusDays(1)) {
            if (!month.matches(date.getMonthValue()) || !dayMatches(date)) {
                continue;
            }
            for (int h : hours) {
                for (int m : minutes) {
                    out.add(date.atTime(h, m));
                }
            }
        }
        return out;
    }

    private boolean dayMatches(LocalDate date) {
        boolean domRestricted = !dayOfMonth.isWildcard();
        boolean dowRestricted = !dayOfWeek.isWildcard();

        boolean domMatch = dayOfMonth.matches(date.getDayOfMonth());
        boolean dowMatch = dayOfWeek.matches(cronDow(date));

        if (domRestricted && dowRestricted) {
            return domMatch || dowMatch;
        }
        if (domRestricted) {
            return domMatch;
        }
        if (dowRestricted) {
            return dowMatch;
        }
        return true;
    }

    /** Monday=1 .. Sunday=7 ({@link DayOfWeek}) converted to cron Sunday=0 .. Saturday=6. */
    private static int cronDow(LocalDate date) {
        return date.getDayOfWeek().getValue() % 7;
    }

    @Override
    public String toString() {
        return "CronExpression{" + minute + " " + hour + " " + dayOfMonth + " "
                + month + " " + dayOfWeek + "}";
    }
}
