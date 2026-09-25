package streamagg.server;

import streamagg.json.Json;
import streamagg.model.Aggregate;
import streamagg.model.Event;
import streamagg.model.IngestOp;
import streamagg.model.IngestResult;
import streamagg.model.OpType;
import streamagg.model.OutputSnapshot;
import streamagg.model.ResolvedOp;

import java.math.BigDecimal;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Converts between JSON documents and the engine's model types. */
public final class Codec {

    private Codec() {
    }

    // ------------------------------------------------------------------
    // Requests -> model
    // ------------------------------------------------------------------

    public static IngestOp toIngestOp(Json.JsonValue body) {
        if (!body.isObject()) {
            throw new IllegalArgumentException("request body must be a JSON object");
        }
        String opId = body.stringOr("opId");
        if (opId == null) {
            opId = body.stringOr("idempotencyKey");
        }
        String eventId = body.stringOr("eventId");
        if (eventId == null) {
            eventId = body.stringOr("id");
        }
        Json.JsonValue opNode = body.get("op");
        if (opNode == null || !opNode.isString()) {
            throw new IllegalArgumentException("'op' is required (ADD | RETRACT | CORRECT)");
        }
        OpType op = OpType.parse(opNode.asString());
        String key = body.stringOr("key");

        BigDecimal value = body.decimalOr("value");
        if (value == null) {
            Json.JsonValue v = body.get("value");
            if (v != null && v.isString()) {
                try {
                    value = new BigDecimal(v.asString());
                } catch (NumberFormatException e) {
                    throw new IllegalArgumentException("'value' is not a valid number: " + v.asString());
                }
            }
        }

        Long version = body.longOr("version");
        if (version == null) {
            version = body.longOr("ver");
        }

        Long eventTime = parseEventTime(body.get("eventTime"));

        return new IngestOp(opId, eventId, op, key, value, version, eventTime);
    }

    private static Long parseEventTime(Json.JsonValue node) {
        if (node == null) {
            return null;
        }
        if (node.isNumber()) {
            return node.asBigDecimal().longValueExact();
        }
        if (node.isString()) {
            String s = node.asString().trim();
            // epoch millis written as a string
            if (s.matches("-?\\d+")) {
                return Long.parseLong(s);
            }
            try {
                return Instant.parse(s).toEpochMilli();
            } catch (DateTimeParseException e) {
                throw new IllegalArgumentException("'eventTime' must be epoch millis or ISO-8601 instant: " + s);
            }
        }
        throw new IllegalArgumentException("'eventTime' must be epoch millis or ISO-8601 instant");
    }

    // ------------------------------------------------------------------
    // Model -> JSON
    // ------------------------------------------------------------------

    public static Map<String, Object> ingestOpJson(IngestOp op) {
        Map<String, Object> m = new LinkedHashMap<>();
        putIfNonNull(m, "opId", op.opId());
        m.put("eventId", op.eventId());
        m.put("op", op.op().name());
        putIfNonNull(m, "key", op.key());
        if (op.value() != null) {
            m.put("value", op.value());
        }
        putIfNonNull(m, "version", op.version());
        putIfNonNull(m, "eventTime", op.eventTime());
        return m;
    }

    public static Map<String, Object> resultJson(IngestResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("status", r.status().name());
        m.put("message", r.message());
        if (r.appliedVersion() > 0) {
            m.put("appliedVersion", r.appliedVersion());
        }
        m.put("bufferedPending", r.bufferedPending());
        if (!r.drained().isEmpty()) {
            List<Object> drained = new ArrayList<>();
            for (ResolvedOp ro : r.drained()) {
                drained.add(resolvedOpJson(ro));
            }
            m.put("drained", drained);
        }
        return m;
    }

    public static Map<String, Object> resolvedOpJson(ResolvedOp r) {
        Map<String, Object> m = new LinkedHashMap<>();
        putIfNonNull(m, "opId", r.opId());
        m.put("eventId", r.eventId());
        m.put("op", r.op().name());
        m.put("version", r.version());
        putIfNonNull(m, "oldKey", r.oldKey());
        if (r.oldValue() != null) {
            m.put("oldValue", r.oldValue());
        }
        putIfNonNull(m, "newKey", r.newKey());
        if (r.newValue() != null) {
            m.put("newValue", r.newValue());
        }
        putIfNonNull(m, "eventTime", r.eventTime());
        putIfNonNull(m, "resolvedTime", r.resolvedTime());
        return m;
    }

    public static Map<String, Object> eventJson(Event e) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("eventId", e.eventId());
        m.put("key", e.key());
        m.put("value", e.value());
        m.put("version", e.version());
        putIfNonNull(m, "createdAt", e.createdAt());
        putIfNonNull(m, "updatedAt", e.updatedAt());
        return m;
    }

    public static Map<String, Object> aggregateJson(String key, Aggregate agg) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("key", key);
        m.put("sum", agg.sum());
        m.put("count", agg.count());
        return m;
    }

    public static Map<String, Object> snapshotJson(OutputSnapshot s) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("sequence", s.sequence());
        putIfNonNull(m, "emittedAt", s.emittedAt());
        putIfNonNull(m, "watermark", s.watermark());

        List<Object> aggs = new ArrayList<>();
        for (Map.Entry<String, BigDecimal> e : s.sums().entrySet()) {
            Map<String, Object> a = new LinkedHashMap<>();
            a.put("key", e.getKey());
            a.put("sum", e.getValue());
            a.put("count", s.counts().get(e.getKey()));
            aggs.add(a);
        }
        m.put("aggregates", aggs);
        m.put("liveEvents", s.liveEvents());
        m.put("pendingOps", s.pendingOps());
        return m;
    }

    private static void putIfNonNull(Map<String, Object> m, String name, Object value) {
        if (value != null) {
            m.put(name, value);
        }
    }
}
