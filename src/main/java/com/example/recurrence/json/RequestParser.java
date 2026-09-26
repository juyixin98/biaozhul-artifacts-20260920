package com.example.recurrence.json;

import com.example.recurrence.core.Frequency;
import com.example.recurrence.core.MonthEndPolicy;
import com.example.recurrence.core.RecurrenceRule;
import com.example.recurrence.core.ValidationException;
import com.fasterxml.jackson.databind.JsonNode;

import java.time.DayOfWeek;
import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** Parses a JSON request document into validated domain objects. */
public final class RequestParser {

    private RequestParser() {
    }

    public record ExpandRequest(
            String requestId,
            RecurrenceRule rule,
            LocalDate windowStart,
            LocalDate windowEnd,
            Integer maxExpansions) {

        public int effectiveMaxExpansions() {
            return maxExpansions != null ? maxExpansions
                    : com.example.recurrence.core.RecurrenceExpander.DEFAULT_MAX_EXPANSIONS;
        }
    }

    public static ExpandRequest parse(JsonNode root) {
        requireObject(root, "request body");
        String requestId = textOrNull(root, "requestId");

        JsonNode ruleNode = root.get("rule");
        requireObject(ruleNode, "rule");

        LocalDate dtStart = parseDate(ruleNode, "dtStart", true);
        Frequency freq = Frequency.fromString(requireText(ruleNode, "freq"));
        int interval = intOrDefault(ruleNode, "interval", 1);
        Integer count = optionalPositiveInt(ruleNode, "count", "INVALID_COUNT");
        LocalDate until = parseDate(ruleNode, "until", false);
        List<DayOfWeek> byDay = parseByDay(ruleNode);
        MonthEndPolicy policy = ruleNode.hasNonNull("monthEndPolicy")
                ? MonthEndPolicy.fromString(ruleNode.get("monthEndPolicy").asText())
                : null;

        RecurrenceRule rule = new RecurrenceRule(dtStart, freq, interval, count, until, byDay, policy);

        LocalDate windowStart = parseDate(root, "windowStart", true);
        LocalDate windowEnd = parseDate(root, "windowEnd", true);
        Integer maxExpansions = optionalPositiveInt(root, "maxExpansions", "INVALID_MAX_EXPANSIONS");
        if (maxExpansions != null
                && maxExpansions > com.example.recurrence.core.RecurrenceExpander.HARD_MAX_EXPANSIONS) {
            throw new ValidationException("INVALID_MAX_EXPANSIONS",
                    "maxExpansions must not exceed "
                            + com.example.recurrence.core.RecurrenceExpander.HARD_MAX_EXPANSIONS);
        }

        return new ExpandRequest(requestId, rule, windowStart, windowEnd, maxExpansions);
    }

    private static List<DayOfWeek> parseByDay(JsonNode ruleNode) {
        JsonNode node = ruleNode.get("byDay");
        if (node == null || node.isNull()) {
            return List.of();
        }
        if (!node.isArray()) {
            throw new ValidationException("INVALID_BYDAY", "byDay must be an array of MO,TU,WE,TH,FR,SA,SU");
        }
        List<DayOfWeek> days = new ArrayList<>();
        for (JsonNode item : node) {
            if (!item.isTextual()) {
                throw new ValidationException("INVALID_BYDAY", "byDay entries must be strings");
            }
            days.add(dayOfWeek(item.asText()));
        }
        return days;
    }

    private static final Map<String, DayOfWeek> DAY_CODES = Map.of(
            "MO", DayOfWeek.MONDAY,
            "TU", DayOfWeek.TUESDAY,
            "WE", DayOfWeek.WEDNESDAY,
            "TH", DayOfWeek.THURSDAY,
            "FR", DayOfWeek.FRIDAY,
            "SA", DayOfWeek.SATURDAY,
            "SU", DayOfWeek.SUNDAY);

    private static DayOfWeek dayOfWeek(String code) {
        DayOfWeek dow = DAY_CODES.get(code);
        if (dow == null) {
            throw new ValidationException("INVALID_BYDAY",
                    "byDay entry must be one of MO,TU,WE,TH,FR,SA,SU, got: " + code);
        }
        return dow;
    }

    private static LocalDate parseDate(JsonNode node, String field, boolean required) {
        JsonNode value = node.get(field);
        if (value == null || value.isNull()) {
            if (required) {
                throw new ValidationException("MISSING_FIELD", field + " is required (YYYY-MM-DD)");
            }
            return null;
        }
        if (!value.isTextual()) {
            throw new ValidationException("INVALID_DATE", field + " must be a YYYY-MM-DD string");
        }
        try {
            return LocalDate.parse(value.asText());
        } catch (DateTimeParseException e) {
            throw new ValidationException("INVALID_DATE",
                    field + " is not a valid YYYY-MM-DD date: " + value.asText());
        }
    }

    private static Integer optionalPositiveInt(JsonNode node, String field, String code) {
        JsonNode value = node.get(field);
        if (value == null || value.isNull()) {
            return null;
        }
        if (!value.isIntegralNumber() || value.asLong() > Integer.MAX_VALUE) {
            throw new ValidationException(code, field + " must be a positive integer");
        }
        int parsed = value.asInt();
        if (parsed < 1) {
            throw new ValidationException(code, field + " must be >= 1, got " + parsed);
        }
        return parsed;
    }

    private static int intOrDefault(JsonNode node, String field, int defaultValue) {
        JsonNode value = node.get(field);
        if (value == null || value.isNull()) {
            return defaultValue;
        }
        if (!value.isIntegralNumber()) {
            throw new ValidationException("INVALID_INTEGER", field + " must be an integer");
        }
        return value.asInt();
    }

    private static String requireText(JsonNode node, String field) {
        JsonNode value = node.get(field);
        if (value == null || value.isNull() || !value.isTextual() || value.asText().isBlank()) {
            throw new ValidationException("MISSING_FIELD", field + " is required");
        }
        return value.asText();
    }

    private static String textOrNull(JsonNode node, String field) {
        JsonNode value = node.get(field);
        return value != null && value.isTextual() ? value.asText() : null;
    }

    private static void requireObject(JsonNode node, String what) {
        if (node == null || !node.isObject()) {
            throw new ValidationException("INVALID_REQUEST", what + " must be a JSON object");
        }
    }
}
