package com.example.watermark.service;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicLong;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import com.example.watermark.json.Json;

/**
 * Tiny JSON-over-HTTP wrapper around {@link WatermarkSession}, using only the
 * JDK's built-in {@link HttpServer}. All simulation time is virtual; wall-clock
 * time is never used for watermark decisions.
 *
 * <h2>API</h2>
 * <ul>
 *   <li>{@code GET  /health} — liveness probe</li>
 *   <li>{@code POST /api/sessions} — body {@code {"config": {...}}} creates a session,
 *       returns {@code sessionId}</li>
 *   <li>{@code POST /api/sessions/{id}/script} — run a {@code {"steps":[...]}} script,
 *       returns state/late events/windows/per-step observations</li>
 *   <li>{@code POST /api/run} — stateless convenience: config + steps in one body</li>
 *   <li>{@code GET  /api/sessions/{id}} — current state snapshot</li>
 *   <li>{@code DELETE /api/sessions/{id}} — close and remove a session</li>
 * </ul>
 */
public final class WatermarkHttpServer implements AutoCloseable {

    private final HttpServer server;
    private final ConcurrentHashMap<String, WatermarkSession> sessions = new ConcurrentHashMap<>();
    private final AtomicLong sequence = new AtomicLong();

    public WatermarkHttpServer(int port) throws IOException {
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/api/run", this::handleRun);
        server.createContext("/api/sessions", this::handleSessions);
        // Cached pool: HttpServer dispatches one exchange per task, and a
        // connection held open by a client can otherwise exhaust a fixed pool.
        server.setExecutor(Executors.newCachedThreadPool(r -> {
            Thread t = new Thread(r, "http-worker");
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

    @Override
    public void close() {
        sessions.values().forEach(WatermarkSession::close);
        sessions.clear();
        server.stop(0);
    }

    // ------------------------------------------------------------- handlers

    private void handleHealth(HttpExchange ex) throws IOException {
        writeJson(ex, 200, Map.of("status", "UP"));
    }

    private void handleRun(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            writeError(ex, 405, "use POST");
            return;
        }
        try {
            Map<String, Object> body = readBody(ex);
            Map<String, Object> result = WatermarkSession.runStandalone(body);
            writeJson(ex, 200, result);
        } catch (Exception e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    private void handleSessions(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            // /api/sessions[/id][/script]
            String rest = path.substring("/api/sessions".length());
            if (rest.isEmpty() || rest.equals("/")) {
                if ("POST".equals(method)) {
                    Map<String, Object> body = readBody(ex);
                    @SuppressWarnings("unchecked")
                    Map<String, Object> config =
                            (Map<String, Object>) body.getOrDefault("config", Map.of());
                    String id = "session-" + sequence.incrementAndGet();
                    WatermarkSession session = new WatermarkSession(id, config);
                    sessions.put(id, session);
                    writeJson(ex, 201, Map.of("sessionId", id, "snapshot",
                            session.manager().snapshot().toMap()));
                } else {
                    writeError(ex, 405, "use POST");
                }
                return;
            }

            String[] parts = rest.substring(1).split("/");
            String id = parts[0];
            WatermarkSession session = sessions.get(id);
            if (session == null) {
                writeError(ex, 404, "unknown session: " + id);
                return;
            }

            if (parts.length == 1) {
                if ("GET".equals(method)) {
                    writeJson(ex, 200, Map.of("sessionId", id, "snapshot",
                            session.manager().snapshot().toMap()));
                } else if ("DELETE".equals(method)) {
                    sessions.remove(id);
                    session.close();
                    writeJson(ex, 200, Map.of("closed", id));
                } else {
                    writeError(ex, 405, "use GET or DELETE");
                }
            } else if (parts.length == 2 && "script".equals(parts[1])) {
                if (!"POST".equals(method)) {
                    writeError(ex, 405, "use POST");
                    return;
                }
                Map<String, Object> body = readBody(ex);
                Map<String, Object> result = session.executeScript(body);
                writeJson(ex, 200, result);
            } else {
                writeError(ex, 404, "not found: " + path);
            }
        } catch (Exception e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    // ------------------------------------------------------------------ io

    private static Map<String, Object> readBody(HttpExchange ex) throws IOException {
        String text = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        if (text.isBlank()) {
            return Map.of();
        }
        Object parsed = Json.parse(text);
        if (!(parsed instanceof Map)) {
            throw new IllegalArgumentException("request body must be a JSON object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> map = (Map<String, Object>) parsed;
        return map;
    }

    private static void writeJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] data = Json.writePretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(data);
        }
    }

    private static void writeError(HttpExchange ex, int status, String message) throws IOException {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("error", message == null ? "request failed" : message);
        err.put("status", status);
        writeJson(ex, status, err);
    }
}
