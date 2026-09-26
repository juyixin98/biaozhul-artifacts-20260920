package com.example.timeout.api;

import com.example.timeout.rule.DeadlineRule;
import com.fasterxml.jackson.databind.JsonNode;

import java.time.DateTimeException;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalTime;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.List;

/**
 * 在系统边界解析并校验调度请求。
 *
 * <p>请求形如：
 * <pre>{@code
 * { "id": "t1", "label": "...",
 *   "rule": { "type": "duration", "seconds": 600 } }
 * }</pre>
 * 支持的 rule.type：absolute / duration / dailyLocal / versionTtl。
 */
public record ScheduleRequest(String id, String label, DeadlineRule rule) {

    public static ScheduleRequest parse(JsonNode body) {
        List<String> errors = new ArrayList<>();
        String id = text(body, "id", errors);
        if (id != null && id.isBlank()) {
            errors.add("id must not be blank");
        }
        String label = body.path("label").asText("");

        JsonNode ruleNode = body.get("rule");
        if (ruleNode == null || !ruleNode.isObject()) {
            errors.add("rule is required and must be an object");
        }
        DeadlineRule rule = ruleNode == null ? null : parseRule(ruleNode, errors);

        if (!errors.isEmpty()) {
            throw new BadRequestException(String.join("; ", errors));
        }
        return new ScheduleRequest(id, label, rule);
    }

    private static DeadlineRule parseRule(JsonNode node, List<String> errors) {
        String type = text(node, "type", errors);
        if (type == null) {
            return null;
        }
        try {
            return switch (type) {
                case "absolute" -> DeadlineRule.absolute(
                        Instant.parse(requireText(node, "deadline", errors)));
                case "duration" -> DeadlineRule.duration(
                        parseDuration(node, "duration", "seconds", errors));
                case "dailyLocal" -> {
                    LocalTime time = LocalTime.parse(requireText(node, "localTime", errors));
                    ZoneId zone = ZoneId.of(requireText(node, "zone", errors));
                    yield DeadlineRule.dailyLocal(time, zone);
                }
                case "versionTtl" -> {
                    Instant issuedAt = Instant.parse(requireText(node, "issuedAt", errors));
                    Duration ttl = parseDuration(node, "ttl", "ttlSeconds", errors);
                    yield DeadlineRule.versionTtl(issuedAt, ttl);
                }
                default -> {
                    errors.add("unknown rule.type: " + type
                            + " (expected absolute|duration|dailyLocal|versionTtl)");
                    yield null;
                }
            };
        } catch (DateTimeException | IllegalArgumentException e) {
            errors.add("invalid rule '" + type + "': " + e.getMessage());
            return null;
        }
    }

    /**
     * 解析时长：优先读 ISO-8601 文本字段 {@code isoField}（如 "PT10M"），
     * 否则读数值秒字段 {@code secondsField}。
     */
    private static Duration parseDuration(JsonNode node, String isoField,
                                          String secondsField, List<String> errors) {
        JsonNode iso = node.get(isoField);
        if (iso != null && iso.isTextual() && !iso.asText().isBlank()) {
            return Duration.parse(iso.asText());
        }
        JsonNode seconds = node.get(secondsField);
        if (seconds != null && seconds.isNumber()) {
            return Duration.ofSeconds(seconds.asLong());
        }
        errors.add("duration requires ISO-8601 string '" + isoField
                + "' or a numeric '" + secondsField + "'");
        return Duration.ZERO;
    }

    private static String text(JsonNode node, String field, List<String> errors) {
        JsonNode v = node.get(field);
        if (v == null || !v.isTextual() || v.asText().isBlank()) {
            errors.add(field + " is required");
            return null;
        }
        return v.asText();
    }

    private static String requireText(JsonNode node, String field, List<String> errors) {
        String t = text(node, field, errors);
        return t == null ? "" : t;
    }
}
