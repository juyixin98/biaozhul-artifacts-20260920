package com.example.window;

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
 * HTTP facade around {@link WindowEngine}, using only the JDK's built-in
 * {@code com.sun.net.httpserver.HttpServer}. The single-threaded executor
 * applies requests strictly in arrival order.
 */
public final class WindowServer {

    private WindowServer() {
    }

    public static void main(String[] args) throws IOException {
        int port = intArg(args, "--port", 8080);
        long windowSize = longArg(args, "--window-size", 10);
        long allowedLateness = longArg(args, "--allowed-lateness", 2);

        WindowEngine engine = new WindowEngine(windowSize, allowedLateness);
        HttpServer server = start(engine, port);

        System.out.println("event-time tumbling window server started");
        System.out.println("  port             = " + server.getAddress().getPort());
        System.out.println("  windowSize       = " + windowSize);
        System.out.println("  allowedLateness  = " + allowedLateness);
        System.out.println("  try: curl -s http://127.0.0.1:" + server.getAddress().getPort() + "/api/state");
    }

    /** Creates and starts the HTTP server. Used by tests with an ephemeral port. */
    public static HttpServer start(WindowEngine engine, int port) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.setExecutor(Executors.newSingleThreadExecutor(r -> {
            Thread t = new Thread(r, "window-server");
            t.setDaemon(true);
            return t;
        }));

        server.createContext("/api/events", ex -> dispatch(ex, "POST", () -> postEvents(engine, ex)));
        server.createContext("/api/watermark", ex -> dispatch(ex, "POST", () -> postWatermark(engine, ex)));
        server.createContext("/api/partitions/idle", ex -> dispatch(ex, "POST", () -> postIdle(engine, ex)));
        server.createContext("/api/state", ex -> dispatch(ex, "GET", engine::snapshot));
        server.createContext("/api/results", ex -> dispatch(ex, "GET", () -> single("results", engine.results())));
        server.createContext("/api/side-output", ex -> dispatch(ex, "GET", () -> single("sideOutput", engine.sideOutput())));
        server.createContext("/api/reset", ex -> dispatch(ex, "POST", () -> {
            engine.reset();
            return single("status", "reset");
        }));
        server.createContext("/", WindowServer::index);

        server.start();
        return server;
    }

    // ---------- handlers ----------

    private static Object postEvents(WindowEngine engine, HttpExchange ex) throws IOException {
        Object parsed = Json.parse(readBody(ex));
        List<Object> items = new ArrayList<>();
        if (parsed instanceof List) {
            items.addAll(Json.asArray(parsed));
        } else {
            items.add(parsed);
        }
        List<Object> out = new ArrayList<>();
        for (Object item : items) {
            Map<String, Object> m = Json.asObject(item);
            String id = Json.requireString(m, "id");
            String key = Json.requireString(m, "key");
            long eventTime = Json.requireLong(m, "eventTime");
            String partition = Json.requireString(m, "partition");
            out.add(engine.addEvent(id, key, eventTime, partition));
        }
        return single("results", out);
    }

    private static Object postWatermark(WindowEngine engine, HttpExchange ex) throws IOException {
        Map<String, Object> m = Json.asObject(Json.parse(readBody(ex)));
        String partition = Json.requireString(m, "partition");
        long watermark = Json.requireLong(m, "watermark");
        return engine.submitWatermark(partition, watermark);
    }

    private static Object postIdle(WindowEngine engine, HttpExchange ex) throws IOException {
        Map<String, Object> m = Json.asObject(Json.parse(readBody(ex)));
        return engine.markIdle(Json.requireString(m, "partition"));
    }

    private static void index(HttpExchange ex) throws IOException {
        String path = ex.getRequestURI().getPath();
        if (!"GET".equals(ex.getRequestMethod())) {
            send(ex, 405, single("error", "only GET is allowed on this path"));
            return;
        }
        if (!"/".equals(path)) {
            send(ex, 404, single("error", "not found: " + path));
            return;
        }
        Map<String, Object> info = new LinkedHashMap<>();
        info.put("service", "event-time tumbling window counter");
        Map<String, Object> endpoints = new LinkedHashMap<>();
        endpoints.put("POST /api/events", "post one event object or an array of them");
        endpoints.put("POST /api/watermark", "{\"partition\":\"p1\",\"watermark\":12}");
        endpoints.put("POST /api/partitions/idle", "{\"partition\":\"p1\"} marks a partition idle");
        endpoints.put("GET  /api/state", "full snapshot (watermarks, open windows, results, side output)");
        endpoints.put("GET  /api/results", "all window emissions so far");
        endpoints.put("GET  /api/side-output", "all late events routed to the side output");
        endpoints.put("POST /api/reset", "clear all state");
        info.put("endpoints", endpoints);
        send(ex, 200, info);
    }

    // ---------- plumbing ----------

    @FunctionalInterface
    private interface BodySupplier {
        Object get() throws IOException;
    }

    private static void dispatch(HttpExchange ex, String expectedMethod, BodySupplier body) throws IOException {
        try {
            if (!ex.getRequestMethod().equalsIgnoreCase(expectedMethod)) {
                send(ex, 405, single("error", "method not allowed, use " + expectedMethod));
                return;
            }
            send(ex, 200, body.get());
        } catch (Json.JsonException | IllegalArgumentException e) {
            send(ex, 400, single("error", e.getMessage()));
        } catch (Exception e) {
            send(ex, 500, single("error", e.toString()));
        } finally {
            ex.close();
        }
    }

    private static String readBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            byte[] bytes = in.readAllBytes();
            return new String(bytes, StandardCharsets.UTF_8);
        }
    }

    private static void send(HttpExchange ex, int status, Object body) throws IOException {
        byte[] bytes = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(bytes);
        }
    }

    private static Map<String, Object> single(String key, Object value) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(key, value);
        return m;
    }

    private static int intArg(String[] args, String name, int dflt) {
        return (int) longArg(args, name, dflt);
    }

    private static long longArg(String[] args, String name, long dflt) {
        for (int i = 0; i < args.length - 1; i++) {
            if (args[i].equals(name)) {
                try {
                    return Long.parseLong(args[i + 1]);
                } catch (NumberFormatException e) {
                    throw new IllegalArgumentException("option " + name + " requires an integer, got '" + args[i + 1] + "'");
                }
            }
        }
        return dflt;
    }
}
