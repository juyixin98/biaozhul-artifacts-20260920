package io.example.orderedcommit;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * HTTP transport around {@link OrderedEventService}, built only on the JDK's
 * built-in {@code com.sun.net.httpserver.HttpServer} (no frameworks).
 *
 * <table>
 * <caption>Routes</caption>
 * <tr><th>Method + path</th><th>Purpose</th></tr>
 * <tr><td>GET  /health</td><td>liveness probe</td></tr>
 * <tr><td>GET  /stats</td><td>service-wide counters</td></tr>
 * <tr><td>GET  /partitions</td><td>list partitions</td></tr>
 * <tr><td>PUT  /partitions/{p}</td><td>create partition (idempotent)</td></tr>
 * <tr><td>GET  /partitions/{p}</td><td>partition counters</td></tr>
 * <tr><td>POST /partitions/{p}/events</td><td>submit an event</td></tr>
 * <tr><td>GET  /partitions/{p}/results</td><td>committed output (long-poll: ?waitMillis=)</td></tr>
 * <tr><td>GET  /partitions/{p}/events/{id}</td><td>event status</td></tr>
 * <tr><td>POST /partitions/{p}/events/{id}/cancel</td><td>cancel an event</td></tr>
 * </table>
 */
public final class HttpEventServer {

    private final OrderedEventService service;
    private final int httpThreads;
    private HttpServer server;

    public HttpEventServer(OrderedEventService service, int httpThreads) {
        this.service = service;
        this.httpThreads = httpThreads;
    }

    public void start(int port, String host) throws IOException {
        server = HttpServer.create(new InetSocketAddress(host, port), 0);
        server.createContext("/", this::handle);
        server.setExecutor(
                Executors.newFixedThreadPool(
                        httpThreads,
                        new ThreadFactory() {
                            private final AtomicInteger n = new AtomicInteger();

                            @Override
                            public Thread newThread(Runnable r) {
                                Thread t = new Thread(r, "http-" + n.incrementAndGet());
                                t.setDaemon(true);
                                return t;
                            }
                        }));
        server.start();
    }

    public int port() {
        return server == null ? -1 : server.getAddress().getPort();
    }

    public void stop() {
        if (server != null) {
            server.stop(1);
        }
        service.shutdown();
    }

    // ------------------------------------------------------------------

    private void handle(HttpExchange exchange) throws IOException {
        try {
            route(exchange);
        } catch (EventException e) {
            writeJson(exchange, e.httpStatus(), errorBody(e.httpStatus(), e.getMessage()));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            writeJson(exchange, 500, errorBody(500, "interrupted"));
        } catch (Exception e) {
            writeJson(exchange, 500, errorBody(500, e.getClass().getSimpleName() + ": " + e.getMessage()));
        } finally {
            exchange.close();
        }
    }

    private void route(HttpExchange exchange) throws IOException, InterruptedException {
        String method = exchange.getRequestMethod();
        Path path = Path.parse(exchange.getRequestURI().getPath());
        Map<String, String> query = parseQuery(exchange.getRequestURI().getRawQuery());

        if (path.equalsTo("health")) {
            requireGet(method);
            writeJson(exchange, 200, Map.of("status", "ok"));
            return;
        }
        if (path.equalsTo("stats")) {
            requireGet(method);
            writeJson(exchange, 200, service.stats());
            return;
        }
        if (path.equalsTo("partitions")) {
            requireGet(method);
            writeJson(exchange, 200, Map.of("partitions", service.listPartitions()));
            return;
        }
        // /partitions/{p}...
        if (path.size() >= 2 && path.segment(0).equals("partitions")) {
            String p = decode(path.segment(1));

            // GET|PUT /partitions/{p}
            if (path.size() == 2) {
                switch (method) {
                    case "GET" -> writeJson(exchange, 200, service.partitionStats(p));
                    case "PUT" -> {
                        Map<String, Object> body = readBodyOrEmpty(exchange);
                        Long cap = optionalLong(body.get("bufferCap"));
                        writeJson(exchange, 200, service.createPartition(p, cap));
                    }
                    default -> throw methodNotAllowed(method, "GET, PUT");
                }
                return;
            }

            // POST /partitions/{p}/events
            if (path.size() == 3 && path.segment(2).equals("events")) {
                if (!"POST".equals(method)) {
                    throw methodNotAllowed(method, "POST");
                }
                writeJson(exchange, 202, service.submit(p, readBody(exchange)));
                return;
            }

            // GET /partitions/{p}/results
            if (path.size() == 3 && path.segment(2).equals("results")) {
                requireGet(method);
                long since = longQuery(query, "sinceSeq", -1);
                long wait = longQuery(query, "waitMillis", 0);
                if (wait < 0 || wait > 120_000) {
                    throw EventException.badRequest("waitMillis must be in [0,120000]");
                }
                List<Map<String, Object>> results = service.results(p, since, wait);
                writeJson(exchange, 200, Map.of("partition", p, "results", results));
                return;
            }

            // POST /partitions/{p}/events/{id}/cancel
            if (path.size() == 5
                    && path.segment(2).equals("events")
                    && path.segment(4).equals("cancel")) {
                if (!"POST".equals(method)) {
                    throw methodNotAllowed(method, "POST");
                }
                String id = decode(path.segment(3));
                Map<String, Object> body = readBodyOrEmpty(exchange);
                Object reason = body.get("reason");
                writeJson(
                        exchange,
                        200,
                        service.cancel(
                                p, id, reason == null || reason == Json.NULL ? null : String.valueOf(reason)));
                return;
            }

            // GET /partitions/{p}/events/{id}
            if (path.size() == 4 && path.segment(2).equals("events")) {
                requireGet(method);
                writeJson(exchange, 200, service.getEvent(p, decode(path.segment(3))));
                return;
            }
        }

        writeJson(exchange, 404, errorBody(404, "no route for " + method + " " + path.raw));
    }

    private static void requireGet(String method) {
        if (!"GET".equals(method)) {
            throw methodNotAllowed(method, "GET");
        }
    }

    private static EventException methodNotAllowed(String method, String allowed) {
        EventException e = new EventException(405, "method " + method + " not allowed; use " + allowed);
        return e;
    }

    // ------------------------------------------------------------------
    // Request/response helpers
    // ------------------------------------------------------------------

    private static final long MAX_BODY = 1_048_576L;

    @SuppressWarnings("unchecked")
    private static Map<String, Object> readBody(HttpExchange exchange) throws IOException {
        byte[] bytes = exchange.getRequestBody().readNBytes((int) MAX_BODY + 1);
        if (bytes.length > MAX_BODY) {
            throw EventException.badRequest("request body too large (max 1MiB)");
        }
        if (bytes.length == 0) {
            throw EventException.badRequest("expected a JSON request body");
        }
        String text = new String(bytes, StandardCharsets.UTF_8);
        try {
            return Json.readObject(text);
        } catch (Json.JsonException e) {
            throw EventException.badRequest("invalid JSON: " + e.getMessage());
        }
    }

    private static Map<String, Object> readBodyOrEmpty(HttpExchange exchange) throws IOException {
        byte[] bytes = exchange.getRequestBody().readNBytes((int) MAX_BODY + 1);
        if (bytes.length == 0) {
            return new LinkedHashMap<>();
        }
        try {
            return Json.readObject(new String(bytes, StandardCharsets.UTF_8));
        } catch (Json.JsonException e) {
            throw EventException.badRequest("invalid JSON: " + e.getMessage());
        }
    }

    private static void writeJson(HttpExchange exchange, int status, Object body) throws IOException {
        byte[] data = Json.write(body).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, data.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(data);
        }
    }

    private static Map<String, Object> errorBody(int status, String message) {
        return Map.of("error", Map.of("status", status, "message", message));
    }

    private static Long optionalLong(Object value) {
        if (value == null || value == Json.NULL) {
            return null;
        }
        if (value instanceof Number n) {
            return n.longValue();
        }
        try {
            return Long.parseLong(String.valueOf(value));
        } catch (NumberFormatException e) {
            throw EventException.badRequest("value must be an integer");
        }
    }

    private static long longQuery(Map<String, String> query, String key, long fallback) {
        String raw = query.get(key);
        if (raw == null) {
            return fallback;
        }
        try {
            return Long.parseLong(raw);
        } catch (NumberFormatException e) {
            throw EventException.badRequest("query parameter " + key + " must be an integer");
        }
    }

    private static String decode(String raw) {
        return java.net.URLDecoder.decode(raw, StandardCharsets.UTF_8);
    }

    private static Map<String, String> parseQuery(String raw) {
        Map<String, String> out = new LinkedHashMap<>();
        if (raw == null || raw.isEmpty()) {
            return out;
        }
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String key = eq < 0 ? pair : pair.substring(0, eq);
            String value = eq < 0 ? "" : pair.substring(eq + 1);
            out.put(decode(key), decode(value));
        }
        return out;
    }

    /** Simple parsed path: "/" split into segments, e.g. ["partitions/foo", "events"]. */
    private record Path(String raw, List<String> segments) {

        static Path parse(String raw) {
            String trimmed = raw.startsWith("/") ? raw.substring(1) : raw;
            if (trimmed.endsWith("/") && trimmed.length() > 1) {
                trimmed = trimmed.substring(0, trimmed.length() - 1);
            }
            List<String> parts =
                    trimmed.isEmpty() ? List.of() : List.of(trimmed.split("/", -1));
            return new Path(raw, parts);
        }

        int size() {
            return segments.size();
        }

        String segment(int i) {
            return segments.get(i);
        }

        boolean equalsTo(String literal) {
            return segments.size() == 1 && segments.get(0).equals(literal);
        }
    }
}
