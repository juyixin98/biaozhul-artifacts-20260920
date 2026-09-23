package com.example.sessionwindow.service;

import com.example.sessionwindow.json.Json;
import com.example.sessionwindow.json.JsonException;
import com.example.sessionwindow.model.Aggregate;
import com.example.sessionwindow.model.ResultRecord;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * Lightweight JSON HTTP service backed by the JDK's built-in
 * {@link HttpServer}. No external dependencies, no message system.
 *
 * <pre>
 * GET    /health
 * POST   /session-windows/run                       stateless batch + offline check
 * GET    /session-windows/pipelines
 * POST   /session-windows/pipelines/{id}            create (config in body)
 * GET    /session-windows/pipelines/{id}            state snapshot
 * POST   /session-windows/pipelines/{id}/events     ingest items, drain records
 * DELETE /session-windows/pipelines/{id}
 * </pre>
 */
public final class HttpServerRunner {

    private final HttpServer server;
    private final SessionWindowService service;
    private final int port;

    public HttpServerRunner(int port, SessionWindowService service) throws IOException {
        this.service = service;
        this.server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        this.port = server.getAddress().getPort();
        server.setExecutor(Executors.newFixedThreadPool(4));
        register();
    }

    public int port() {
        return port;
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
        service.shutdown();
    }

    private void register() {
        server.createContext("/health", this::handleHealth);
        server.createContext("/session-windows/run", this::handleRun);
        server.createContext("/session-windows/pipelines", this::handlePipelines);
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "UP");
        writeJson(ex, 200, body);
    }

    private void handleRun(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            writeError(ex, 405, "use POST");
            return;
        }
        try {
            Map<String, Object> req = readJson(ex);
            long gap = Json.lng(req, "gap", 10L);
            long allowedLateness = Json.lng(req, "allowedLateness", 0L);
            boolean flushAtEnd = Json.bool(req, "flushAtEnd", true);
            List<BatchProcessor.Item> items = Requests.parseItems(req);

            BatchProcessor.Outcome outcome = BatchProcessor.run(gap, allowedLateness, items, flushAtEnd);
            writeJson(ex, 200, outcomeJson(outcome, flushAtEnd));
        } catch (IllegalArgumentException | JsonException e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    private void handlePipelines(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            // /session-windows/pipelines[/id[/events]]
            String rest = path.substring("/session-windows/pipelines".length());
            String[] parts = rest.isEmpty() ? new String[0] : rest.substring(1).split("/");
            String method = ex.getRequestMethod();

            if (parts.length == 0) {
                if (!"GET".equals(method)) {
                    writeError(ex, 405, "use GET");
                    return;
                }
                Map<String, Object> b = new LinkedHashMap<>();
                b.put("pipelineIds", service.pipelineIds());
                writeJson(ex, 200, b);
                return;
            }

            String id = parts[0];
            if (parts.length == 1) {
                switch (method) {
                    case "POST" -> {
                        ServiceConfig config = Requests.parseConfig(readJson(ex));
                        service.createPipeline(id, config);
                        Map<String, Object> b = new LinkedHashMap<>();
                        b.put("pipelineId", id);
                        b.put("config", SessionWindowService.configJson(config));
                        writeJson(ex, 201, b);
                    }
                    case "GET" -> writeJson(ex, 200, service.status(id));
                    case "DELETE" -> {
                        service.deletePipeline(id);
                        writeJson(ex, 200, Map.of("deleted", id));
                    }
                    default -> writeError(ex, 405, "unsupported method");
                }
                return;
            }

            if (parts.length == 2 && "events".equals(parts[1])) {
                if (!"POST".equals(method)) {
                    writeError(ex, 405, "use POST");
                    return;
                }
                List<BatchProcessor.Item> items = Requests.parseItems(readJson(ex));
                List<ResultRecord> records = service.ingest(id, items);
                Map<String, Object> b = new LinkedHashMap<>();
                b.put("pipelineId", id);
                b.put("status", service.status(id));
                b.put("records", records.stream().map(ResultRecord::toJson).toList());
                writeJson(ex, 200, b);
                return;
            }
            writeError(ex, 404, "unknown path: " + path);
        } catch (IllegalArgumentException | JsonException e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    // ------------------------------------------------------------------
    // JSON helpers
    // ------------------------------------------------------------------

    static Map<String, Object> outcomeJson(BatchProcessor.Outcome out, boolean flushAtEnd) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("finalWatermark", out.finalWatermark);
        b.put("records", out.records.stream().map(ResultRecord::toJson).toList());

        Map<String, Object> results = new LinkedHashMap<>();
        for (Map.Entry<String, List<Aggregate>> e : out.resultsAsLists().entrySet()) {
            results.put(e.getKey(), e.getValue().stream().map(Aggregate::toJson).toList());
        }
        b.put("results", results);

        Map<String, Object> reference = new LinkedHashMap<>();
        for (Map.Entry<String, List<Aggregate>> e : out.reference.entrySet()) {
            reference.put(e.getKey(), e.getValue().stream().map(Aggregate::toJson).toList());
        }
        b.put("offlineReference", reference);
        b.put("matchesOfflineReference", out.matchesReference);

        List<Map<String, Object>> dropped = new ArrayList<>();
        for (var e : out.droppedEvents) {
            dropped.add(e.toJson());
        }
        b.put("droppedEvents", dropped);

        if (flushAtEnd) {
            boolean cleaned = out.remainingKeys == 0 && out.remainingSessions == 0;
            Map<String, Object> state = new LinkedHashMap<>();
            state.put("retainedKeys", out.remainingKeys);
            state.put("retainedSessions", out.remainingSessions);
            state.put("allStateCleaned", cleaned);
            b.put("stateAfterFlush", state);
        }
        return b;
    }

    private Map<String, Object> readJson(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            String text = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            if (text.isBlank()) {
                return Map.of();
            }
            return Json.parseObject(text);
        }
    }

    private void writeJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] bytes = Json.pretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    private void writeError(HttpExchange ex, int status, String message) throws IOException {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("error", message);
        writeJson(ex, status, b);
    }
}
