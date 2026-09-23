package colscan;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * Pure-JDK HTTP front end (com.sun.net.httpserver.HttpServer). No third-party
 * dependencies. JSON in/out via the bundled {@link Json} parser.
 *
 * Routes:
 *   GET    /health
 *   GET    /tables
 *   GET    /tables/{name}
 *   POST   /tables/{name}/shards/{shardId}
 *   DELETE /tables/{name}
 *   POST   /query
 *   POST   /verify      (runs PRUNED + FULL_SCAN and compares them)
 */
public final class Server {

    private final Catalog catalog;
    private final QueryEngine engine;
    private final HttpServer http;
    private final java.util.concurrent.ExecutorService executor;

    public Server(Path dataDir, int port) throws IOException {
        this.catalog = new Catalog(dataDir);
        this.engine = new QueryEngine(catalog);
        this.http = HttpServer.create(new InetSocketAddress(port), 0);
        this.executor = Executors.newFixedThreadPool(4);
        http.setExecutor(executor);
        http.createContext("/", this::route);
    }

    public int getAddressPort() {
        return http.getAddress().getPort();
    }

    public void start() {
        http.start();
    }

    public void stop(int delaySeconds) {
        http.stop(delaySeconds);
        executor.shutdownNow();
    }

    // ---------- routing ----------

    private void route(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            if (path.equals("/health") && method.equals("GET")) {
                sendJson(ex, 200, Map.of("status", "ok", "dataDir", catalog.dataDir().toString()));
                return;
            }
            if (path.equals("/tables") && method.equals("GET")) {
                sendJson(ex, 200, Map.of("tables", catalog.tableNames()));
                return;
            }
            if (path.startsWith("/tables/")) {
                handleTables(ex, method, path);
                return;
            }
            if (path.equals("/query") && method.equals("POST")) {
                handleQuery(ex, false);
                return;
            }
            if (path.equals("/verify") && method.equals("POST")) {
                handleQuery(ex, true);
                return;
            }
            sendError(ex, 404, "no route for " + method + " " + path);
        } catch (IllegalArgumentException e) {
            sendError(ex, 400, e.getMessage());
        } catch (Exception e) {
            sendError(ex, 500, e.getClass().getSimpleName() + ": " + e.getMessage());
        }
    }

    @SuppressWarnings("unchecked")
    private void handleTables(HttpExchange ex, String method, String path) throws IOException {
        String[] parts = path.split("/");
        // "/tables/{name}" or "/tables/{name}/shards/{shardId}"
        if (parts.length == 3 && method.equals("GET")) {
            String table = parts[2];
            List<Map<String, Object>> shards = new ArrayList<>();
            for (ColumnFile.Handle h : catalog.shards(table)) {
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("shardId", h.shardId);
                m.put("rowCount", h.rowCount);
                m.put("columns", h.columns);
                m.put("stats", statsOf(h));
                shards.add(m);
            }
            sendJson(ex, 200, Map.of("table", table, "shards", shards));
            return;
        }
        if (parts.length == 3 && method.equals("DELETE")) {
            catalog.dropTable(parts[2]);
            sendJson(ex, 200, Map.of("dropped", parts[2]));
            return;
        }
        if (parts.length == 5 && parts[3].equals("shards") && method.equals("POST")) {
            String table = parts[2];
            String shardId = parts[4];
            Map<String, Object> body = (Map<String, Object>) Json.parse(readBody(ex));
            Object rowsObj = body.get("rows");
            if (!(rowsObj instanceof List)) {
                throw new IllegalArgumentException("body needs 'rows' array");
            }
            List<Map<String, Object>> rows = new ArrayList<>();
            for (Object o : (List<Object>) rowsObj) {
                if (!(o instanceof Map)) {
                    throw new IllegalArgumentException("each row must be an object");
                }
                rows.add((Map<String, Object>) o);
            }
            List<String> missing = new ArrayList<>();
            Object ms = body.get("missingStats");
            if (ms instanceof List) {
                for (Object o : (List<Object>) ms) missing.add(o.toString());
            }
            ColumnFile.Handle h = catalog.ingest(table, shardId, rows, missing);
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("ingested", h.shardId);
            resp.put("table", h.table);
            resp.put("rowCount", h.rowCount);
            resp.put("columns", h.columns);
            resp.put("stats", statsOf(h));
            resp.put("file", h.path.toString());
            sendJson(ex, 201, resp);
            return;
        }
        sendError(ex, 404, "no route for " + method + " " + path);
    }

    private Map<String, Object> statsOf(ColumnFile.Handle h) {
        Map<String, Object> all = new LinkedHashMap<>();
        for (Map.Entry<String, Shard.Stats> e : h.stats.entrySet()) {
            Shard.Stats s = e.getValue();
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("min", s.min);
            m.put("max", s.max);
            m.put("nullCount", s.nullCount);
            m.put("statsPresent", s.min != null || s.max != null || s.nullCount != null);
            all.put(e.getKey(), m);
        }
        return all;
    }

    @SuppressWarnings("unchecked")
    private void handleQuery(HttpExchange ex, boolean verify) throws IOException {
        Map<String, Object> body = (Map<String, Object>) Json.parse(readBody(ex));
        Object tableObj = body.get("table");
        if (!(tableObj instanceof String)) {
            throw new IllegalArgumentException("'table' (string) required");
        }
        String table = (String) tableObj;
        if (catalog.shards(table).isEmpty()) {
            throw new IllegalArgumentException("unknown or empty table: " + table);
        }
        Object f = body.get("filter");
        if (f == null) {
            throw new IllegalArgumentException("'filter' required (use {\"op\":\"AND\",\"predicates\":[]} for none)");
        }
        Predicate filter = Predicate.parse(f);
        List<Aggregator> aggs = Aggregator.parseAll(body.get("aggregates"));
        boolean includeRows = Boolean.TRUE.equals(body.get("includeRows"));
        String modeStr = body.get("mode") == null ? "PRUNED" : body.get("mode").toString().toUpperCase();
        QueryEngine.Mode mode;
        try {
            mode = QueryEngine.Mode.valueOf(modeStr);
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException("mode must be PRUNED or FULL_SCAN");
        }

        QueryEngine.Result pruned = engine.query(table, filter, aggs, includeRows,
                QueryEngine.Mode.PRUNED);
        if (!verify) {
            if (mode == QueryEngine.Mode.FULL_SCAN) {
                sendJson(ex, 200, resultView(engine.query(table, filter, aggs, includeRows, mode)));
            } else {
                sendJson(ex, 200, resultView(pruned));
            }
            return;
        }

        QueryEngine.Result full = engine.query(table, filter, aggs, includeRows,
                QueryEngine.Mode.FULL_SCAN);
        List<String> diffs = new ArrayList<>();
        if (pruned.matchedRowsCount != full.matchedRowsCount) {
            diffs.add("matchedRowsCount pruned=" + pruned.matchedRowsCount
                    + " full=" + full.matchedRowsCount);
        }
        if (!pruned.aggregates.equals(full.aggregates)) {
            diffs.add("aggregates differ pruned=" + pruned.aggregates + " full=" + full.aggregates);
        }
        if (includeRows && !pruned.rows.equals(full.rows)) {
            diffs.add("rows differ");
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("correct", diffs.isEmpty());
        resp.put("differences", diffs);
        resp.put("pruned", resultView(pruned));
        resp.put("fullScan", resultView(full));
        resp.put("bytesSaved", full.bytesRead - pruned.bytesRead);
        resp.put("bytesReadRatio", full.bytesRead == 0 ? null
                : ((double) pruned.bytesRead / full.bytesRead));
        resp.put("shardsSkipped", full.shardsScanned - pruned.shardsScanned);
        sendJson(ex, diffs.isEmpty() ? 200 : 409, resp);
    }

    private Map<String, Object> resultView(QueryEngine.Result r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("table", r.table);
        m.put("mode", r.mode.name());
        m.put("shardsTotal", r.shardsTotal);
        m.put("shardsScanned", r.shardsScanned);
        m.put("scannedShardIds", r.scannedShardIds);
        m.put("prunedShards", r.prunedShards);
        m.put("matchedRowsCount", r.matchedRowsCount);
        m.put("aggregates", r.aggregates);
        m.put("io", Map.of(
                "bytesRead", r.bytesRead,
                "metadataBytes", r.metadataBytes,
                "dataBytes", r.dataBytes,
                "scannedFileBytes", r.scannedFileBytes,
                "prunedFileBytes", r.prunedFileBytes,
                "totalFileBytes", r.totalFileBytes));
        if (!r.rows.isEmpty()) m.put("rows", r.rows);
        return m;
    }

    // ---------- http helpers ----------

    private static String readBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static void sendJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(payload);
        }
    }

    private static void sendError(HttpExchange ex, int status, String message) throws IOException {
        sendJson(ex, status, Map.of("error", message == null ? "error" : message));
    }

    // ---------- main ----------

    public static void main(String[] args) throws Exception {
        int port = 8080;
        Path dataDir = Path.of("data");
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--port": port = Integer.parseInt(args[++i]); break;
                case "--data": dataDir = Path.of(args[++i]); break;
                default:
                    System.err.println("usage: Server [--port 8080] [--data data]");
                    System.exit(2);
            }
        }
        Server server = new Server(dataDir, port);
        server.start();
        System.out.println("column-scan server listening on http://localhost:"
                + server.getAddressPort() + " (data dir: " + dataDir.toAbsolutePath() + ")");
    }
}
