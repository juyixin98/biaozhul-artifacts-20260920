package com.example.drvb.server;

import com.example.drvb.core.Event;
import com.example.drvb.core.Rule;
import com.example.drvb.core.RuleVersion;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;

import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * JSON &lt;-&gt; domain conversion.
 *
 * <p>Event times accept either epoch milliseconds (a JSON number) or an
 * ISO-8601 instant string ({@code 2026-09-23T10:00:00Z}). Event payloads are
 * taken from a {@code data} object when present, otherwise from every
 * top-level field other than {@code id}/{@code type}/{@code eventTime}.
 */
final class JsonCodecs {

    static final ObjectMapper MAPPER = new ObjectMapper()
            .configure(SerializationFeature.INDENT_OUTPUT, true)
            .configure(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS, false);

    private JsonCodecs() {
    }

    static long parseEventTime(JsonNode node) {
        JsonNode t = node.get("eventTime");
        if (t == null || t.isNull()) {
            throw new BadRequestException("eventTime is required");
        }
        if (t.isNumber()) {
            return t.asLong();
        }
        if (t.isTextual()) {
            try {
                return Instant.parse(t.asText()).toEpochMilli();
            } catch (DateTimeParseException e) {
                throw new BadRequestException(
                        "eventTime must be epoch millis or ISO-8601 instant: "
                                + t.asText());
            }
        }
        throw new BadRequestException("eventTime must be a number or ISO-8601 string");
    }

    @SuppressWarnings("unchecked")
    static Event toEvent(JsonNode node) {
        String id = requireText(node, "id");
        String type = requireText(node, "type");
        long eventTime = parseEventTime(node);
        Map<String, Object> payload;
        if (node.hasNonNull("data")) {
            JsonNode data = node.get("data");
            if (!data.isObject()) {
                throw new BadRequestException("data must be a JSON object");
            }
            payload = MAPPER.convertValue(data, Map.class);
        } else {
            payload = new LinkedHashMap<>();
            node.fields().forEachRemaining(e -> {
                String k = e.getKey();
                if (!k.equals("id") && !k.equals("type") && !k.equals("eventTime")) {
                    payload.put(k, MAPPER.convertValue(e.getValue(), Object.class));
                }
            });
        }
        return new Event(id, type, eventTime, payload);
    }

    @SuppressWarnings("unchecked")
    static RuleVersion toVersion(JsonNode node, long createdAt, boolean bootstrap) {
        String versionId = requireText(node, "versionId");
        JsonNode rulesNode = node.get("rules");
        if (rulesNode == null || !rulesNode.isArray() || rulesNode.isEmpty()) {
            throw new BadRequestException("rules must be a non-empty array");
        }
        List<Rule> rules = new ArrayList<>();
        for (JsonNode rn : rulesNode) {
            Map<String, Object> rm = MAPPER.convertValue(rn, Map.class);
            rules.add(Rule.fromMap(rm));
        }
        long effectiveFrom;
        if (bootstrap) {
            effectiveFrom = Long.MIN_VALUE;
        } else {
            JsonNode from = node.get("effectiveFrom");
            if (from == null || from.isNull()) {
                throw new BadRequestException(
                        "effectiveFrom is required when publishing a new version");
            }
            effectiveFrom = parseTimeValue(from, "effectiveFrom");
        }
        String description = node.path("description").asText("");
        return new RuleVersion(versionId, effectiveFrom, createdAt, description, rules);
    }

    static long parseTimeValue(JsonNode node, String field) {
        if (node.isNumber()) {
            return node.asLong();
        }
        if (node.isTextual()) {
            try {
                return Instant.parse(node.asText()).toEpochMilli();
            } catch (DateTimeParseException e) {
                throw new BadRequestException(
                        field + " must be epoch millis or ISO-8601 instant");
            }
        }
        throw new BadRequestException(field + " must be a number or ISO-8601 string");
    }

    static String requireText(JsonNode node, String field) {
        JsonNode v = node.get(field);
        if (v == null || !v.isTextual() || v.asText().isBlank()) {
            throw new BadRequestException(field + " is required");
        }
        return v.asText();
    }

    // --------------------------------------------------------- result views

    static Map<String, Object> matchView(com.example.drvb.stream.IngestResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("eventId", r.event().id());
        m.put("eventType", r.event().type());
        m.put("eventTime", r.event().eventTime());
        m.put("late", r.late());
        m.put("versionId", r.version().versionId());
        m.put("versionChecksum", r.version().checksum());
        List<Map<String, Object>> ms = new ArrayList<>();
        for (var match : r.matches()) {
            Map<String, Object> mm = new LinkedHashMap<>();
            mm.put("ruleId", match.ruleId());
            mm.put("ruleName", match.ruleName());
            mm.put("action", match.action());
            ms.add(mm);
        }
        m.put("matches", ms);
        m.put("watermark", r.watermark());
        m.put("processedAt", r.processedAt());
        return m;
    }

    static Map<String, Object> rejectionView(com.example.drvb.stream.IngestResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("eventId", r.event().id());
        m.put("eventType", r.event().type());
        m.put("eventTime", r.event().eventTime());
        m.put("reason", r.rejectionReason());
        m.put("watermark", r.watermark());
        m.put("processedAt", r.processedAt());
        return m;
    }
}
