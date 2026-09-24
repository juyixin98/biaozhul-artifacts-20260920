package com.example.diff.server;

import com.example.diff.ApplyEdits;
import com.example.diff.DiffResult;
import com.example.diff.Edit;
import com.example.diff.Lines;
import com.example.diff.MyersDiff;
import com.example.diff.UnifiedDiff;
import com.example.diff.corpus.Corpus;
import com.example.diff.corpus.Document;
import com.example.diff.corpus.SearchEngine;
import com.example.diff.json.Json;
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

/**
 * JSON HTTP service backed only by the JDK ({@code com.sun.net.httpserver}).
 *
 * <table>
 *   <caption>Endpoints</caption>
 *   <tr><td>POST /api/diff</td><td>{old,new,maxEditDistance?,maxInputChars?,context?}</td></tr>
 *   <tr><td>POST /api/apply</td><td>{old,edits:[...]} validates and applies a script</td></tr>
 *   <tr><td>POST /api/search</td><td>{query,limit?} over the built-in synthetic corpus</td></tr>
 *   <tr><td>GET  /api/corpus</td><td>lists corpus document ids/titles</td></tr>
 *   <tr><td>GET  /api/corpus/{id}</td><td>one corpus document</td></tr>
 *   <tr><td>GET  /health</td><td>liveness</td></tr>
 * </table>
 */
public final class HttpServerMain {

    private final SearchEngine engine;

    private HttpServerMain() {
        this.engine = new SearchEngine(Corpus.synthetic());
    }

    public static void start(int port) {
        try {
            HttpServer server = create(port);
            System.out.println("diff service listening on http://localhost:" + server.getAddress().getPort());
        } catch (IOException e) {
            throw new RuntimeException("server failed to start: " + e.getMessage(), e);
        }
    }

    /** Creates and starts a server; port 0 selects an ephemeral port (used by tests). */
    public static HttpServer create(int port) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        HttpServerMain app = new HttpServerMain();
        server.createContext("/", app::route);
        server.setExecutor(java.util.concurrent.Executors.newFixedThreadPool(4));
        server.start();
        return server;
    }

    private void route(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (path) {
                case "/health":
                    if ("GET".equals(method)) {
                        sendJson(ex, 200, Map.of("status", "ok"));
                    } else {
                        sendError(ex, 405, "method not allowed");
                    }
                    break;
                case "/api/diff":
                    requirePost(method, ex, this::handleDiff);
                    break;
                case "/api/apply":
                    requirePost(method, ex, this::handleApply);
                    break;
                case "/api/search":
                    requirePost(method, ex, this::handleSearch);
                    break;
                case "/api/corpus":
                    if ("GET".equals(method)) {
                        handleCorpus(ex);
                    } else {
                        sendError(ex, 405, "method not allowed");
                    }
                    break;
                default:
                    if (path.startsWith("/api/corpus/") && "GET".equals(method)) {
                        handleCorpusDoc(ex, path.substring("/api/corpus/".length()));
                    } else {
                        sendError(ex, 404, "not found: " + path);
                    }
            }
        } catch (Json.JsonException e) {
            sendError(ex, 400, "invalid JSON: " + e.getMessage());
        } catch (IllegalArgumentException e) {
            sendError(ex, 400, e.getMessage());
        } catch (Exception e) {
            sendError(ex, 500, "internal error: " + e);
        } finally {
            ex.close();
        }
    }

    private interface Handler {
        void handle(HttpExchange ex, Map<String, Object> body) throws IOException;
    }

    private void requirePost(String method, HttpExchange ex, Handler h) throws IOException {
        if (!"POST".equals(method)) {
            sendError(ex, 405, "method not allowed; use POST");
            return;
        }
        String raw = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        Map<String, Object> body = Json.parseObject(raw);
        h.handle(ex, body);
    }

    // ------------------------------------------------------------------
    // Handlers
    // ------------------------------------------------------------------

    private void handleDiff(HttpExchange ex, Map<String, Object> body) throws IOException {
        String oldText = str(body, "old", "");
        String newText = str(body, "new", "");
        int maxD = intOr(body, "maxEditDistance", MyersDiff.UNLIMITED_DISTANCE);
        int maxChars = intOr(body, "maxInputChars", MyersDiff.DEFAULT_MAX_INPUT_CHARS);
        int context = intOr(body, "context", 3);
        MyersDiff diff = new MyersDiff(maxD, maxChars, context);
        DiffResult result = diff.diff(oldText, newText);

        Map<String, Object> out = Dto.diffResult(result);
        // Always include a rendered unified diff for convenience, and an
        // applied-output check so callers can see round-trip correctness.
        out.put("unifiedDiff", UnifiedDiff.render(result));
        ApplyEdits.Result applied = ApplyEdits.apply(oldText, result.edits);
        out.put("appliedOk", applied.ok);
        out.put("appliedEqualsNew", applied.ok && applied.text.equals(newText));
        sendJson(ex, 200, out);
    }

    private void handleApply(HttpExchange ex, Map<String, Object> body) throws IOException {
        String oldText = str(body, "old", "");
        Object editsRaw = body.get("edits");
        if (!(editsRaw instanceof List<?> list)) {
            throw new IllegalArgumentException("'edits' must be an array");
        }
        List<Edit> edits = new ArrayList<>();
        int idx = 0;
        for (Object o : list) {
            edits.add(parseEdit(o, idx++));
        }
        ApplyEdits.Result result = ApplyEdits.apply(oldText, edits);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("ok", result.ok);
        out.put("error", result.error);
        out.put("text", result.ok ? result.text : null);
        sendJson(ex, result.ok ? 200 : 422, out);
    }

    @SuppressWarnings("unchecked")
    private static Edit parseEdit(Object o, int idx) {
        if (!(o instanceof Map<?, ?> m)) {
            throw new IllegalArgumentException("edit " + idx + " must be an object");
        }
        String kindStr = str(m, "kind", "");
        Edit.Kind kind;
        try {
            kind = Edit.Kind.valueOf(kindStr.toUpperCase(java.util.Locale.ROOT));
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException("edit " + idx + " has unknown kind '" + kindStr + "'");
        }
        int os = intOr(m, "oldStart", 0);
        int oe = intOr(m, "oldEnd", 0);
        int ns = intOr(m, "newStart", 0);
        int ne = intOr(m, "newEnd", 0);
        List<String> oldLines = strList(m.get("oldLines"), "oldLines", idx);
        List<String> newLines = strList(m.get("newLines"), "newLines", idx);
        // Char offsets are recomputed by validation? They are informational;
        // fill from line material lengths so the object is complete.
        return new Edit(kind, os, oe, ns, ne,
                sumLen(oldLines), sumLen(oldLines), sumLen(newLines), sumLen(newLines),
                (List<String>) (List<?>) oldLines, (List<String>) (List<?>) newLines);
    }

    private static int sumLen(List<String> lines) {
        int total = 0;
        for (String l : lines) {
            total += l.length();
        }
        return total;
    }

    private static List<String> strList(Object o, String field, int idx) {
        if (o == null) {
            return new ArrayList<>();
        }
        if (!(o instanceof List<?> list)) {
            throw new IllegalArgumentException("edit " + idx + " field " + field + " must be an array of strings");
        }
        List<String> out = new ArrayList<>();
        for (Object x : list) {
            if (!(x instanceof String)) {
                throw new IllegalArgumentException("edit " + idx + " field " + field + " must contain only strings");
            }
            out.add((String) x);
        }
        return out;
    }

    private void handleSearch(HttpExchange ex, Map<String, Object> body) throws IOException {
        String query = str(body, "query", "");
        int limit = intOr(body, "limit", 10);
        List<SearchEngine.Hit> hits = engine.search(query, limit);
        List<Object> arr = new ArrayList<>();
        for (SearchEngine.Hit h : hits) {
            arr.add(Dto.hit(h));
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("query", query);
        out.put("count", arr.size());
        out.put("hits", arr);
        sendJson(ex, 200, out);
    }

    private void handleCorpus(HttpExchange ex) throws IOException {
        List<Object> arr = new ArrayList<>();
        for (Document d : engine.documents()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", d.id);
            m.put("title", d.title);
            m.put("lineCount", Lines.split(d.body).size());
            m.put("charCount", d.body.length());
            arr.add(m);
        }
        sendJson(ex, 200, Map.of("documents", arr));
    }

    private void handleCorpusDoc(HttpExchange ex, String id) throws IOException {
        for (Document d : engine.documents()) {
            if (d.id.equals(id)) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("id", d.id);
                m.put("title", d.title);
                m.put("body", d.body);
                m.put("crlf", d.body.contains("\r\n"));
                m.put("trailingNewline", d.body.endsWith("\n"));
                sendJson(ex, 200, m);
                return;
            }
        }
        sendError(ex, 404, "no such document: " + id);
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    private static String str(Map<?, ?> m, String key, String def) {
        Object v = m.get(key);
        if (v == null) {
            return def;
        }
        if (!(v instanceof String)) {
            throw new IllegalArgumentException("'" + key + "' must be a string");
        }
        return (String) v;
    }

    private static int intOr(Map<?, ?> m, String key, int def) {
        Object v = m.get(key);
        if (v == null) {
            return def;
        }
        if (v instanceof Number n) {
            return n.intValue();
        }
        throw new IllegalArgumentException("'" + key + "' must be an integer");
    }

    private void sendJson(HttpExchange ex, int status, Object payload) throws IOException {
        byte[] data = Json.writePretty(payload).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(data);
        }
    }

    private void sendError(HttpExchange ex, int status, String message) throws IOException {
        sendJson(ex, status, Map.of("error", message));
    }
}
