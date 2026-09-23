package bitmapindex;

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
import java.util.concurrent.ThreadPoolExecutor;

/**
 * Pure-JDK HTTP service ({@code com.sun.net.httpserver.HttpServer}) wrapping
 * {@link BitMapIndex}. No third-party dependencies.
 *
 * <pre>
 * Endpoints
 *   GET  /health                         liveness probe
 *   POST /load      {"columns":[...optional], "rows":[ {col:val,...}, ... ]}
 *   POST /query     a boolean expression; query params limit, offset, includeRows
 *   POST /delete    {"rowIds":[...]} or {"expr":{...}}  (expr deletes all matches)
 *   POST /restore   {"rowIds":[...]}
 *   GET  /stats                           index space statistics
 *   GET  /row/{id}                        fetch one row by stable id
 * </pre>
 */
public final class HttpServerMain {

    private static final int MAX_BODY = 512 * 1024 * 1024; // 512 MiB

    private final BitMapIndex index = new BitMapIndex();
    private HttpServer server;
    private int port;

    public static void main(String[] args) throws Exception {
        int port = 8080;
        String host = "0.0.0.0";
        for (String a : args) {
            if (a.startsWith("--port=")) {
                port = Integer.parseInt(a.substring("--port=".length()));
            } else if (a.startsWith("--host=")) {
                host = a.substring("--host=".length());
            } else if (a.equals("--help") || a.equals("-h")) {
                System.out.println("Usage: java bitmapindex.HttpServerMain [--port=8080] [--host=0.0.0.0]");
                return;
            } else {
                throw new IllegalArgumentException("unknown argument: " + a);
            }
        }
        HttpServerMain app = new HttpServerMain();
        app.start(host, port);
    }

    void start(String host, int requestedPort) throws IOException {
        server = HttpServer.create(new InetSocketAddress(host, requestedPort), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/load", this::handleLoad);
        server.createContext("/query", this::handleQuery);
        server.createContext("/delete", this::handleDelete);
        server.createContext("/restore", this::handleRestore);
        server.createContext("/stats", this::handleStats);
        server.createContext("/row/", this::handleRow);
        server.setExecutor(Executors.newFixedThreadPool(Math.max(4, Runtime.getRuntime().availableProcessors())));
        server.start();
        port = server.getAddress().getPort();
        System.out.println("Bitmap index service listening on http://" + host + ":" + port);
        System.out.println("Endpoints: POST /load, POST /query, POST /delete, POST /restore, GET /stats, GET /row/{id}, GET /health");
    }

    void stop() {
        if (server != null) {
            ((ThreadPoolExecutor) server.getExecutor()).shutdown();
            server.stop(0);
        }
    }

    int getPort() {
        return port;
    }

    /* ------------------------------------------------------------------ */
    /* handlers                                                            */
    /* ------------------------------------------------------------------ */

    private void handleHealth(HttpExchange ex) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "ok");
        body.put("totalRows", index.totalRows());
        body.put("liveRows", index.liveCount());
        writeJson(ex, 200, body);
    }

    private void handleLoad(HttpExchange ex) throws IOException {
        try {
            Map<String, Object> req = readJsonObject(ex);
            Object rowsObj = req.get("rows");
            if (!(rowsObj instanceof List)) {
                throw new BadRequest("'rows' array is required");
            }
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> rows = (List<Map<String, Object>>) (List<?>) rowsObj;
            List<String> columns = new ArrayList<>();
            Object colsObj = req.get("columns");
            if (colsObj instanceof List) {
                for (Object c : (List<?>) colsObj) {
                    columns.add(String.valueOf(c));
                }
            }
            BitMapIndex.BuildResult result = index.load(rows, columns);
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("loaded", true);
            body.put("rows", result.rows);
            body.put("columns", result.columns);
            writeJson(ex, 200, body);
        } catch (BadRequest e) {
            writeError(ex, 400, e.getMessage());
        } catch (Exception e) {
            writeError(ex, 400, "load failed: " + e.getMessage());
        }
    }

    private void handleQuery(HttpExchange ex) throws IOException {
        try {
            Object expr = Json.parse(readBody(ex));
            // validate eagerly for a better error
            Map<String, String> params = queryParams(ex);
            int limit = parseIntOr(params.get("limit"), 1000);
            int offset = parseIntOr(params.get("offset"), 0);
            boolean includeRows = !"false".equals(params.getOrDefault("includeRows", "true"));
            if (limit < 0 || offset < 0) {
                throw new BadRequest("limit/offset must be non-negative");
            }

            RoaringBitmap hits = index.query(expr);
            int[] all = hits.toArray(); // ascending stable row ids
            int from = Math.min(offset, all.length);
            int to = Math.min(from + limit, all.length);
            List<Object> page = new ArrayList<>(to - from);
            List<Object> pageRows = includeRows ? new ArrayList<>(to - from) : null;
            for (int i = from; i < to; i++) {
                int id = all[i];
                page.add(id);
                if (includeRows) {
                    Map<String, Object> r = new LinkedHashMap<>();
                    r.put("rowId", id);
                    r.put("data", index.getRow(id));
                    pageRows.add(r);
                }
            }
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("count", all.length);
            body.put("liveRows", index.liveCount());
            body.put("offset", from);
            body.put("limit", limit);
            body.put("returned", to - from);
            body.put("hasMore", to < all.length);
            body.put("rowIds", page);
            if (includeRows) {
                body.put("rows", pageRows);
            }
            writeJson(ex, 200, body);
        } catch (BadRequest e) {
            writeError(ex, 400, e.getMessage());
        } catch (Exception e) {
            writeError(ex, 400, "query failed: " + e.getMessage());
        }
    }

    private void handleDelete(HttpExchange ex) throws IOException {
        try {
            Map<String, Object> req = readJsonObject(ex);
            List<Integer> targets = new ArrayList<>();
            if (req.containsKey("rowIds")) {
                Object o = req.get("rowIds");
                if (!(o instanceof List)) {
                    throw new BadRequest("'rowIds' must be an array");
                }
                for (Object x : (List<?>) o) {
                    targets.add(asInt(x));
                }
            } else if (req.containsKey("expr")) {
                RoaringBitmap hits = index.query(req.get("expr"));
                for (int id : hits.toArray()) {
                    targets.add(id);
                }
            } else {
                throw new BadRequest("provide 'rowIds' or 'expr'");
            }
            List<Object> deleted = new ArrayList<>();
            List<Object> alreadyDeleted = new ArrayList<>();
            for (int id : targets) {
                if (id < 0 || id >= index.totalRows()) {
                    throw new BadRequest("rowId out of range [0," + index.totalRows() + "): " + id);
                }
                if (index.isLive(id)) {
                    index.delete(id);
                    deleted.add(id);
                } else {
                    alreadyDeleted.add(id);
                }
            }
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("deletedCount", deleted.size());
            body.put("deleted", deleted);
            body.put("alreadyDeleted", alreadyDeleted);
            body.put("liveRows", index.liveCount());
            body.put("totalRows", index.totalRows());
            writeJson(ex, 200, body);
        } catch (BadRequest e) {
            writeError(ex, 400, e.getMessage());
        } catch (Exception e) {
            writeError(ex, 400, "delete failed: " + e.getMessage());
        }
    }

    private void handleRestore(HttpExchange ex) throws IOException {
        try {
            Map<String, Object> req = readJsonObject(ex);
            Object o = req.get("rowIds");
            if (!(o instanceof List)) {
                throw new BadRequest("'rowIds' must be an array");
            }
            List<Object> restored = new ArrayList<>();
            for (Object x : (List<?>) o) {
                int id = asInt(x);
                if (id < 0 || id >= index.totalRows()) {
                    throw new BadRequest("rowId out of range [0," + index.totalRows() + "): " + id);
                }
                if (!index.isLive(id)) {
                    index.restore(id);
                    restored.add(id);
                }
            }
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("restored", restored);
            body.put("liveRows", index.liveCount());
            writeJson(ex, 200, body);
        } catch (BadRequest e) {
            writeError(ex, 400, e.getMessage());
        } catch (Exception e) {
            writeError(ex, 400, "restore failed: " + e.getMessage());
        }
    }

    private void handleStats(HttpExchange ex) throws IOException {
        writeJson(ex, 200, index.stats());
    }

    private void handleRow(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String idStr = path.substring("/row/".length());
            if (idStr.contains("/")) {
                throw new BadRequest("bad path");
            }
            int id = Integer.parseInt(idStr);
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("rowId", id);
            body.put("live", index.isLive(id));
            body.put("data", index.getRow(id));
            writeJson(ex, 200, body);
        } catch (Exception e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    /* ------------------------------------------------------------------ */
    /* HTTP plumbing                                                       */
    /* ------------------------------------------------------------------ */

    private static final class BadRequest extends RuntimeException {
        @SuppressWarnings("unused")
        private static final long serialVersionUID = 1L;

        BadRequest(String msg) {
            super(msg);
        }
    }

    private Map<String, Object> readJsonObject(HttpExchange ex) throws IOException {
        Object v = Json.parse(readBody(ex));
        if (!(v instanceof Map)) {
            throw new BadRequest("request body must be a JSON object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) v;
        return m;
    }

    private String readBody(HttpExchange ex) throws IOException {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            throw new BadRequest("POST required");
        }
        try (InputStream in = ex.getRequestBody()) {
            byte[] buf = new byte[64 * 1024];
            java.io.ByteArrayOutputStream collected = new java.io.ByteArrayOutputStream();
            int total = 0;
            int n;
            while ((n = in.read(buf)) != -1) {
                total += n;
                if (total > MAX_BODY) {
                    throw new BadRequest("request body too large (>" + MAX_BODY + " bytes)");
                }
                collected.write(buf, 0, n);
            }
            return collected.toString(StandardCharsets.UTF_8);
        }
    }

    private void writeJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] data = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(data);
        }
    }

    private void writeError(HttpExchange ex, int status, String message) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", true);
        body.put("status", status);
        body.put("message", message);
        writeJson(ex, status, body);
    }

    private static Map<String, String> queryParams(HttpExchange ex) {
        Map<String, String> out = new LinkedHashMap<>();
        String q = ex.getRequestURI().getRawQuery();
        if (q == null) {
            return out;
        }
        for (String pair : q.split("&")) {
            int eq = pair.indexOf('=');
            String k = eq < 0 ? pair : pair.substring(0, eq);
            String v = eq < 0 ? "" : pair.substring(eq + 1);
            out.put(java.net.URLDecoder.decode(k, StandardCharsets.UTF_8),
                    java.net.URLDecoder.decode(v, StandardCharsets.UTF_8));
        }
        return out;
    }

    private static int parseIntOr(String s, int dflt) {
        if (s == null) {
            return dflt;
        }
        return Integer.parseInt(s);
    }

    private static int asInt(Object o) {
        if (o instanceof Number) {
            long l = ((Number) o).longValue();
            if (l < Integer.MIN_VALUE || l > Integer.MAX_VALUE) {
                throw new BadRequest("row id out of int range: " + l);
            }
            return (int) l;
        }
        return Integer.parseInt(String.valueOf(o));
    }
}
