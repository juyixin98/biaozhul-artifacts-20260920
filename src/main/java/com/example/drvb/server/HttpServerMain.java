package com.example.drvb.server;

import com.example.drvb.core.RuleErrorCode;
import com.example.drvb.core.RuleRegistryException;
import com.example.drvb.core.RuleVersion;
import com.example.drvb.stream.IngestResult;
import com.example.drvb.stream.RuleBindingEngine;
import com.example.drvb.time.ManualScheduler;
import com.example.drvb.time.Scheduler;
import com.example.drvb.time.SimClock;
import com.example.drvb.time.SystemClock;
import com.example.drvb.time.TimeSource;
import com.example.drvb.time.WallScheduler;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * JSON/HTTP service around {@link RuleBindingEngine}.
 *
 * <p>No frameworks: the JDK's built-in {@link HttpServer} handles routing and
 * Jackson handles JSON. Endpoints:
 * <pre>
 * POST /admin/bootstrap        install the first immutable rule version
 * POST /admin/rules/versions   append a hot-updated version (watermark-gated)
 * POST /admin/rules/rollback   append a copy of an earlier version
 * GET  /admin/rules/versions   list versions (oldest first)
 * POST /events                 ingest one event
 * POST /events/batch           ingest events in request order
 * GET  /results                accepted matches (?from=&to= by event time)
 * GET  /results/rejected       rejected events with reasons
 * POST /admin/watermark        force the watermark
 * POST /admin/maintenance      purge results + garbage-collect old versions
 * GET  /admin/gc-preview       show GC eligibility without mutating state
 * GET  /admin/stats            counters and configuration
 * POST /admin/tick             fire the maintenance scheduler once (manual mode)
 * POST /admin/clock            set/advance the simulated clock (sim mode)
 * GET  /health                 liveness
 * </pre>
 */
public final class HttpServerMain {

    private final RuleBindingEngine engine;
    private final TimeSource clock;
    private final Scheduler scheduler;
    private final boolean simMode;
    private HttpServer server;

    HttpServerMain(RuleBindingEngine engine, TimeSource clock, Scheduler scheduler,
                   boolean simMode) {
        this.engine = engine;
        this.clock = clock;
        this.scheduler = scheduler;
        this.simMode = simMode;
    }

    void start(int port, long maintenancePeriodMillis) throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", this::route);
        server.setExecutor(Executors.newFixedThreadPool(8));
        server.start();
        scheduler.start(this::scheduledMaintenance, maintenancePeriodMillis);
    }

    int port() {
        return server.getAddress().getPort();
    }

    void stop() {
        scheduler.stop();
        server.stop(0);
    }

    private void scheduledMaintenance() {
        try {
            engine.runMaintenance();
        } catch (RuntimeException e) {
            // A scheduled task must never die silently; print and continue.
            System.err.println("[maintenance] " + e);
        }
    }

    // ------------------------------------------------------------- routing

    private void route(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (path + "#" + method) {
                case "/health#GET" -> handleHealth(ex);
                case "/admin/bootstrap#POST" -> handleBootstrap(ex);
                case "/admin/rules/versions#POST" -> handlePublish(ex);
                case "/admin/rules/versions#GET" -> handleListVersions(ex);
                case "/admin/rules/rollback#POST" -> handleRollback(ex);
                case "/events#POST" -> handleEvent(ex, false);
                case "/events/batch#POST" -> handleEvent(ex, true);
                case "/results#GET" -> handleResults(ex);
                case "/results/rejected#GET" -> handleRejected(ex);
                case "/admin/watermark#POST" -> handleWatermark(ex);
                case "/admin/maintenance#POST" -> handleMaintenance(ex);
                case "/admin/gc-preview#GET" -> handleGcPreview(ex);
                case "/admin/stats#GET" -> handleStats(ex);
                case "/admin/tick#POST" -> handleTick(ex);
                case "/admin/clock#POST" -> handleClock(ex);
                default -> sendError(ex, 404, "NOT_FOUND",
                        "no route for " + method + " " + path);
            }
        } catch (BadRequestException e) {
            sendError(ex, e.status(), "BAD_REQUEST", e.getMessage());
        } catch (RuleRegistryException e) {
            sendError(ex, httpStatusFor(e.code()), e.code().name(), e.getMessage());
        } catch (JsonProcessingException e) {
            sendError(ex, 400, "MALFORMED_JSON", "malformed JSON body: "
                    + e.getOriginalMessage());
        } catch (RuntimeException e) {
            sendError(ex, 500, "INTERNAL_ERROR", e.toString());
        }
    }

    private static int httpStatusFor(RuleErrorCode code) {
        return switch (code) {
            case DUPLICATE_VERSION, ALREADY_BOOTSTRAPPED -> 409;
            case NOT_BOOTSTRAPPED, SOURCE_VERSION_NOT_FOUND -> 404;
            case EFFECTIVE_TIME_IN_PAST -> 422;
        };
    }

    // ------------------------------------------------------------- handlers

    private void handleHealth(HttpExchange ex) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("status", "UP");
        m.put("simMode", simMode);
        m.put("processingTime", clock.nowMillis());
        sendJson(ex, 200, m);
    }

    private void handleBootstrap(HttpExchange ex) throws IOException {
        JsonNode body = readJson(ex);
        RuleVersion v = JsonCodecs.toVersion(body, clock.nowMillis(), true);
        RuleVersion installed = engine.bootstrap(v);
        sendJson(ex, 201, Map.of("version", installed.toMap()));
    }

    private void handlePublish(HttpExchange ex) throws IOException {
        JsonNode body = readJson(ex);
        RuleVersion v = JsonCodecs.toVersion(body, clock.nowMillis(), false);
        RuleVersion installed = engine.publish(v);
        sendJson(ex, 201, Map.of("version", installed.toMap()));
    }

    private void handleRollback(HttpExchange ex) throws IOException {
        JsonNode body = readJson(ex);
        String source = JsonCodecs.requireText(body, "sourceVersionId");
        String newId = JsonCodecs.requireText(body, "newVersionId");
        long effectiveFrom = JsonCodecs.parseTimeValue(
                body.get("effectiveFrom"), "effectiveFrom");
        String description = body.path("description").asText(null);
        RuleVersion installed = engine.rollback(source, newId, effectiveFrom,
                description);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("version", installed.toMap());
        resp.put("restoredFrom", source);
        resp.put("checksumsMatch", installed.checksum().equals(
                engine.registry().get(source).checksum()));
        sendJson(ex, 201, resp);
    }

    private void handleListVersions(HttpExchange ex) throws IOException {
        List<Object> versions = new ArrayList<>();
        for (RuleVersion v : engine.registry().versions()) {
            versions.add(v.toMap());
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("versions", versions);
        resp.put("watermark", engine.registry().watermark());
        sendJson(ex, 200, resp);
    }

    private void handleEvent(HttpExchange ex, boolean batch) throws IOException {
        JsonNode body = readJson(ex);
        List<IngestResult> results = new ArrayList<>();
        if (batch) {
            JsonNode arr = body.get("events");
            if (arr == null || !arr.isArray()) {
                throw new BadRequestException("batch body requires an 'events' array");
            }
            for (JsonNode en : arr) {
                results.add(engine.ingest(JsonCodecs.toEvent(en)));
            }
        } else {
            results.add(engine.ingest(JsonCodecs.toEvent(body)));
        }
        List<Object> items = new ArrayList<>();
        int accepted = 0;
        int rejected = 0;
        for (IngestResult r : results) {
            if (r.accepted()) {
                accepted++;
                items.add(viewAccepted(r));
            } else {
                rejected++;
                items.add(viewRejected(r));
            }
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("acceptedCount", accepted);
        resp.put("rejectedCount", rejected);
        resp.put("results", items);
        sendJson(ex, 200, resp);
    }

    private void handleResults(HttpExchange ex) throws IOException {
        long[] range = timeRange(ex.getRequestURI());
        List<IngestResult> rows = engine.store().query(range[0], range[1]);
        List<Object> items = new ArrayList<>();
        for (IngestResult r : rows) {
            items.add(viewAccepted(r));
        }
        sendJson(ex, 200, Map.of("results", items, "count", items.size()));
    }

    private void handleRejected(HttpExchange ex) throws IOException {
        List<Object> items = new ArrayList<>();
        for (IngestResult r : engine.store().allRejected()) {
            items.add(viewRejected(r));
        }
        sendJson(ex, 200, Map.of("results", items, "count", items.size()));
    }

    private void handleWatermark(HttpExchange ex) throws IOException {
        JsonNode body = readJson(ex);
        long wm = JsonCodecs.parseTimeValue(body.get("watermark"), "watermark");
        long now = engine.advanceWatermark(wm);
        sendJson(ex, 200, Map.of("watermark", now));
    }

    private void handleMaintenance(HttpExchange ex) throws IOException {
        readJson(ex); // body optional; parse to fail fast on malformed input
        sendJson(ex, 200, gcReportView(engine.runMaintenance()));
    }

    private void handleGcPreview(HttpExchange ex) throws IOException {
        RuleBindingEngine.GcPreview p = engine.gcPreview();
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("watermark", p.watermark());
        resp.put("latenessHorizon", p.horizon());
        resp.put("referencedVersionIds", p.referencedVersionIds());
        resp.put("eligibleForReclaim", versionIds(p.eligibleForReclaim()));
        resp.put("retainedVersions", versionIds(p.retainedVersions()));
        sendJson(ex, 200, resp);
    }

    private void handleStats(HttpExchange ex) throws IOException {
        sendJson(ex, 200, engine.stats());
    }

    private void handleTick(HttpExchange ex) throws IOException {
        readJson(ex);
        if (!(scheduler instanceof ManualScheduler manual)) {
            throw new BadRequestException(409,
                    "tick is only available with --scheduler=manual");
        }
        int before = manual.fireCount();
        manual.tick();
        sendJson(ex, 200, Map.of("ticks", before + 1));
    }

    private void handleClock(HttpExchange ex) throws IOException {
        JsonNode body = readJson(ex);
        if (!(clock instanceof SimClock sim)) {
            throw new BadRequestException(409,
                    "clock control is only available with --clock=sim");
        }
        if (body.hasNonNull("set")) {
            sim.set(JsonCodecs.parseTimeValue(body.get("set"), "set"));
        }
        if (body.hasNonNull("advanceMillis")) {
            sim.advance(body.get("advanceMillis").asLong());
        }
        sendJson(ex, 200, Map.of("processingTime", sim.nowMillis(),
                "isoTime", Instant.ofEpochMilli(sim.nowMillis()).toString()));
    }

    // ------------------------------------------------------------- views

    private static Map<String, Object> viewAccepted(IngestResult r) {
        Map<String, Object> m = JsonCodecs.matchView(r);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("status", "ACCEPTED");
        out.putAll(m);
        return out;
    }

    private static Map<String, Object> viewRejected(IngestResult r) {
        Map<String, Object> m = JsonCodecs.rejectionView(r);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("status", "REJECTED");
        out.putAll(m);
        return out;
    }

    private static List<Object> versionIds(List<RuleVersion> vs) {
        List<Object> ids = new ArrayList<>();
        for (RuleVersion v : vs) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("versionId", v.versionId());
            m.put("effectiveFrom", v.effectiveFrom());
            m.put("checksum", v.checksum());
            ids.add(m);
        }
        return ids;
    }

    private static Map<String, Object> gcReportView(RuleBindingEngine.GcReport r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("atMillis", r.atMillis());
        m.put("watermark", r.watermark());
        m.put("resultsPurged", r.resultsPurged());
        m.put("versionsReclaimed", r.versionsReclaimed());
        m.put("removed", versionIds(r.removed()));
        m.put("retainedVersions", versionIds(r.retainedVersions()));
        return m;
    }

    private static long[] timeRange(URI uri) {
        long from = Long.MIN_VALUE;
        long to = Long.MAX_VALUE;
        String query = uri.getRawQuery();
        if (query != null) {
            for (String pair : query.split("&")) {
                String[] kv = pair.split("=", 2);
                if (kv.length != 2) {
                    continue;
                }
                String key = kv[0];
                String val = java.net.URLDecoder.decode(kv[1], StandardCharsets.UTF_8);
                long parsed;
                try {
                    parsed = val.matches("\\d+|[-+]?\\d+")
                            ? Long.parseLong(val)
                            : Instant.parse(val).toEpochMilli();
                } catch (Exception e) {
                    throw new BadRequestException("bad query value for " + key + ": " + val);
                }
                if (key.equals("from")) {
                    from = parsed;
                } else if (key.equals("to")) {
                    to = parsed;
                }
            }
        }
        return new long[] {from, to};
    }

    // -------------------------------------------------------------- io

    private JsonNode readJson(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        if (bytes.length == 0) {
            return JsonCodecs.MAPPER.createObjectNode();
        }
        return JsonCodecs.MAPPER.readTree(bytes);
    }

    private void sendJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = JsonCodecs.MAPPER.writeValueAsBytes(body);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(payload);
        }
    }

    private void sendError(HttpExchange ex, int status, String code, String message)
            throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("error", code);
        m.put("message", message);
        sendJson(ex, status, m);
    }

    // ---------------------------------------------------------------- main

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(getArg(args, "port", "8080"));
        String clockMode = getArg(args, "clock", "system");
        String schedulerMode = getArg(args, "scheduler", "wall");
        long wmDelay = Long.parseLong(getArg(args, "watermark-delay", "5000"));
        long allowedLateness = Long.parseLong(getArg(args, "allowed-lateness", "60000"));
        long retention = Long.parseLong(getArg(args, "result-retention", "3600000"));
        long maintenancePeriod = Long.parseLong(
                getArg(args, "maintenance-period", "1000"));
        long simStart = Long.parseLong(getArg(args, "sim-start", "0"));

        TimeSource clock = "sim".equals(clockMode)
                ? new SimClock(simStart)
                : new SystemClock();
        Scheduler scheduler = "manual".equals(schedulerMode)
                ? new ManualScheduler()
                : new WallScheduler();

        var engine = new RuleBindingEngine(
                new com.example.drvb.core.RuleRegistry(),
                new com.example.drvb.stream.WatermarkTracker(wmDelay),
                new com.example.drvb.stream.InMemoryResultStore(),
                clock, allowedLateness, retention);

        var app = new HttpServerMain(engine, clock, scheduler,
                "sim".equals(clockMode) || "manual".equals(schedulerMode));
        app.start(port, maintenancePeriod);
        System.out.printf("""

                        dynamic-rule-binding listening on http://localhost:%d
                          clock=%s scheduler=%s watermarkDelay=%dms \
                        allowedLateness=%dms resultRetention=%dms%s
                        %n""",
                app.port(), clockMode, schedulerMode, wmDelay,
                allowedLateness, retention,
                simStart != 0 ? " simStart=" + simStart : "");
    }

    private static String getArg(String[] args, String name, String dflt) {
        String prefix = "--" + name + "=";
        for (String a : args) {
            if (a.startsWith(prefix)) {
                return a.substring(prefix.length());
            }
        }
        return dflt;
    }
}
