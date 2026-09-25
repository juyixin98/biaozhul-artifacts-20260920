package streamagg.server;

import streamagg.json.Json;
import streamagg.model.IngestResult;
import streamagg.time.Clock;
import streamagg.time.SystemClock;
import streamagg.time.SystemScheduler;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicReference;

/**
 * JSON-over-HTTP service for the streaming correction engine.
 *
 * <table>
 *   <caption>Endpoints</caption>
 *   <tr><td>POST /v1/events/ingest</td><td>ingest one operation</td></tr>
 *   <tr><td>POST /v1/events/batch</td><td>ingest an array of operations in order</td></tr>
 *   <tr><td>GET  /v1/aggregates</td><td>per-key sums/counts (?verify=1 adds ledger check)</td></tr>
 *   <tr><td>GET  /v1/events</td><td>live events and buffered pending operations</td></tr>
 *   <tr><td>GET  /v1/ledger</td><td>resolved event ledger (?limit=N)</td></tr>
 *   <tr><td>POST /v1/replay</td><td>reference replay: raw ops or current ledger</td></tr>
 *   <tr><td>POST /v1/emit</td><td>produce one output snapshot</td></tr>
 *   <tr><td>POST /v1/admin/reset</td><td>clear all state</td></tr>
 *   <tr><td>GET  /health</td><td>liveness</td></tr>
 * </table>
 */
public final class AggregateHttpServer implements AutoCloseable {

    private final HttpServer server;
    private final AggregateService service;
    private final Clock clock;
    private final SystemScheduler scheduler;
    private final AtomicReference<Duration> periodicPeriod = new AtomicReference<>();

    public AggregateHttpServer(int port) throws IOException {
        this(port, SystemClock.INSTANCE);
    }

    public AggregateHttpServer(int port, Clock clock) throws IOException {
        this.clock = clock;
        this.service = AggregateService.create(clock);
        this.scheduler = new SystemScheduler();
        this.server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);

        server.createContext("/health", this::handleHealth);
        server.createContext("/v1/events/ingest", this::handleIngest);
        server.createContext("/v1/events/batch", this::handleBatch);
        server.createContext("/v1/events", this::handleEvents);
        server.createContext("/v1/aggregates", this::handleAggregates);
        server.createContext("/v1/ledger", this::handleLedger);
        server.createContext("/v1/replay", this::handleReplay);
        server.createContext("/v1/emit", this::handleEmit);
        server.createContext("/v1/admin/reset", this::handleReset);

        server.setExecutor(Executors.newFixedThreadPool(4, r -> {
            Thread t = new Thread(r, "streamagg-http");
            t.setDaemon(true);
            return t;
        }));
    }

    public void start() {
        server.start();
    }

    public int getPort() {
        return server.getAddress().getPort();
    }

    public AggregateService service() {
        return service;
    }

    @Override
    public void close() {
        service.stopPeriodicEmission();
        scheduler.close();
        server.stop(1);
    }

    // ------------------------------------------------------------------
    // Handlers
    // ------------------------------------------------------------------

    private void handleHealth(HttpExchange ex) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("status", "UP");
        writeJson(ex, 200, m);
    }

    private void handleIngest(HttpExchange ex) throws IOException {
        if (!requirePost(ex)) {
            return;
        }
        Json.JsonValue body;
        try {
            body = Json.parse(readBody(ex));
        } catch (RuntimeException e) {
            writeError(ex, 400, e.getMessage());
            return;
        }
        try {
            IngestResult r = service.ingestOne(body);
            int code = r.status().name().equals("INVALID") || r.status().name().equals("LATE") ? 400 : 200;
            writeJson(ex, code, Codec.resultJson(r));
        } catch (IllegalArgumentException e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    private void handleBatch(HttpExchange ex) throws IOException {
        if (!requirePost(ex)) {
            return;
        }
        Json.JsonValue body;
        try {
            body = Json.parse(readBody(ex));
        } catch (RuntimeException e) {
            writeError(ex, 400, e.getMessage());
            return;
        }
        try {
            List<AggregateService.BatchItemResult> results = service.ingestBatch(body);
            Map<String, Object> out = new LinkedHashMap<>();
            List<Object> rows = new java.util.ArrayList<>();
            int applied = 0;
            int buffered = 0;
            int duplicate = 0;
            int conflict = 0;
            for (AggregateService.BatchItemResult r : results) {
                rows.add(Codec.resultJson(r.result()));
                switch (r.result().status()) {
                    case APPLIED -> applied++;
                    case BUFFERED -> buffered++;
                    case DUPLICATE -> duplicate++;
                    case CONFLICT -> conflict++;
                    default -> { }
                }
            }
            out.put("count", results.size());
            out.put("applied", applied);
            out.put("buffered", buffered);
            out.put("duplicate", duplicate);
            out.put("conflict", conflict);
            out.put("results", rows);
            out.put("aggregates", service.aggregateViews());
            writeJson(ex, 200, out);
        } catch (IllegalArgumentException e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    private void handleAggregates(HttpExchange ex) throws IOException {
        boolean verify = "1".equals(queryParam(ex, "verify")) || "true".equals(queryParam(ex, "verify"));
        writeJson(ex, 200, service.aggregateResponse(verify));
    }

    private void handleEvents(HttpExchange ex) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("liveEvents", service.eventViews());
        m.put("pending", service.pendingViews());
        writeJson(ex, 200, m);
    }

    private void handleLedger(HttpExchange ex) throws IOException {
        Integer limit = null;
        String lim = queryParam(ex, "limit");
        if (lim != null) {
            try {
                limit = Integer.parseInt(lim);
            } catch (NumberFormatException ignored) {
                // leave unlimited
            }
        }
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("entries", service.ledgerViews(limit));
        m.put("count", service.engine().ledger().size());
        writeJson(ex, 200, m);
    }

    private void handleReplay(HttpExchange ex) throws IOException {
        if (!requirePost(ex)) {
            return;
        }
        Json.JsonValue body = null;
        String raw = readBody(ex);
        if (!raw.isBlank()) {
            try {
                body = Json.parse(raw);
            } catch (RuntimeException e) {
                writeError(ex, 400, e.getMessage());
                return;
            }
        }
        writeJson(ex, 200, service.replayCheck(body, clock));
    }

    private void handleEmit(HttpExchange ex) throws IOException {
        if (!requirePost(ex)) {
            return;
        }
        writeJson(ex, 200, Codec.snapshotJson(service.engine().emitSnapshot()));
    }

    private void handleReset(HttpExchange ex) throws IOException {
        if (!requirePost(ex)) {
            return;
        }
        service.reset();
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("status", "RESET");
        writeJson(ex, 200, m);
    }

    // ------------------------------------------------------------------
    // HTTP plumbing
    // ------------------------------------------------------------------

    private boolean requirePost(HttpExchange ex) throws IOException {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            writeError(ex, 405, "method not allowed; use POST");
            return false;
        }
        return true;
    }

    private String readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private String queryParam(HttpExchange ex, String name) {
        String q = ex.getRequestURI().getQuery();
        if (q == null) {
            return null;
        }
        for (String pair : q.split("&")) {
            int eq = pair.indexOf('=');
            if (eq > 0 && pair.substring(0, eq).equals(name)) {
                return java.net.URLDecoder.decode(pair.substring(eq + 1), StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    private void writeError(HttpExchange ex, int code, String message) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("error", true);
        m.put("status", code);
        m.put("message", message);
        writeJson(ex, code, m);
    }

    @SuppressWarnings("unchecked")
    private void writeJson(HttpExchange ex, int code, Object model) throws IOException {
        String json;
        if (model instanceof String s) {
            json = s;
        } else if (model instanceof Json.JsonValue jv) {
            json = Json.pretty(jv);
        } else if (model instanceof Map<?, ?> map) {
            json = Json.pretty(Json.wrap((Map<String, Object>) map));
        } else if (model instanceof List<?> list) {
            json = Json.pretty(Json.wrap((List<Object>) list));
        } else {
            json = Json.pretty(Json.wrap(model));
        }
        byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }
}
