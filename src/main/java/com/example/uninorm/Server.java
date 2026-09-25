package com.example.uninorm;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * JSON-over-HTTP service built on the JDK's embedded HTTP server.
 *
 * Endpoints:
 *   GET  /health                       -> {"status":"ok"}
 *   GET  /documents                    -> {"ids":[...]}
 *   POST /documents  {"id","text"}     -> index/replace a document
 *   POST /normalize  {"text"}          -> normalized form + segment mapping
 *   POST /search     {"query", "max"?} -> hits mapped to ORIGINAL offsets
 */
public final class Server {

    private final SearchEngine engine = new SearchEngine();
    private HttpServer http;

    public SearchEngine engine() {
        return engine;
    }

    public int start(int port) throws IOException {
        http = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        http.createContext("/health", ex -> respond(ex, 200,
                Json.encode(Json.obj("status", "ok"))));
        http.createContext("/documents", this::handleDocuments);
        http.createContext("/normalize", this::handleNormalize);
        http.createContext("/search", this::handleSearch);
        http.start();
        return http.getAddress().getPort();
    }

    public void stop() {
        if (http != null) http.stop(0);
    }

    private void handleDocuments(HttpExchange ex) throws IOException {
        if ("GET".equals(ex.getRequestMethod())) {
            respond(ex, 200, Json.encode(Json.obj("ids", engine.documentIds())));
            return;
        }
        if (!"POST".equals(ex.getRequestMethod())) {
            respond(ex, 405, error("method not allowed"));
            return;
        }
        try {
            Map<String, Object> body = Json.parseObject(readBody(ex));
            String id = requireString(body, "id");
            String text = requireString(body, "text");
            engine.addDocument(id, text);
            respond(ex, 200, Json.encode(Json.obj("indexed", id, "documents", engine.documentCount())));
        } catch (IllegalArgumentException e) {
            respond(ex, 400, error(e.getMessage()));
        }
    }

    private void handleNormalize(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            respond(ex, 405, error("method not allowed"));
            return;
        }
        try {
            Map<String, Object> body = Json.parseObject(readBody(ex));
            String text = requireString(body, "text");
            NormalizedText nt = TextNormalizer.normalize(text);
            // Segment-level mapping: merge consecutive normalized units that
            // share the same original range.
            List<Object> segments = new ArrayList<>();
            int i = 0;
            while (i < nt.text.length()) {
                int j = i + 1;
                while (j < nt.text.length()
                        && nt.origStart[j] == nt.origStart[i]
                        && nt.origEnd[j] == nt.origEnd[i]) {
                    j++;
                }
                segments.add(Json.obj(
                        "normStart", i,
                        "normEnd", j,
                        "norm", nt.text.substring(i, j),
                        "origStart", nt.origStart[i],
                        "origEnd", nt.origEnd[i],
                        "orig", text.substring(nt.origStart[i], nt.origEnd[i])));
                i = j;
            }
            respond(ex, 200, Json.encode(Json.obj(
                    "original", text,
                    "normalized", nt.text,
                    "segments", segments)));
        } catch (IllegalArgumentException e) {
            respond(ex, 400, error(e.getMessage()));
        }
    }

    private void handleSearch(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            respond(ex, 405, error("method not allowed"));
            return;
        }
        try {
            Map<String, Object> body = Json.parseObject(readBody(ex));
            String query = requireString(body, "query");
            int max = 100;
            Object m = body.get("max");
            if (m instanceof Number) max = ((Number) m).intValue();
            List<SearchEngine.Hit> hits = engine.search(query, max);
            List<Object> out = new ArrayList<>();
            for (SearchEngine.Hit h : hits) {
                out.add(Json.obj(
                        "docId", h.docId,
                        "start", h.start,
                        "end", h.end,
                        "matched", h.matched));
            }
            respond(ex, 200, Json.encode(Json.obj(
                    "query", query,
                    "normalizedQuery", TextNormalizer.normalize(query).text,
                    "hitCount", hits.size(),
                    "hits", out)));
        } catch (IllegalArgumentException e) {
            respond(ex, 400, error(e.getMessage()));
        }
    }

    private static String requireString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof String)) {
            throw new IllegalArgumentException("missing or non-string field: " + key);
        }
        return (String) v;
    }

    private static String error(String msg) {
        return Json.encode(Json.obj("error", msg == null ? "bad request" : msg));
    }

    private static String readBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static void respond(HttpExchange ex, int status, String json) throws IOException {
        byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }
}
