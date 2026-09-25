package com.example.editdistance;

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
 * JSON over HTTP front end for {@link SearchService}, built on the JDK's bundled
 * {@link com.sun.net.httpserver.HttpServer} — no external web framework.
 *
 * <p>Endpoints:
 * <ul>
 *   <li>{@code GET  /health} — liveness probe.</li>
 *   <li>{@code GET  /corpus} — corpus size, normalization policy, first terms.</li>
 *   <li>{@code POST /corpus/reload} — regenerate the synthetic corpus;
 *       body {@code {"size": 2000, "seed": 42}} (both optional).</li>
 *   <li>{@code POST /search} — body {@code {"query": "apple", "k": 2, "limit": 20}};
 *       {@code k} and {@code limit} are optional (defaults 2 and 20).</li>
 * </ul>
 */
public final class HttpApi {

    private static final int MAX_K = 10;
    private static final int MAX_LIMIT = 1000;
    private static final int MAX_BODY_BYTES = 1 << 20;

    private final SearchService service;
    private final long defaultSeed;
    private final int defaultCorpusSize;
    private HttpServer server;

    public HttpApi(SearchService service, int defaultCorpusSize, long defaultSeed) {
        this.service = service;
        this.defaultCorpusSize = defaultCorpusSize;
        this.defaultSeed = defaultSeed;
    }

    /** Starts the server; port 0 means an ephemeral port (used by tests). */
    public void start(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/corpus", this::handleCorpus);
        server.createContext("/corpus/reload", this::handleReload);
        server.createContext("/search", this::handleSearch);
        server.setExecutor(Executors.newFixedThreadPool(4));
        server.start();
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
    }

    private void handleHealth(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }
        sendJson(exchange, 200, Map.of("status", "ok"));
    }

    private void handleCorpus(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("size", service.corpusSize());
        body.put("normalize", service.normalize().name());
        sendJson(exchange, 200, body);
    }

    private void handleReload(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }
        Map<String, Object> request = parseBody(exchange);
        if (request == null) {
            return;
        }
        int size = intParam(request, "size", defaultCorpusSize, 1, 1_000_000, exchange);
        if (size < 0) {
            return; // error already sent
        }
        long seed = longParam(request, "seed", defaultSeed, exchange);
        if (seed == Long.MIN_VALUE) {
            return; // error already sent
        }
        service.rebuild(CorpusGenerator.generate(size, seed));
        sendJson(exchange, 200, Map.of("size", service.corpusSize(), "seed", seed));
    }

    private void handleSearch(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }
        Map<String, Object> request = parseBody(exchange);
        if (request == null) {
            return;
        }
        Object query = request.get("query");
        if (!(query instanceof String q)) {
            sendError(exchange, 400, "missing or non-string field: query");
            return;
        }
        int k = intParam(request, "k", 2, 0, MAX_K, exchange);
        if (k < 0) {
            return;
        }
        int limit = intParam(request, "limit", 20, 1, MAX_LIMIT, exchange);
        if (limit < 0) {
            return;
        }
        SearchResult result = service.search(q, k, limit);
        sendJson(exchange, 200, toJson(result));
    }

    private static Map<String, Object> toJson(SearchResult result) {
        Map<String, Object> stats = new LinkedHashMap<>();
        stats.put("corpusSize", result.corpusSize());
        stats.put("lengthPassed", result.lengthPassed());
        stats.put("candidates", result.candidates());
        stats.put("totalMatches", result.totalMatches());

        List<Object> matches = new ArrayList<>();
        for (Match match : result.matches()) {
            matches.add(Map.of("term", match.term(), "distance", match.distance()));
        }

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("query", result.query());
        body.put("normalizedQuery", result.normalizedQuery());
        body.put("k", result.k());
        body.put("stats", stats);
        body.put("matches", matches);
        return body;
    }

    // ---- request/response helpers ----

    private boolean requireMethod(HttpExchange exchange, String method) throws IOException {
        if (exchange.getRequestMethod().equals(method)) {
            return true;
        }
        sendError(exchange, 405, "method not allowed, use " + method);
        return false;
    }

    private Map<String, Object> parseBody(HttpExchange exchange) throws IOException {
        String text = new String(readBody(exchange), StandardCharsets.UTF_8);
        try {
            return Json.parseObject(text.isEmpty() ? "{}" : text);
        } catch (Json.JsonException e) {
            sendError(exchange, 400, "invalid JSON: " + e.getMessage());
            return null;
        }
    }

    private static byte[] readBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] body = in.readNBytes(MAX_BODY_BYTES + 1);
            if (body.length > MAX_BODY_BYTES) {
                throw new IOException("request body too large");
            }
            return body;
        }
    }

    /** Returns the int parameter, or -1 after sending a 400 error (caller must return). */
    private static int intParam(Map<String, Object> request, String name, int defaultValue,
                                int min, int max, HttpExchange exchange) throws IOException {
        Object raw = request.get(name);
        if (raw == null) {
            return defaultValue;
        }
        if (!(raw instanceof Number number)) {
            sendError(exchange, 400, "field " + name + " must be a number");
            return -1;
        }
        long value = number.longValue();
        if (value < min || value > max) {
            sendError(exchange, 400, "field " + name + " must be in [" + min + ", " + max + "]");
            return -1;
        }
        return (int) value;
    }

    /** Returns the long parameter, or Long.MIN_VALUE after sending a 400 error. */
    private static long longParam(Map<String, Object> request, String name, long defaultValue,
                                  HttpExchange exchange) throws IOException {
        Object raw = request.get(name);
        if (raw == null) {
            return defaultValue;
        }
        if (!(raw instanceof Number number)) {
            sendError(exchange, 400, "field " + name + " must be a number");
            return Long.MIN_VALUE;
        }
        return number.longValue();
    }

    private static void sendJson(HttpExchange exchange, int status, Object body) throws IOException {
        byte[] bytes = Json.write(body).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(bytes);
        }
    }

    private static void sendError(HttpExchange exchange, int status, String message) throws IOException {
        sendJson(exchange, status, Map.of("error", message));
    }
}
