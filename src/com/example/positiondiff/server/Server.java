package com.example.positiondiff.server;

import com.example.positiondiff.diff.DiffEngine;
import com.example.positiondiff.diff.DiffResult;
import com.example.positiondiff.diff.EditOp;
import com.example.positiondiff.json.Json;
import com.example.positiondiff.json.JsonException;
import com.example.positiondiff.json.JsonParser;
import com.example.positiondiff.model.Line;
import com.example.positiondiff.search.SearchIndex;
import com.example.positiondiff.text.LineSplitter;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

/**
 * Localhost-only JSON HTTP service. Endpoints:
 *
 * <ul>
 *   <li>POST /api/diff   — {oldText, newText, budget?, context?}</li>
 *   <li>POST /api/apply  — {oldText, ops: [...]}</li>
 *   <li>POST /api/search — {query, limit?}</li>
 *   <li>GET  /api/corpus — lists the synthetic corpus</li>
 *   <li>GET  /health</li>
 * </ul>
 */
public final class Server {

    private final SearchIndex index = SearchIndex.synthetic();
    private HttpServer http;

    public void start(int port) throws IOException {
        http = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        http.createContext("/api/diff", this::handleDiff);
        http.createContext("/api/apply", this::handleApply);
        http.createContext("/api/search", this::handleSearch);
        http.createContext("/api/corpus", this::handleCorpus);
        http.createContext("/health", this::handleHealth);
        http.createContext("/", this::handleRoot);
        http.start();
    }

    public int port() {
        return http.getAddress().getPort();
    }

    public void stop() {
        if (http != null) http.stop(0);
    }

    private void handleDiff(HttpExchange ex) throws IOException {
        if (!requirePost(ex)) return;
        try {
            Map<String, Object> req = readJson(ex);
            String oldText = Api.decodeText(req, "oldText");
            String newText = Api.decodeText(req, "newText");
            var budget = Api.decodeBudget(req);
            int context = (int) Json.getLong(req, "context", 3);
            if (context < 0) context = 0;

            List<Line> oldLines = LineSplitter.split(oldText);
            List<Line> newLines = LineSplitter.split(newText);
            DiffResult result = DiffEngine.diff(oldLines, newLines, budget);

            Map<String, Object> out = Api.encodeResult(result, oldLines, newLines, context);
            out.put("unifiedDiff", com.example.positiondiff.diff.Hunks.toUnifiedDiff(
                    "old", "new", result.hunks()));
            writeJson(ex, 200, out);
        } catch (IllegalArgumentException | JsonException e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    private void handleApply(HttpExchange ex) throws IOException {
        if (!requirePost(ex)) return;
        try {
            Map<String, Object> req = readJson(ex);
            String oldText = Api.decodeText(req, "oldText");
            List<Object> opsInput = Json.getArray(req, "ops");
            if (opsInput == null) throw new IllegalArgumentException("missing 'ops' array");

            List<Line> oldLines = LineSplitter.split(oldText);
            List<EditOp> ops = Api.decodeOps(opsInput, oldLines);
            List<Line> newLines = DiffEngine.apply(oldLines, ops);
            String newText = LineSplitter.join(newLines);

            Map<String, Object> out = Json.obj();
            out.put("newText", newText);
            out.put("newTextLines", linesAsJson(newLines));
            out.put("applied", true);
            writeJson(ex, 200, out);
        } catch (IllegalArgumentException | JsonException e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    private void handleSearch(HttpExchange ex) throws IOException {
        if (!requirePost(ex)) return;
        try {
            Map<String, Object> req = readJson(ex);
            String query = Json.requireString(req, "query");
            int limit = (int) Json.getLong(req, "limit", 10);
            writeJson(ex, 200, Api.encodeSearch(index.search(query, limit)));
        } catch (IllegalArgumentException | JsonException e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    private void handleCorpus(HttpExchange ex) throws IOException {
        Map<String, Object> out = Json.obj();
        List<Object> docs = Json.arr();
        for (var doc : com.example.positiondiff.search.Corpus.docs()) {
            Map<String, Object> m = Json.obj();
            m.put("id", doc.id());
            m.put("path", doc.path());
            m.put("title", doc.title());
            m.put("lineCount", doc.lines().size());
            docs.add(m);
        }
        out.put("corpus", docs);
        writeJson(ex, 200, out);
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        Map<String, Object> out = Json.obj();
        out.put("status", "ok");
        out.put("service", "position-diff");
        writeJson(ex, 200, out);
    }

    private void handleRoot(HttpExchange ex) throws IOException {
        String help = """
                position-diff service (backend only)
                  POST /api/diff    {oldText,newText,budget?,context?}
                  POST /api/apply   {oldText,ops}
                  POST /api/search  {query,limit?}
                  GET  /api/corpus
                  GET  /health
                """;
        byte[] body = help.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "text/plain; charset=utf-8");
        ex.sendResponseHeaders(200, body.length);
        try (OutputStream os = ex.getResponseBody()) { os.write(body); }
    }

    private List<Object> linesAsJson(List<Line> lines) {
        List<Object> arr = Json.arr();
        for (Line line : lines) arr.add(Api.encodeLine(line));
        return arr;
    }

    private boolean requirePost(HttpExchange ex) throws IOException {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            writeError(ex, 405, "use POST for " + ex.getRequestURI().getPath());
            return false;
        }
        return true;
    }

    private Map<String, Object> readJson(HttpExchange ex) throws IOException {
        byte[] body = ex.getRequestBody().readAllBytes();
        if (body.length == 0) throw new IllegalArgumentException("empty request body");
        return JsonParser.parseObject(new String(body, StandardCharsets.UTF_8));
    }

    private void writeJson(HttpExchange ex, int status, Object payload) throws IOException {
        byte[] body = Json.stringify(payload).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, body.length);
        try (OutputStream os = ex.getResponseBody()) { os.write(body); }
    }

    private void writeError(HttpExchange ex, int status, String message) throws IOException {
        Map<String, Object> err = Json.obj();
        err.put("error", message == null ? "invalid request" : message);
        err.put("status", status);
        writeJson(ex, status, err);
    }
}
