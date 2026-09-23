package com.example.watermark.service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.example.watermark.json.Json;
import com.example.watermark.time.Clock;
import com.example.watermark.time.ManualScheduler;
import com.example.watermark.time.VirtualClock;
import com.example.watermark.watermarks.LateEvent;
import com.example.watermark.watermarks.StreamEvent;
import com.example.watermark.watermarks.Watermark;
import com.example.watermark.watermarks.WatermarkConfig;
import com.example.watermark.watermarks.WatermarkManager;
import com.example.watermark.watermarks.WatermarkStrategy;
import com.example.watermark.windowing.TumblingWindowProcessor;
import com.example.watermark.windowing.WindowResult;

/**
 * One deterministic simulation: virtual clock + manual scheduler + watermark
 * manager + optional exact tumbling-window reference implementation.
 *
 * <p>A session executes a JSON script of ordered steps
 * ({@code register} / {@code event} / {@code advance} / {@code tick}); the
 * returned JSON reports the full final state plus per-step observations, which
 * is what the acceptance tests and examples use.
 */
public final class WatermarkSession {

    private final String id;
    private final VirtualClock clock;
    private final ManualScheduler scheduler;
    private final WatermarkManager<Object> manager;
    private final TumblingWindowProcessor windows; // nullable
    private final List<Map<String, Object>> stepResults = new ArrayList<>();
    private long prevGlobalWatermark = Watermark.NO_WATERMARK;

    @SuppressWarnings("unchecked")
    public WatermarkSession(String id, Map<String, Object> config) {
        this.id = id;
        long outOfOrderness = asLong(config.get("maxOutOfOrdernessMillis"), 0L);
        long interval = asLong(config.getOrDefault(
                "autoWatermarkIntervalMillis", WatermarkConfig.DEFAULT_EMIT_INTERVAL),
                WatermarkConfig.DEFAULT_EMIT_INTERVAL);
        long idleTimeout = asLong(config.get("idleTimeoutMillis"), 500L);
        long allowedLateness = asLong(config.get("allowedLatenessMillis"), 0L);
        Long windowSize = config.containsKey("windowSizeMillis")
                ? asLong(config.get("windowSizeMillis"), 0L) : null;

        this.clock = new VirtualClock(0L);
        this.scheduler = new ManualScheduler(clock);
        clock.addTickListener(scheduler::runDue);

        WatermarkConfig wmConfig =
                new WatermarkConfig(interval, idleTimeout, allowedLateness);
        WatermarkStrategy<Object> strategy =
                WatermarkStrategy.boundedOutOfOrderness(outOfOrderness, wmConfig);
        this.manager = new WatermarkManager<>(strategy, clock, scheduler);

        Object partitions = config.get("partitions");
        if (partitions instanceof List<?> list) {
            for (Object p : list) {
                manager.registerPartition(String.valueOf(p));
            }
        }

        this.windows = (windowSize != null && windowSize > 0)
                ? new TumblingWindowProcessor(windowSize) : null;

        if (windows != null) {
            manager.addGlobalWatermarkListener(windows::onWatermark);
            manager.addLateEventListener(windows::onLateEvent);
        }
        manager.start();
    }

    /** Execute a parsed script: {@code {"steps": [...]}}. */
    public Map<String, Object> executeScript(Map<String, Object> request) {
        Object rawSteps = request.get("steps");
        if (!(rawSteps instanceof List<?> list)) {
            throw new IllegalArgumentException("request requires a 'steps' array");
        }
        for (Object raw : list) {
            if (!(raw instanceof Map<?, ?>)) {
                throw new IllegalArgumentException("each step must be an object");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> step = (Map<String, Object>) raw;
            executeStep(step);
        }
        return result();
    }

    private void executeStep(Map<String, Object> step) {
        String op = String.valueOf(step.getOrDefault("op", "event"));
        Map<String, Object> observation = new LinkedHashMap<>();
        observation.put("op", op);
        switch (op) {
            case "register" -> {
                String key = requireString(step, "key");
                manager.registerPartition(key);
            }
            case "event" -> handleEvent(step, observation);
            case "advance" -> handleAdvance(step, observation);
            case "tick" -> {
                // Explicitly run one emission/idle tick at the current time.
                manager.emitNow();
            }
            case "snapshot" -> { /* observation only */ }
            default -> throw new IllegalArgumentException("unknown op: " + op);
        }
        addObservation(observation);
    }

    private void handleEvent(Map<String, Object> step, Map<String, Object> observation) {
        String key = requireString(step, "key");
        long timestamp = asLong(step.get("timestamp"), Long.MIN_VALUE);
        if (timestamp == Long.MIN_VALUE) {
            throw new IllegalArgumentException("event step requires a numeric 'timestamp'");
        }
        Object value = step.getOrDefault("value", null);
        StreamEvent event = new StreamEvent(timestamp, key, value);
        boolean onTime = manager.onEvent(event);
        if (onTime && windows != null) {
            windows.onEvent(event);
        }
        observation.put("onTime", onTime);
        observation.put("late", !onTime);
    }

    private void handleAdvance(Map<String, Object> step, Map<String, Object> observation) {
        Long target = step.containsKey("time") ? asLong(step.get("time"), 0L) : null;
        Long duration = step.containsKey("durationMillis")
                ? asLong(step.get("durationMillis"), 0L) : null;
        if (target == null && duration == null) {
            throw new IllegalArgumentException("advance requires 'time' or 'durationMillis'");
        }
        if (target != null) {
            clock.advanceTo(target);
        } else {
            clock.advanceBy(duration);
        }
        observation.put("advancedTo", clock.currentTimeMillis());
    }

    private void addObservation(Map<String, Object> observation) {
        long wm = manager.getGlobalWatermark();
        observation.put("processingTime", clock.currentTimeMillis());
        observation.put("globalWatermark", wm == Watermark.NO_WATERMARK ? null : wm);
        observation.put("watermarkAdvanced",
                prevGlobalWatermark != Watermark.NO_WATERMARK && wm > prevGlobalWatermark);
        prevGlobalWatermark = wm;
        Map<String, String> states = new LinkedHashMap<>();
        for (String k : manager.getPartitionKeys()) {
            states.put(k, manager.getPartitionState(k).name());
        }
        observation.put("partitionStates", states);
        stepResults.add(observation);
    }

    public Map<String, Object> result() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("sessionId", id);
        out.put("snapshot", manager.snapshot().toMap());
        List<Map<String, Object>> late = new ArrayList<>();
        for (LateEvent le : manager.getLateEvents()) {
            Map<String, Object> m = new LinkedHashMap<>();
            StreamEvent e = le.event();
            m.put("timestamp", e.timestamp());
            m.put("key", e.key());
            m.put("value", e.payload());
            m.put("globalWatermark",
                    le.globalWatermark() == Watermark.NO_WATERMARK ? null : le.globalWatermark());
            m.put("arrivalProcessingTimeMillis", le.arrivalProcessingTimeMillis());
            m.put("fromResumedPartition", le.fromResumedPartition());
            late.add(m);
        }
        out.put("lateEvents", late);
        if (windows != null) {
            List<Map<String, Object>> wins = new ArrayList<>();
            for (WindowResult r : windows.getResults()) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("partition", r.partitionKey());
                m.put("windowStart", r.windowStart());
                m.put("windowEnd", r.windowEnd());
                m.put("eventCount", r.eventCount());
                m.put("payloads", r.payloads());
                wins.add(m);
            }
            out.put("windows", wins);
        }
        out.put("steps", stepResults);
        return out;
    }

    public WatermarkManager<Object> manager() {
        return manager;
    }

    public Clock clock() {
        return clock;
    }

    public void close() {
        manager.close();
        if (windows != null) {
            windows.close();
        }
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------

    private static String requireString(Map<String, Object> map, String key) {
        Object v = map.get(key);
        if (v == null) {
            throw new IllegalArgumentException("step requires '" + key + "'");
        }
        return String.valueOf(v);
    }

    private static long asLong(Object v, long fallback) {
        if (v == null) {
            return fallback;
        }
        if (v instanceof Number n) {
            return n.longValue();
        }
        return Long.parseLong(String.valueOf(v));
    }

    /** Convenience: build and run a full request body in one call. */
    public static Map<String, Object> runStandalone(Map<String, Object> request) {
        @SuppressWarnings("unchecked")
        Map<String, Object> config = (Map<String, Object>)
                request.getOrDefault("config", Map.of());
        WatermarkSession session = new WatermarkSession("standalone", config);
        try {
            return session.executeScript(request);
        } finally {
            session.close();
        }
    }

    static String toJson(Object value) {
        return Json.writePretty(value);
    }
}
