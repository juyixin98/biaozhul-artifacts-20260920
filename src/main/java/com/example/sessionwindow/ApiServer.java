package com.example.sessionwindow;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * HTTP API backed by the JDK's built-in HttpServer (no third-party deps).
 *
 *   POST /events             submit one event
 *   POST /events/batch       submit several events (applied in order)
 *   POST /watermark          manually advance the watermark
 *   GET  /sessions?key=...   current materialized sessions
 *   GET  /output?afterSeq=N  changelog (UPSERT/RETRACT), optionally incremental
 *   GET  /watermark          current watermark
 *   GET  /stats              counters + config
 *   POST /reset              clear all state and the on-disk log
 *   GET  /health             liveness probe
 */
public final class ApiServer {

    private final HttpServer server;
    private final SessionEngine engine;

    public ApiServer(int port, SessionEngine engine) throws IOException {
        this.engine = engine;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        // daemon threads so worker pools do not keep the JVM alive on shutdown
        this.server.setExecutor(Executors.newFixedThreadPool(8, r -> {
            Thread t = new Thread(r, "httpserver-worker");
            t.setDaemon(true);
            return t;
        }));

        server.createContext("/health", ex -> handle(ex, this::health));
        server.createContext("/events/batch", ex -> handle(ex, this::eventsBatch));
        server.createContext("/events", ex -> handle(ex, this::events));
        server.createContext("/watermark", ex -> handle(ex, this::watermark));
        server.createContext("/sessions", ex -> handle(ex, this::sessions));
        server.createContext("/output", ex -> handle(ex, this::output));
        server.createContext("/stats", ex -> handle(ex, this::stats));
        server.createContext("/reset", ex -> handle(ex, this::reset));
    }

    public void start() {
        server.start();
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public void stop() {
        server.stop(0);
    }

    // ---------------- handlers ----------------

    private void health(HttpExchange ex, Map<String, Object> resp) {
        resp.put("status", "ok");
    }

    private void events(HttpExchange ex, Map<String, Object> resp) throws IOException {
        requireMethod(ex, "POST");
        Map<String, Object> body = readJsonObject(ex);
        Event event = Event.fromMap(body);
        SessionEngine.Result r = engine.submit(event);
        fillResult(resp, r);
    }

    private void eventsBatch(HttpExchange ex, Map<String, Object> resp) throws IOException {
        requireMethod(ex, "POST");
        Map<String, Object> body = readJsonObject(ex);
        Object raw = body.get("events");
        if (!(raw instanceof List<?> list)) {
            throw new ApiException(400, "Field 'events' must be an array");
        }
        var results = new ArrayList<Map<String, Object>>();
        for (Object item : list) {
            if (!(item instanceof Map<?, ?> m)) {
                throw new ApiException(400, "Each entry of 'events' must be an object");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> mm = (Map<String, Object>) m;
            Event e = Event.fromMap(mm);
            SessionEngine.Result r = engine.submit(e);
            var entry = new LinkedHashMap<String, Object>();
            fillResult(entry, r);
            entry.put("key", e.key());
            entry.put("ts", e.ts());
            results.add(entry);
        }
        resp.put("results", results);
        resp.put("watermark", engine.watermark());
    }

    private void watermark(HttpExchange ex, Map<String, Object> resp) throws IOException {
        if ("GET".equals(ex.getRequestMethod())) {
            resp.put("watermark", engine.watermark());
            return;
        }
        requireMethod(ex, "POST");
        Map<String, Object> body = readJsonObject(ex);
        Long wm = Json.numOrNull(body, "watermark");
        if (wm == null) {
            wm = Json.numOrNull(body, "wm");
        }
        if (wm == null) {
            throw new ApiException(400, "Field 'watermark' is required");
        }
        long rev = engine.advanceWatermark(wm);
        resp.put("watermark", engine.watermark());
        resp.put("revision", rev);
    }

    private void sessions(HttpExchange ex, Map<String, Object> resp) {
        requireMethod(ex, "GET");
        String key = queryParam(ex, "key");
        Map<String, List<Map<String, Object>>> all = engine.sessions();
        if (key != null) {
            resp.put("key", key);
            resp.put("sessions", all.getOrDefault(key, List.of()));
        } else {
            resp.put("sessions", all);
        }
    }

    private void output(HttpExchange ex, Map<String, Object> resp) {
        requireMethod(ex, "GET");
        String after = queryParam(ex, "afterSeq");
        List<Change> changes;
        if (after == null) {
            changes = engine.changelog();
        } else {
            long afterSeq;
            try {
                afterSeq = Long.parseLong(after);
            } catch (NumberFormatException e) {
                throw new ApiException(400, "afterSeq must be an integer");
            }
            changes = engine.changesSince(afterSeq);
        }
        var list = new ArrayList<Object>(changes.size());
        for (Change c : changes) {
            list.add(c.toMap());
        }
        resp.put("changes", list);
    }

    private void stats(HttpExchange ex, Map<String, Object> resp) {
        requireMethod(ex, "GET");
        resp.putAll(engine.stats());
    }

    private void reset(HttpExchange ex, Map<String, Object> resp) throws IOException {
        requireMethod(ex, "POST");
        // body optional; ignore
        ex.getRequestBody().readAllBytes();
        engine.reset();
        resp.put("status", "reset");
    }

    // ---------------- plumbing ----------------

    private static void fillResult(Map<String, Object> resp, SessionEngine.Result r) {
        resp.put("status", r.status().name());
        resp.put("revision", r.revision());
        resp.put("watermark", r.watermark());
        if (r.reason() != null) {
            resp.put("reason", r.reason());
        }
        var changes = new ArrayList<Object>(r.changes().size());
        for (Change c : r.changes()) {
            changes.add(c.toMap());
        }
        resp.put("changes", changes);
    }

    private static void requireMethod(HttpExchange ex, String method) {
        if (!method.equals(ex.getRequestMethod())) {
            throw new ApiException(405, "Method " + ex.getRequestMethod() + " not allowed; use " + method);
        }
    }

    private static Map<String, Object> readJsonObject(HttpExchange ex) throws IOException {
        String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        if (body.isBlank()) {
            throw new ApiException(400, "Request body must be a JSON object");
        }
        try {
            return Json.parseObject(body);
        } catch (RuntimeException e) {
            throw new ApiException(400, "Invalid JSON: " + e.getMessage());
        }
    }

    private static String queryParam(HttpExchange ex, String name) {
        String q = ex.getRequestURI().getRawQuery();
        if (q == null) {
            return null;
        }
        for (String pair : q.split("&")) {
            int i = pair.indexOf('=');
            String k = i < 0 ? pair : pair.substring(0, i);
            if (k.equals(name)) {
                return i < 0 ? "" : java.net.URLDecoder.decode(pair.substring(i + 1), StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    private static final class ApiException extends RuntimeException {
        final int code;

        ApiException(int code, String message) {
            super(message);
            this.code = code;
        }
    }

    @FunctionalInterface
    private interface Handler {
        void handle(HttpExchange ex, Map<String, Object> response) throws IOException;
    }

    private void handle(HttpExchange ex, Handler handler) {
        int code = 200;
        Object payload;
        try {
            var resp = new LinkedHashMap<String, Object>();
            handler.handle(ex, resp);
            payload = resp;
        } catch (ApiException e) {
            code = e.code;
            var err = new LinkedHashMap<String, Object>();
            err.put("error", e.getMessage());
            payload = err;
        } catch (Exception e) {
            code = 500;
            var err = new LinkedHashMap<String, Object>();
            err.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
            payload = err;
        }
        byte[] bytes = Json.write(payload).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        try {
            ex.sendResponseHeaders(code, bytes.length);
            try (OutputStream os = ex.getResponseBody()) {
                os.write(bytes);
            }
        } catch (IOException ignored) {
            // client disconnected; nothing actionable
        } finally {
            ex.close();
        }
    }
}
