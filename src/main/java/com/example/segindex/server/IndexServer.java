package com.example.segindex.server;

import com.example.segindex.Index;
import com.example.segindex.IndexReader;
import com.example.segindex.Manifest;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * Minimal JSON API over an {@link Index}, using only the JDK built-in HTTP
 * server. Every write request commits synchronously, so a successful
 * response means the change is durable.
 */
public final class IndexServer {

    private final Index index;
    private final HttpServer server;
    private final ObjectMapper mapper = new ObjectMapper();

    public IndexServer(Index index, int port) throws IOException {
        this.index = index;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/docs", this::handleDocs);
        server.createContext("/docs/", this::handleDocById);
        server.createContext("/search", this::handleSearch);
        server.createContext("/segments", this::handleSegments);
        server.createContext("/merge", this::handleMerge);
        server.createContext("/health", this::handleHealth);
        server.setExecutor(Executors.newFixedThreadPool(4));
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

    private void handleDocs(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            send(ex, 405, Map.of("error", "method not allowed"));
            return;
        }
        Map<?, ?> body = mapper.readValue(ex.getRequestBody(), Map.class);
        Object id = body.get("id");
        Object text = body.get("text");
        if (!(id instanceof String) || !(text instanceof String)) {
            send(ex, 400, Map.of("error", "expected JSON body {\"id\": string, \"text\": string}"));
            return;
        }
        long generation = index.addDocument((String) id, (String) text);
        index.commit();
        send(ex, 200, Map.of("id", (String) id, "generation", generation));
    }

    private void handleDocById(HttpExchange ex) throws IOException {
        if (!"DELETE".equals(ex.getRequestMethod())) {
            send(ex, 405, Map.of("error", "method not allowed"));
            return;
        }
        String id = ex.getRequestURI().getPath().substring("/docs/".length());
        if (id.isBlank()) {
            send(ex, 400, Map.of("error", "missing document id in path"));
            return;
        }
        boolean deleted = index.deleteDocument(id);
        index.commit();
        send(ex, 200, Map.of("id", id, "deleted", deleted));
    }

    private void handleSearch(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) {
            send(ex, 405, Map.of("error", "method not allowed"));
            return;
        }
        Map<String, String> params = queryParams(ex.getRequestURI().getRawQuery());
        String q = params.get("q");
        if (q == null || q.isBlank()) {
            send(ex, 400, Map.of("error", "missing query parameter q"));
            return;
        }
        int limit = params.containsKey("limit") ? Integer.parseInt(params.get("limit")) : 100;
        long start = System.nanoTime();
        List<IndexReader.Hit> hits = index.newReader().search(q, limit);
        long tookMs = (System.nanoTime() - start) / 1_000_000;
        List<Map<String, Object>> hitMaps = hits.stream()
                .map(h -> {
                    Map<String, Object> m = new LinkedHashMap<String, Object>();
                    m.put("id", h.docId());
                    m.put("generation", h.generation());
                    m.put("freq", h.freq());
                    m.put("text", h.text());
                    return m;
                })
                .toList();
        Map<String, Object> response = new LinkedHashMap<>();
        response.put("query", q);
        response.put("total", hitMaps.size());
        response.put("tookMs", tookMs);
        response.put("hits", hitMaps);
        send(ex, 200, response);
    }

    private void handleSegments(HttpExchange ex) throws IOException {
        Manifest m = index.manifestSnapshot();
        Map<String, Object> status = new LinkedHashMap<>();
        status.put("segments", m.segments);
        status.put("segmentCount", m.segments.size());
        status.put("tombstones", m.tombstones.size());
        status.put("trackedDocIds", m.generations.size());
        send(ex, 200, status);
    }

    private void handleMerge(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            send(ex, 405, Map.of("error", "method not allowed"));
            return;
        }
        index.forceMerge();
        send(ex, 200, Map.of("segments", index.manifestSnapshot().segments));
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        send(ex, 200, Map.of("status", "ok"));
    }

    private void send(HttpExchange ex, int status, Object body) throws IOException {
        byte[] bytes = mapper.writeValueAsBytes(body);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        ex.getResponseBody().write(bytes);
        ex.close();
    }

    private static Map<String, String> queryParams(String rawQuery) {
        Map<String, String> params = new LinkedHashMap<>();
        if (rawQuery == null) {
            return params;
        }
        for (String pair : rawQuery.split("&")) {
            int eq = pair.indexOf('=');
            if (eq > 0) {
                params.put(urlDecode(pair.substring(0, eq)), urlDecode(pair.substring(eq + 1)));
            }
        }
        return params;
    }

    private static String urlDecode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }
}
