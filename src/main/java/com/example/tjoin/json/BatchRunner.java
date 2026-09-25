package com.example.tjoin.json;

import com.example.tjoin.join.IntervalJoinOperator;
import com.example.tjoin.model.BufferOverflowPolicy;
import com.example.tjoin.model.JoinConfig;
import com.example.tjoin.model.ProcessResult;
import com.example.tjoin.model.SideName;
import com.example.tjoin.model.StreamEvent;
import com.example.tjoin.time.ManualClock;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * File-based JSON input/output runner: runs one scenario against the
 * streaming operator with a fully manual clock and prints the resulting
 * emissions plus final status as JSON. No HTTP server or external system
 * is involved — convenient for scripts and for deterministic acceptance
 * demos.
 *
 * <p>Usage: {@code java -cp <jar> com.example.tjoin.json.BatchRunner scenario.json}</p>
 *
 * <p>Scenario shape (see examples/scenario.json):
 * <pre>
 * {
 *   "config": {
 *     "lowerBound": 0, "upperBound": 10000,
 *     "left":  {"maxOutOfOrderness":0,"idleTimeoutMillis":5000,
 *               "maxBufferSize":100,"overflowPolicy":"REJECT"},
 *     "right": { ... }
 *   },
 *   "steps": [
 *     {"type":"event","side":"left","processingTime":1000,
 *      "event":{"id":"L1","key":"k","eventTime":10000,"value":42}},
 *     {"type":"watermark","side":"right","processingTime":2000,"watermark":9000},
 *     {"type":"advanceTime","to":8000}
 *   ]
 * }
 * </pre>
 */
public final class BatchRunner {

    private final ObjectMapper mapper = new ObjectMapper();

    public Map<String, Object> run(JsonNode scenario) {
        JoinConfig config = parseConfig(scenario.get("config"));
        ManualClock clock = new ManualClock(0L);
        IntervalJoinOperator op = new IntervalJoinOperator(config, clock, clock);

        List<Object> stepResults = new ArrayList<>();
        JsonNode steps = scenario.get("steps");
        if (steps != null && steps.isArray()) {
            for (JsonNode step : steps) {
                stepResults.add(applyStep(step, clock, op));
            }
        }
        op.onProcessingTimeTick();

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("ok", true);
        out.put("steps", stepResults);
        out.put("final", statusOf(clock, op));
        return out;
    }

    private Map<String, Object> applyStep(JsonNode step, ManualClock clock,
                                          IntervalJoinOperator op) {
        String type = step.path("type").asText("event");
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("type", type);

        if (step.hasNonNull("processingTime")) {
            clock.advanceTo(Math.max(step.get("processingTime").asLong(),
                    clock.currentTimeMillis()));
        }

        switch (type) {
            case "event" -> {
                SideName side = parseSide(step.path("side").asText("left"));
                StreamEvent event = parseEvent(step.get("event"));
                ProcessResult pr = op.processEvent(side, event);
                result.put("side", side.name());
                result.put("id", event.getId());
                result.put("status", pr.status().name());
                result.put("emitted", pr.emitted());
                if (pr.evicted() != null) {
                    result.put("evictedId", pr.evicted().getId());
                }
                result.put("cleaned", pr.cleaned());
            }
            case "watermark" -> {
                SideName side = parseSide(step.path("side").asText("left"));
                long wm = step.get("watermark").asLong();
                int cleaned = op.advanceWatermark(side, wm);
                result.put("side", side.name());
                result.put("watermark", wm);
                result.put("cleaned", cleaned);
            }
            case "advanceTime" -> {
                long to = step.hasNonNull("to") ? step.get("to").asLong()
                        : clock.currentTimeMillis() + step.path("by").asLong(0);
                int fired = clock.advanceTo(to);
                result.put("now", clock.currentTimeMillis());
                result.put("timersFired", fired);
                op.onProcessingTimeTick();
            }
            case "tick" -> op.onProcessingTimeTick();
            default -> throw new IllegalArgumentException("unknown step type: " + type);
        }
        return result;
    }

    private JoinConfig parseConfig(JsonNode c) {
        if (c == null || c.isNull()) {
            throw new IllegalArgumentException("scenario requires a 'config' object");
        }
        long lower = c.path("lowerBound").asLong(0L);
        long upper = c.path("upperBound").asLong(0L);
        JoinConfig.SideConfig left = parseSide(c.get("left"));
        JoinConfig.SideConfig right = parseSide(c.get("right"));
        if (left == null) {
            left = parseSide(c);
        }
        if (right == null) {
            right = parseSide(c);
        }
        return new JoinConfig(lower, upper, left, right);
    }

    private JoinConfig.SideConfig parseSide(JsonNode n) {
        if (n == null || n.isNull()) {
            return null;
        }
        long ooo = n.path("maxOutOfOrderness").asLong(0L);
        long idle = n.path("idleTimeoutMillis").asLong(0L);
        int cap = n.path("maxBufferSize").asInt(0);
        String policy = n.path("overflowPolicy").asText("REJECT");
        BufferOverflowPolicy p = "DROP_OLDEST".equalsIgnoreCase(policy)
                ? BufferOverflowPolicy.DROP_OLDEST : BufferOverflowPolicy.REJECT;
        return new JoinConfig.SideConfig(ooo, idle, cap, p);
    }

    private StreamEvent parseEvent(JsonNode n) {
        if (n == null || n.isNull()) {
            throw new IllegalArgumentException("event step requires an 'event' object");
        }
        String id = n.path("id").asText(null);
        String key = n.path("key").asText(null);
        if (id == null || id.isEmpty() || key == null || !n.hasNonNull("eventTime")) {
            throw new IllegalArgumentException("event requires non-empty id, key, eventTime");
        }
        Object value = n.has("value")
                ? mapper.convertValue(n.get("value"), Object.class) : null;
        return new StreamEvent(id, key, n.get("eventTime").asLong(), value);
    }

    private SideName parseSide(String s) {
        return "right".equalsIgnoreCase(s) ? SideName.RIGHT : SideName.LEFT;
    }

    private Map<String, Object> statusOf(ManualClock clock, IntervalJoinOperator op) {
        Map<String, Object> status = new LinkedHashMap<>();
        status.put("processingTime", clock.currentTimeMillis());
        status.put("leftWatermark",
                op.leftWatermark() == Long.MIN_VALUE ? null : op.leftWatermark());
        status.put("rightWatermark",
                op.rightWatermark() == Long.MIN_VALUE ? null : op.rightWatermark());
        status.put("leftBuffered", op.leftBufferSize());
        status.put("rightBuffered", op.rightBufferSize());
        var m = op.metrics();
        Map<String, Object> metrics = new LinkedHashMap<>();
        metrics.put("pairsEmitted", m.pairsEmitted.get());
        metrics.put("duplicatesDropped", m.duplicatesDropped.get());
        metrics.put("lateDropped", m.lateDropped.get());
        metrics.put("bufferRejected", m.bufferRejected.get());
        metrics.put("oldestEvicted", m.oldestEvicted.get());
        metrics.put("leftStateCleaned", m.leftStateCleaned.get());
        metrics.put("rightStateCleaned", m.rightStateCleaned.get());
        metrics.put("leftIdle", m.leftIdle);
        metrics.put("rightIdle", m.rightIdle);
        status.put("metrics", metrics);
        return status;
    }

    public static void main(String[] args) throws IOException {
        if (args.length != 1) {
            System.err.println("usage: BatchRunner <scenario.json>");
            System.exit(2);
        }
        String json = Files.readString(Path.of(args[0]), StandardCharsets.UTF_8);
        ObjectMapper mapper = new ObjectMapper();
        JsonNode scenario = mapper.readTree(json);
        Map<String, Object> result = new BatchRunner().run(scenario);

        ObjectMapper out = new ObjectMapper().enable(SerializationFeature.INDENT_OUTPUT);
        System.out.println(out.writeValueAsString(result));
    }
}
