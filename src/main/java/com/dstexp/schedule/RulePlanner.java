package com.dstexp.schedule;

import com.dstexp.cron.CronExpression;
import com.dstexp.model.RuleSpec;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Expands a {@link RuleSpec} into the local wall-clock date-times it denotes within a
 * date range. No time-zone logic happens here.
 */
public final class RulePlanner {

    private static final DateTimeFormatter HM = DateTimeFormatter.ofPattern("H:mm");

    private RulePlanner() {
    }

    public static List<LocalDateTime> plan(RuleSpec spec, LocalDate from, LocalDate to) {
        String type = spec.getType() == null ? "" : spec.getType().trim().toLowerCase(Locale.ROOT);
        return switch (type) {
            case "daily" -> planDaily(spec, from, to);
            case "cron" -> planCron(spec, from, to);
            default -> throw new IllegalArgumentException(
                    "Unsupported rule type '" + spec.getType() + "' for rule '" + spec.getRuleId()
                            + "' (allowed: daily, cron)");
        };
    }

    private static List<LocalDateTime> planDaily(RuleSpec spec, LocalDate from, LocalDate to) {
        LocalTime time = parseLocalTime(spec.getLocalTime(), spec.getRuleId());
        List<LocalDateTime> out = new ArrayList<>();
        for (LocalDate date = from; !date.isAfter(to); date = date.plusDays(1)) {
            out.add(date.atTime(time));
        }
        return out;
    }

    private static List<LocalDateTime> planCron(RuleSpec spec, LocalDate from, LocalDate to) {
        if (spec.getCron() == null || spec.getCron().isBlank()) {
            throw new IllegalArgumentException(
                    "Cron rule '" + spec.getRuleId() + "' is missing the 'cron' field");
        }
        try {
            CronExpression cron = CronExpression.parse(spec.getCron());
            return cron.occurrencesBetween(from, to);
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException(
                    "Invalid cron expression in rule '" + spec.getRuleId() + "': " + e.getMessage(), e);
        }
    }

    /** Accepts {@code HH:mm} and {@code HH:mm:ss}. */
    static LocalTime parseLocalTime(String raw, String ruleId) {
        if (raw == null || raw.isBlank()) {
            throw new IllegalArgumentException(
                    "Daily rule '" + ruleId + "' is missing the 'localTime' field");
        }
        String text = raw.trim();
        try {
            return text.length() <= 5 ? LocalTime.parse(normalizeHM(text), HM) : LocalTime.parse(text);
        } catch (DateTimeParseException e) {
            throw new IllegalArgumentException(
                    "Invalid localTime '" + raw + "' in rule '" + ruleId
                            + "' (expected HH:mm or HH:mm:ss)", e);
        }
    }

    /** Pads a single-digit hour, e.g. {@code 2:30} -> {@code 02:30}. */
    private static String normalizeHM(String text) {
        int colon = text.indexOf(':');
        if (colon == 1) {
            return "0" + text;
        }
        return text;
    }
}
