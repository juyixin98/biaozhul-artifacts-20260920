package com.example.tjoin.server;

import com.example.tjoin.join.IntervalJoinOperator;
import com.example.tjoin.model.AcceptStatus;
import com.example.tjoin.model.BufferOverflowPolicy;
import com.example.tjoin.model.JoinConfig;
import com.example.tjoin.model.ProcessResult;
import com.example.tjoin.model.SideName;
import com.example.tjoin.model.StreamEvent;
import com.example.tjoin.time.ManualClock;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * JSON-over-HTTP handlers for the join service. All paths are prefixed with
 * {@code /api/v1}. Bodies are JSON; responses are always JSON envelopes:
 *
 * <pre>
 * { "ok": true,  "data": ... }
 * { "ok": false, "error": "code", "message": "..." }
 * </pre>
 */
final class JoinHandlers {

    private final ObjectMapper mapper;
    private final JobRegistry registry;
    private final java.util.concurrent.atomic.AtomicLong defaultJobSeq =
            new java.util.concurrent.atomic.AtomicLong();

    JoinHandlers(ObjectMapper mapper, JobRegistry registry) {
        this.mapper = mapper;
        this.registry = registry;
    }

    // ------------------------------------------------------------------
    // Handlers
    // ------------------------------------------------------------------

    /** POST /api/v1/jobs — create a join job. */
    HttpHandler createJob() {
        return exchange -> {
            if (!requirePost(exchange)) {
                return;
            }
            try {
                JsonNode body = readJson(exchange);
                String jobId = text(body, "jobId",
                        "job-" + defaultJobSeq.incrementAndGet());
                JoinConfig config = parseConfig(body);
                JoinJob job = registry.create(jobId, config);
                writeJson(exchange, 201, Map.of(
                        "ok", true,
                        "data", Map.of(
                                "jobId", job.id(),
                                "config", config,
                                "manualTime", true,
                                "timeMillis", job.manualClock().currentTimeMillis())));
            } catch (IllegalArgumentException e) {
                writeError(exchange, 400, "invalid_request", e.getMessage());
            } catch (IllegalStateException e) {
                writeError(exchange, 409, "job_exists", e.getMessage());
            } catch (Exception e) {
                writeError(exchange, 400, "bad_json", e.getMessage());
            }
        };
    }

    /** GET /api/v1/jobs — list jobs. */
    HttpHandler listJobs() {
        return exchange -> {
            if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) {
                writeError(exchange, 405, "method_not_allowed", "use GET");
                return;
            }
            List<Map<String, Object>> jobs = new ArrayList<>();
            for (JoinJob job : registry.all()) {
                jobs.add(summary(job));
            }
            writeJson(exchange, 200, Map.of("ok", true, "data", jobs));
        };
    }

    /** GET /api/v1/jobs/{id}/status — status, watermarks, buffer sizes, metrics. */
    HttpHandler jobStatus() {
        return exchange -> {
            JoinJob job;
            try {
                job = lookup(exchange);
            } catch (HttpStatusException e) {
                writeError(exchange, e.status, e.code, e.getMessage());
                return;
            }
            writeJson(exchange, 200, Map.of("ok", true, "data", summary(job)));
        };
    }

    /**
     * POST /api/v1/jobs/{id}/events/{side}
     * body: {"processingTime": 123, "events": [ {id,key,eventTime,value}, ... ]}
     */
    HttpHandler pushEvents() {
        return exchange -> {
            if (!requirePost(exchange)) {
                return;
            }
            JoinJob job;
            SideName side;
            try {
                job = lookup(exchange);
                side = parseSide(exchange);
            } catch (HttpStatusException e) {
                writeError(exchange, e.status, e.code, e.getMessage());
                return;
            }
            try {
                JsonNode body = readJson(exchange);

                // Optional explicit processing time; otherwise time stands
                // still (manual clock). Idleness is therefore a function
                // of the times the caller injects.
                if (body.hasNonNull("processingTime")) {
                    long pt = body.get("processingTime").asLong();
                    job.manualClock().advanceTo(Math.max(pt, job.manualClock().currentTimeMillis()));
                }

                JsonNode events = body.get("events");
                if (events == null || !events.isArray()) {
                    writeError(exchange, 400, "invalid_request", "expected an 'events' array");
                    return;
                }

                List<Map<String, Object>> itemResults = new ArrayList<>();
                List<Object> emitted = new ArrayList<>();
                int emittedCount = 0;

                for (JsonNode node : events) {
                    StreamEvent event = parseEvent(node);
                    ProcessResult result = job.operator().processEvent(side, event);
                    emittedCount += result.emitted().size();
                    emitted.addAll(result.emitted());

                    Map<String, Object> item = new LinkedHashMap<>();
                    item.put("id", event.getId());
                    item.put("status", result.status().name());
                    if (result.status() != AcceptStatus.ACCEPTED) {
                        item.put("rejected", true);
                    }
                    if (result.evicted() != null) {
                        item.put("evictedId", result.evicted().getId());
                    }
                    if (result.cleaned() > 0) {
                        item.put("cleaned", result.cleaned());
                    }
                    item.put("matchedNow", result.emitted().size());
                    itemResults.add(item);
                }

                job.operator().onProcessingTimeTick();

                Map<String, Object> data = new LinkedHashMap<>();
                data.put("jobId", job.id());
                data.put("side", side.name());
                data.put("processingTime", job.manualClock().currentTimeMillis());
                data.put("received", events.size());
                data.put("emittedCount", emittedCount);
                data.put("emitted", emitted);
                data.put("items", itemResults);
                data.put("status", summary(job));
                writeJson(exchange, 200, Map.of("ok", true, "data", data));
            } catch (IllegalArgumentException e) {
                writeError(exchange, 400, "invalid_event", e.getMessage());
            } catch (Exception e) {
                writeError(exchange, 400, "bad_json", e.getMessage());
            }
        };
    }

    /**
     * POST /api/v1/jobs/{id}/watermark/{side}
     * body: {"watermark": 1000, "processingTime": 2000}
     */
    HttpHandler pushWatermark() {
        return exchange -> {
            if (!requirePost(exchange)) {
                return;
            }
            JoinJob job;
            SideName side;
            try {
                job = lookup(exchange);
                side = parseSide(exchange);
            } catch (HttpStatusException e) {
                writeError(exchange, e.status, e.code, e.getMessage());
                return;
            }
            try {
                JsonNode body = readJson(exchange);
                if (!body.hasNonNull("watermark")) {
                    writeError(exchange, 400, "invalid_request", "missing 'watermark'");
                    return;
                }
                if (body.hasNonNull("processingTime")) {
                    long pt = body.get("processingTime").asLong();
                    job.manualClock().advanceTo(Math.max(pt, job.manualClock().currentTimeMillis()));
                }
                long wm = body.get("watermark").asLong();
                int cleaned = job.operator().advanceWatermark(side, wm);
                job.operator().onProcessingTimeTick();
                Map<String, Object> data = new LinkedHashMap<>();
                data.put("cleaned", cleaned);
                data.put("status", summary(job));
                writeJson(exchange, 200, Map.of("ok", true, "data", data));
            } catch (Exception e) {
                writeError(exchange, 400, "bad_json", e.getMessage());
            }
        };
    }

    /**
     * POST /api/v1/jobs/{id}/time
     * body: {"advanceTo": 5000} or {"advanceBy": 1000}
     * Drives processing time forward, firing scheduled idleness checks.
     */
    HttpHandler advanceTime() {
        return exchange -> {
            if (!requirePost(exchange)) {
                return;
            }
            JoinJob job;
            try {
                job = lookup(exchange);
            } catch (HttpStatusException e) {
                writeError(exchange, e.status, e.code, e.getMessage());
                return;
            }
            try {
                JsonNode body = readJson(exchange);
                ManualClock clock = job.manualClock();
                long before = clock.currentTimeMillis();
                int timersFired;
                if (body.hasNonNull("advanceTo")) {
                    timersFired = clock.advanceTo(body.get("advanceTo").asLong());
                } else if (body.hasNonNull("advanceBy")) {
                    timersFired = clock.advanceBy(body.get("advanceBy").asLong());
                } else {
                    writeError(exchange, 400, "invalid_request",
                            "provide 'advanceTo' or 'advanceBy'");
                    return;
                }
                job.operator().onProcessingTimeTick();
                Map<String, Object> data = new LinkedHashMap<>();
                data.put("before", before);
                data.put("now", clock.currentTimeMillis());
                data.put("timersFired", timersFired);
                data.put("status", summary(job));
                writeJson(exchange, 200, Map.of("ok", true, "data", data));
            } catch (IllegalArgumentException e) {
                writeError(exchange, 400, "invalid_request", e.getMessage());
            } catch (Exception e) {
                writeError(exchange, 400, "bad_json", e.getMessage());
            }
        };
    }

    /** DELETE /api/v1/jobs/{id}. */
    HttpHandler deleteJob() {
        return exchange -> {
            if (!"DELETE".equalsIgnoreCase(exchange.getRequestMethod())) {
                writeError(exchange, 405, "method_not_allowed", "use DELETE");
                return;
            }
            String id = jobIdFromPath(exchange);
            if (registry.remove(id)) {
                writeJson(exchange, 200, Map.of("ok", true, "data", Map.of("jobId", id)));
            } else {
                writeError(exchange, 404, "not_found", "no such job: " + id);
            }
        };
    }

    // ------------------------------------------------------------------
    // Parsing / rendering helpers
    // ------------------------------------------------------------------

    private JoinConfig parseConfig(JsonNode body) {
        JsonNode bounds = body.hasNonNull("bounds") ? body.get("bounds") : body;
        long lower = bounds.path("lowerBound").asLong(0L);
        long upper = bounds.path("upperBound").asLong(0L);
        JoinConfig.SideConfig left = parseSideConfig(body.get("left"));
        JoinConfig.SideConfig right = parseSideConfig(body.get("right"));
        if (left == null) {
            left = parseSideConfig(body);
        }
        if (right == null) {
            right = parseSideConfig(body);
        }
        return new JoinConfig(lower, upper, left, right);
    }

    private JoinConfig.SideConfig parseSideConfig(JsonNode node) {
        if (node == null || node.isNull()) {
            return null;
        }
        long ooo = node.path("maxOutOfOrderness").asLong(0L);
        long idle = node.path("idleTimeoutMillis").asLong(0L);
        int cap = node.path("maxBufferSize").asInt(0);
        BufferOverflowPolicy policy = parsePolicy(node.path("overflowPolicy").asText("REJECT"));
        return new JoinConfig.SideConfig(ooo, idle, cap, policy);
    }

    private BufferOverflowPolicy parsePolicy(String text) {
        return switch (text.trim().toUpperCase()) {
            case "REJECT" -> BufferOverflowPolicy.REJECT;
            case "DROP_OLDEST", "DROPOLDEST" -> BufferOverflowPolicy.DROP_OLDEST;
            default -> throw new IllegalArgumentException(
                    "unknown overflowPolicy: " + text + " (REJECT or DROP_OLDEST)");
        };
    }

    private StreamEvent parseEvent(JsonNode node) {
        if (node == null || !node.isObject()) {
            throw new IllegalArgumentException("each event must be a JSON object");
        }
        String id = node.path("id").asText(null);
        String key = node.path("key").asText(null);
        if (id == null || id.isEmpty()) {
            throw new IllegalArgumentException("event id required");
        }
        if (key == null) {
            throw new IllegalArgumentException("event key required (event " + id + ")");
        }
        if (!node.hasNonNull("eventTime")) {
            throw new IllegalArgumentException("eventTime required (event " + id + ")");
        }
        long ts = node.get("eventTime").asLong();
        Object value = node.has("value") ? mapper.convertValue(node.get("value"), Object.class) : null;
        return new StreamEvent(id, key, ts, value);
    }

    private Map<String, Object> summary(JoinJob job) {
        IntervalJoinOperator op = job.operator();
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("jobId", job.id());
        m.put("processingTime", job.manualClock().currentTimeMillis());
        m.put("leftWatermark", renderWm(op.leftWatermark()));
        m.put("rightWatermark", renderWm(op.rightWatermark()));
        m.put("leftBuffered", op.leftBufferSize());
        m.put("rightBuffered", op.rightBufferSize());

        Map<String, Object> metrics = new LinkedHashMap<>();
        var mx = op.metrics();
        metrics.put("leftEventsAccepted", mx.leftEventsAccepted.get());
        metrics.put("rightEventsAccepted", mx.rightEventsAccepted.get());
        metrics.put("duplicatesDropped", mx.duplicatesDropped.get());
        metrics.put("lateDropped", mx.lateDropped.get());
        metrics.put("bufferRejected", mx.bufferRejected.get());
        metrics.put("oldestEvicted", mx.oldestEvicted.get());
        metrics.put("leftStateCleaned", mx.leftStateCleaned.get());
        metrics.put("rightStateCleaned", mx.rightStateCleaned.get());
        metrics.put("pairsEmitted", mx.pairsEmitted.get());
        metrics.put("pairsSuppressed", mx.pairsSuppressed.get());
        metrics.put("leftIdle", mx.leftIdle);
        metrics.put("rightIdle", mx.rightIdle);
        m.put("metrics", metrics);
        return m;
    }

    private static Object renderWm(long wm) {
        return wm == Long.MIN_VALUE ? null : wm;
    }

    private JoinJob lookup(HttpExchange exchange) throws HttpStatusException {
        String id = jobIdFromPath(exchange);
        JoinJob job = registry.get(id);
        if (job == null) {
            throw new HttpStatusException(404, "not_found", "no such job: " + id);
        }
        return job;
    }

    private SideName parseSide(HttpExchange exchange) throws HttpStatusException {
        String seg = lastPathSegment(exchange);
        return switch (seg.toLowerCase()) {
            case "left" -> SideName.LEFT;
            case "right" -> SideName.RIGHT;
            default -> throw new HttpStatusException(400, "invalid_side",
                    "side must be 'left' or 'right', got: " + seg);
        };
    }

    private String jobIdFromPath(HttpExchange exchange) {
        String path = exchange.getRequestURI().getPath();
        String[] parts = path.split("/");
        // /api/v1/jobs/{id}...  -> index 4
        if (parts.length < 5) {
            throw new HttpStatusException(400, "invalid_path", "missing job id in path");
        }
        return parts[4];
    }

    private String lastPathSegment(HttpExchange exchange) {
        String path = exchange.getRequestURI().getPath();
        String[] parts = path.split("/");
        return parts[parts.length - 1];
    }

    private boolean requirePost(HttpExchange exchange) throws IOException {
        if (!"POST".equalsIgnoreCase(exchange.getRequestMethod())) {
            writeError(exchange, 405, "method_not_allowed", "use POST");
            return false;
        }
        return true;
    }

    private JsonNode readJson(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            if (bytes.length == 0) {
                return mapper.createObjectNode();
            }
            return mapper.readTree(bytes);
        }
    }

    private void writeJson(HttpExchange exchange, int status, Object body) throws IOException {
        byte[] payload = mapper.writeValueAsBytes(body);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, payload.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(payload);
        }
    }

    private void writeError(HttpExchange exchange, int status, String code, String message)
            throws IOException {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("ok", false);
        err.put("error", code);
        err.put("message", message == null ? "" : message);
        writeJson(exchange, status, err);
    }

    private static String text(JsonNode node, String field, String fallback) {
        JsonNode v = node.get(field);
        return v == null || v.isNull() ? fallback : v.asText();
    }

    /** Internal unchecked exception carrying an HTTP status. */
    private static final class HttpStatusException extends RuntimeException {
        final int status;
        final String code;

        HttpStatusException(int status, String code, String message) {
            super(message);
            this.status = status;
            this.code = code;
        }
    }
}
