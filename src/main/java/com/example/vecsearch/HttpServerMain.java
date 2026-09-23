package com.example.vecsearch;

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
 * HTTP front end built on the JDK's built-in {@code com.sun.net.httpserver}.
 * No third-party dependencies.
 *
 * Routes:
 *   GET  /health                  liveness probe
 *   GET  /stats                   store / index statistics
 *   POST /insert    {id, vector, metadata?, metric?}
 *   POST /batch     {metric?, vectors: [{id, vector, metadata?}, ...]}
 *   POST /delete    {id}
 *   POST /search    {vector, metric, k?, filter?, mode?, budget?, nprobe?}
 *   POST /reindex   rebuild the approximate indices
 *   POST /reset     clear all data (test helper)
 */
public class HttpServerMain {

    private final SearchService service;
    private final VectorStore store;

    public HttpServerMain(SearchService service, VectorStore store) {
        this.service = service;
        this.store = store;
    }

    public void start(int port, int threads) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", ex -> handle(ex, this::health));
        server.createContext("/stats", ex -> handle(ex, this::stats));
        server.createContext("/insert", ex -> handle(ex, this::insert));
        server.createContext("/batch", ex -> handle(ex, this::batch));
        server.createContext("/delete", ex -> handle(ex, this::delete));
        server.createContext("/search", ex -> handle(ex, this::search));
        server.createContext("/reindex", ex -> handle(ex, this::reindex));
        server.createContext("/reset", ex -> handle(ex, this::reset));
        server.setExecutor(Executors.newFixedThreadPool(threads));
        server.start();
        this.boundPort = server.getAddress().getPort();
        System.out.println("Vector search server listening on http://0.0.0.0:" + this.boundPort);
    }

    private int boundPort;

    public int boundPort() {
        return boundPort;
    }

    // ------------------------------------------------------------------
    // Handlers
    // ------------------------------------------------------------------

    private Object health(HttpExchange ex, Map<String, Object> body) {
        requireGet(ex);
        return Map.of("status", "ok");
    }

    private Object stats(HttpExchange ex, Map<String, Object> body) {
        requireGet(ex);
        return service.stats();
    }

    private Object insert(HttpExchange ex, Map<String, Object> body) {
        requirePost(ex);
        String id = requireString(body, "id");
        double[] vector = requireVector(body);
        Map<String, Object> metadata = optionalMetadata(body);
        Metric metric = body.containsKey("metric")
                ? Metric.fromString(asString(body.get("metric")))
                : Metric.L2;
        boolean replaced = service.upsert(id, vector, metadata, metric);
        return Map.of("inserted", !replaced, "replaced", replaced, "size", store.size());
    }

    private Object batch(HttpExchange ex, Map<String, Object> body) {
        requirePost(ex);
        Object rawVectors = body.get("vectors");
        if (!(rawVectors instanceof List<?> list) || list.isEmpty()) {
            throw new ApiException(400, "field \"vectors\" must be a non-empty array");
        }
        Metric defaultMetric = body.containsKey("metric")
                ? Metric.fromString(asString(body.get("metric")))
                : Metric.L2;
        List<String> inserted = new ArrayList<>();
        List<String> replaced = new ArrayList<>();
        for (int i = 0; i < list.size(); i++) {
            if (!(list.get(i) instanceof Map<?, ?> rawItem)) {
                throw new ApiException(400, "vectors[" + i + "] must be an object");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> item = (Map<String, Object>) rawItem;
            String id = requireString(item, "id");
            double[] vector = requireVector(item);
            Map<String, Object> metadata = optionalMetadata(item);
            Metric metric = item.containsKey("metric")
                    ? Metric.fromString(asString(item.get("metric")))
                    : defaultMetric;
            boolean wasReplaced = service.upsert(id, vector, metadata, metric);
            (wasReplaced ? replaced : inserted).add(id);
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("insertedCount", inserted.size());
        result.put("replacedCount", replaced.size());
        result.put("inserted", inserted);
        result.put("replaced", replaced);
        result.put("size", store.size());
        return result;
    }

    private Object delete(HttpExchange ex, Map<String, Object> body) {
        requirePost(ex);
        String id = requireString(body, "id");
        boolean deleted = service.delete(id);
        return Map.of("id", id, "deleted", deleted, "size", store.size());
    }

    private Object search(HttpExchange ex, Map<String, Object> body) {
        requirePost(ex);
        double[] query = requireVector(body);
        Metric metric = Metric.fromString(asString(body.get("metric")));
        int k = body.containsKey("k") ? asInt(body.get("k"), "k") : 10;
        String mode = body.containsKey("mode") ? asString(body.get("mode")) : "exact";
        Long budget = null;
        if (body.containsKey("budget") && body.get("budget") != null) {
            budget = (long) asInt(body.get("budget"), "budget");
        }
        int nprobe = 0;
        if (body.containsKey("nprobe") && body.get("nprobe") != null) {
            nprobe = asInt(body.get("nprobe"), "nprobe");
        }
        Filter filter = Filter.from(extractFilter(body));

        SearchService.Outcome outcome =
                service.search(query, metric, k, filter, mode, budget, nprobe);

        List<Object> hits = new ArrayList<>();
        for (SearchHit hit : outcome.hits) {
            Map<String, Object> j = new LinkedHashMap<>();
            j.put("id", hit.id());
            j.put("distance", hit.distance());
            j.put("metadata", hit.metadata());
            hits.add(j);
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("mode", mode);
        result.put("metric", metric.name());
        result.put("k", k);
        result.put("count", hits.size());
        result.put("hits", hits);
        result.put("vectorDistanceCalculations", outcome.vectorDistances);
        result.put("centroidDistanceCalculations", outcome.centroidDistances);
        result.put("totalDistanceCalculations",
                outcome.vectorDistances + outcome.centroidDistances);
        if ("approx".equals(mode)) {
            result.put("nprobe", outcome.nprobeUsed);
            result.put("indexBuiltThisRequest", outcome.indexBuilt);
        }
        return result;
    }

    private Object reindex(HttpExchange ex, Map<String, Object> body) {
        requirePost(ex);
        service.rebuildIndex();
        return service.stats();
    }

    private Object reset(HttpExchange ex, Map<String, Object> body) {
        requirePost(ex);
        store.clear();
        service.rebuildIndex();
        return Map.of("reset", true);
    }

    // ------------------------------------------------------------------
    // Request plumbing
    // ------------------------------------------------------------------

    private void handle(HttpExchange ex, Handler handler) {
        try {
            Map<String, Object> body = Map.of();
            if ("POST".equalsIgnoreCase(ex.getRequestMethod())) {
                String text = readBody(ex);
                if (!text.isEmpty()) {
                    try {
                        body = Json.parseObject(text);
                    } catch (Json.JsonException e) {
                        throw new ApiException(400, "invalid JSON: " + e.getMessage());
                    }
                }
            }
            Object result = handler.handle(ex, body);
            writeJson(ex, 200, result);
        } catch (ApiException e) {
            writeJson(ex, e.status(), errorBody(e.getMessage()));
        } catch (Exception e) {
            e.printStackTrace();
            writeJson(ex, 500, errorBody("internal error: " + e.getMessage()));
        } finally {
            ex.close();
        }
    }

    private interface Handler {
        Object handle(HttpExchange ex, Map<String, Object> body);
    }

    private static void requireGet(HttpExchange ex) {
        if (!"GET".equalsIgnoreCase(ex.getRequestMethod())) {
            throw new ApiException(405, "method not allowed; use GET");
        }
    }

    private static void requirePost(HttpExchange ex) {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            throw new ApiException(405, "method not allowed; use POST");
        }
    }

    private static String readBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static void writeJson(HttpExchange ex, int status, Object payload) {
        byte[] bytes = Json.write(payload).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        try {
            ex.sendResponseHeaders(status, bytes.length);
            try (OutputStream out = ex.getResponseBody()) {
                out.write(bytes);
            }
        } catch (IOException e) {
            // Client went away; nothing actionable.
        }
    }

    private static Map<String, Object> errorBody(String message) {
        return Map.of("error", message);
    }

    // ------------------------------------------------------------------
    // JSON field extraction helpers
    // ------------------------------------------------------------------

    private static String requireString(Map<String, Object> body, String field) {
        Object v = body.get(field);
        if (!(v instanceof String s) || s.isEmpty()) {
            throw new ApiException(400, "field \"" + field + "\" is required and must be a string");
        }
        return s;
    }

    private static String asString(Object v) {
        if (v instanceof String s) {
            return s;
        }
        throw new ApiException(400, "expected a string but got " + typeName(v));
    }

    private static int asInt(Object v, String field) {
        if (v instanceof Number n) {
            double d = n.doubleValue();
            if (d != Math.rint(d) || d < Integer.MIN_VALUE || d > Integer.MAX_VALUE) {
                throw new ApiException(400, "field \"" + field + "\" must be an integer");
            }
            return (int) d;
        }
        throw new ApiException(400, "field \"" + field + "\" must be an integer");
    }

    private static double[] requireVector(Map<String, Object> body) {
        Object raw = body.get("vector");
        if (!(raw instanceof List<?> list) || list.isEmpty()) {
            throw new ApiException(400,
                    "field \"vector\" is required and must be a non-empty array of numbers");
        }
        double[] vector = new double[list.size()];
        for (int i = 0; i < list.size(); i++) {
            if (!(list.get(i) instanceof Number n)) {
                throw new ApiException(400, "vector[" + i + "] must be a number");
            }
            vector[i] = n.doubleValue();
        }
        return vector;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> optionalMetadata(Map<String, Object> body) {
        if (!body.containsKey("metadata") || body.get("metadata") == null) {
            return new LinkedHashMap<>();
        }
        Object md = body.get("metadata");
        if (!(md instanceof Map<?, ?>)) {
            throw new ApiException(400, "field \"metadata\" must be an object");
        }
        return new LinkedHashMap<>((Map<String, Object>) md);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> extractFilter(Map<String, Object> body) {
        Object f = body.get("filter");
        if (f == null) {
            return Map.of();
        }
        if (!(f instanceof Map<?, ?>)) {
            throw new ApiException(400, "field \"filter\" must be an object of key=value conditions");
        }
        return (Map<String, Object>) f;
    }

    private static String typeName(Object v) {
        return v == null ? "null" : v.getClass().getSimpleName();
    }

    // ------------------------------------------------------------------
    // Entry point
    // ------------------------------------------------------------------

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(System.getProperty("port",
                System.getenv().getOrDefault("PORT", "8080")));
        int threads = Integer.parseInt(System.getProperty("threads", "8"));
        int nlist = Integer.parseInt(System.getProperty("nlist", "32"));
        int kmeansIterations = Integer.parseInt(System.getProperty("kmeansIterations", "20"));
        long seed = Long.parseLong(System.getProperty("seed", "42"));

        VectorStore store = new VectorStore();
        SearchService service = new SearchService(store, nlist, kmeansIterations, seed);
        new HttpServerMain(service, store).start(port, threads);
    }
}
