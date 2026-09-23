package com.example.uninorm;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * JSON-over-HTTP front end for {@link SearchEngine}, built only on the JDK's
 * bundled {@code com.sun.net.httpserver} (no external dependencies).
 *
 * <ul>
 *   <li>{@code GET  /health} - liveness probe.</li>
 *   <li>{@code GET  /search?q=...&limit=N} - query via URL parameters.</li>
 *   <li>{@code POST /search} with body {@code {"query": "...", "limit": N}}.</li>
 *   <li>{@code GET  /docs} - list corpus documents.</li>
 *   <li>{@code GET  /normalize?text=...} - show the normalized key.</li>
 * </ul>
 */
public final class SearchServer {

    private final SearchEngine engine;
    private HttpServer server;

    public SearchServer(SearchEngine engine) {
        this.engine = engine;
    }

    public void start(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/search", this::handleSearch);
        server.createContext("/docs", this::handleDocs);
        server.createContext("/normalize", this::handleNormalize);
        server.setExecutor(Executors.newCachedThreadPool());
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

    private void handleHealth(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) {
            sendJson(ex, 405, Map.of("error", "method not allowed"));
            return;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "ok");
        body.put("documents", engine.documentCount());
        sendJson(ex, 200, body);
    }

    private void handleDocs(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) {
            sendJson(ex, 405, Map.of("error", "method not allowed"));
            return;
        }
        List<Map<String, Object>> docs = new ArrayList<>();
        for (Document d : Corpus.documents()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", d.id());
            m.put("text", d.text());
            m.put("normalized", engine.normalizedOf(d.id()));
            docs.add(m);
        }
        sendJson(ex, 200, Map.of("documents", docs));
    }

    private void handleNormalize(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) {
            sendJson(ex, 405, Map.of("error", "method not allowed"));
            return;
        }
        Map<String, String> params = queryParams(ex);
        String text = params.getOrDefault("text", "");
        NormalizedText nt = TextNormalizer.normalize(text);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("original", nt.original());
        body.put("normalized", nt.normalized());
        body.put("originalCodePoints", nt.originalCodePointCount());
        body.put("normalizedUtf16Length", nt.normalized().length());
        sendJson(ex, 200, body);
    }

    private void handleSearch(HttpExchange ex) throws IOException {
        String query;
        int limit = 0;
        try {
            switch (ex.getRequestMethod()) {
                case "GET" -> {
                    Map<String, String> params = queryParams(ex);
                    query = params.get("q");
                    if (params.containsKey("limit")) {
                        limit = Integer.parseInt(params.get("limit"));
                    }
                }
                case "POST" -> {
                    String bodyText = new String(readAll(ex),
                            StandardCharsets.UTF_8);
                    Object parsed = Json.parse(bodyText);
                    if (!(parsed instanceof Map<?, ?> map)) {
                        sendJson(ex, 400,
                                Map.of("error", "body must be a JSON object"));
                        return;
                    }
                    Object q = map.get("query");
                    if (!(q instanceof String)) {
                        sendJson(ex, 400, Map.of("error",
                                "missing string field 'query'"));
                        return;
                    }
                    query = (String) q;
                    Object lim = map.get("limit");
                    if (lim instanceof Number n) {
                        limit = n.intValue();
                    }
                }
                default -> {
                    sendJson(ex, 405, Map.of("error", "method not allowed"));
                    return;
                }
            }
        } catch (IllegalArgumentException e) {
            sendJson(ex, 400, Map.of("error", "bad request: " + e.getMessage()));
            return;
        }
        if (query == null) {
            sendJson(ex, 400, Map.of("error", "missing query parameter 'q'"));
            return;
        }
        if (limit < 0) {
            limit = 0;
        }

        List<SearchHit> hits = engine.search(query, limit);
        List<Map<String, Object>> hitJson = new ArrayList<>();
        for (SearchHit h : hits) {
            OffsetRange r = h.range();
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("docId", h.docId());
            m.put("startUtf16", r.startUtf16());
            m.put("endUtf16", r.endUtf16());
            m.put("startCodePoint", r.startCodePoint());
            m.put("endCodePoint", r.endCodePoint());
            m.put("startUtf8", r.startUtf8());
            m.put("endUtf8", r.endUtf8());
            m.put("matchedOriginal", h.matchedOriginal());
            hitJson.add(m);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("query", query);
        body.put("normalizedQuery",
                TextNormalizer.normalize(query).normalized());
        body.put("count", hitJson.size());
        body.put("hits", hitJson);
        sendJson(ex, 200, body);
    }

    private static Map<String, String> queryParams(HttpExchange ex) {
        Map<String, String> params = new LinkedHashMap<>();
        String raw = ex.getRequestURI().getRawQuery();
        if (raw == null) {
            return params;
        }
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String key = eq < 0 ? pair : pair.substring(0, eq);
            String value = eq < 0 ? "" : pair.substring(eq + 1);
            params.put(URLDecoder.decode(key, StandardCharsets.UTF_8),
                    URLDecoder.decode(value, StandardCharsets.UTF_8));
        }
        return params;
    }

    private static byte[] readAll(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            return in.readAllBytes();
        }
    }

    private static void sendJson(HttpExchange ex, int status, Object body)
            throws IOException {
        byte[] bytes = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type",
                "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(bytes);
        }
    }
}
