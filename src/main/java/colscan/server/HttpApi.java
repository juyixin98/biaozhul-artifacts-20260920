package colscan.server;

import colscan.json.Json;
import colscan.query.QueryEngine;
import colscan.query.QueryRequest;
import colscan.store.Catalog;
import colscan.store.Types;

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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 纯 JDK HTTP 服务（com.sun.net.httpserver.HttpServer），零第三方依赖。
 *
 * 路由：
 *   GET  /health                       健康检查
 *   GET  /tables                       列出表与 schema
 *   POST /tables/{name}/ingest         追加一个分片
 *   POST /query                        范围过滤 + 聚合（同时返回裁剪与全扫描对照）
 */
public final class HttpApi {

    static final int MAX_BODY_BYTES = 16 * 1024 * 1024;

    private final Catalog catalog;
    private final QueryEngine engine;

    public HttpApi(Catalog catalog) {
        this.catalog = catalog;
        this.engine = new QueryEngine(catalog);
    }

    /** 创建并启动一个 HTTP 服务（测试与 Main 共用）。 */
    public static HttpServer newServer(String host, int port, Catalog catalog)
            throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(host, port), 0);
        HttpApi api = new HttpApi(catalog);
        server.createContext("/health", api::handleHealth);
        server.createContext("/tables", api::routeTables);
        server.createContext("/query", api::handleQuery);
        // 使用守护线程：HttpServer.stop() 不会关闭外部传入的线程池，
        // 若用普通非守护线程，调用方 stop 后 JVM 仍会挂住不退出（测试会因此卡死）。
        ExecutorService pool = Executors.newFixedThreadPool(8, r -> {
            Thread t = new Thread(r, "colscan-http");
            t.setDaemon(true);
            return t;
        });
        server.setExecutor(pool);
        server.start();
        return server;
    }

    // ---------------- 通用 HTTP 工具 ----------------

    static void sendJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = Json.pretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(payload);
        }
    }

    static void sendError(HttpExchange ex, int status, String message) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", message);
        sendJson(ex, status, body);
    }

    private static byte[] readBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            byte[] all = in.readNBytes(MAX_BODY_BYTES + 1);
            if (all.length > MAX_BODY_BYTES) {
                throw new IllegalArgumentException("请求体超过 16MB 上限");
            }
            return all;
        }
    }

    // ---------------- handlers ----------------

    private void handleHealth(HttpExchange ex) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "ok");
        sendJson(ex, 200, body);
    }

    private void routeTables(HttpExchange ex) throws IOException {
        String path = ex.getRequestURI().getPath();
        // /tables 或 /tables/{name}/ingest
        String rest = path.substring("/tables".length());
        try {
            if (rest.isEmpty() || rest.equals("/")) {
                if (!"GET".equals(ex.getRequestMethod())) {
                    sendError(ex, 405, "只支持 GET");
                    return;
                }
                Map<String, Object> body = new LinkedHashMap<>();
                List<Object> tables = new ArrayList<>();
                for (String name : catalog.listTables()) {
                    Catalog.Table t = catalog.getTable(name);
                    Map<String, Object> tm = new LinkedHashMap<>();
                    tm.put("name", name);
                    tm.put("columns", t.columns);
                    tm.put("shardCount", t.shardCount);
                    tables.add(tm);
                }
                body.put("tables", tables);
                sendJson(ex, 200, body);
                return;
            }
            if (rest.endsWith("/ingest")) {
                String table = rest.substring(1, rest.length() - "/ingest".length());
                if (!"POST".equals(ex.getRequestMethod())) {
                    sendError(ex, 405, "只支持 POST");
                    return;
                }
                handleIngest(ex, table);
                return;
            }
            sendError(ex, 404, "未知路径: " + path);
        } catch (IllegalArgumentException | Json.JsonException e) {
            sendError(ex, 400, e.getMessage());
        } catch (Exception e) {
            sendError(ex, 500, "服务器内部错误: " + e);
        }
    }

    @SuppressWarnings("unchecked")
    private void handleIngest(HttpExchange ex, String table) throws IOException {
        byte[] raw = readBody(ex);
        Map<String, Object> body = Json.parseObject(new String(raw, StandardCharsets.UTF_8));

        Object rowsObj = body.get("rows");
        if (!(rowsObj instanceof List)) {
            throw new IllegalArgumentException("缺少 rows 数组");
        }
        List<Object> rows = (List<Object>) rowsObj;
        if (rows.isEmpty()) {
            throw new IllegalArgumentException("rows 不能为空（至少一行）");
        }

        Object typesObj = body.get("types");
        Map<String, String> declaredTypes = new LinkedHashMap<>();
        if (typesObj instanceof Map) {
            for (Map.Entry<String, Object> e : ((Map<String, Object>) typesObj).entrySet()) {
                String t = String.valueOf(e.getValue());
                if (!Types.isValid(t)) {
                    throw new IllegalArgumentException(
                            "types." + e.getKey() + " 非法（仅支持 LONG / DOUBLE）: " + t);
                }
                declaredTypes.put(e.getKey(), t);
            }
        }

        // 行式 JSON -> 列式
        Map<String, List<Object>> columns = new LinkedHashMap<>();
        for (int iIt = 0; iIt < rows.size(); iIt++) {
            final int i = iIt;
            Object rowObj = rows.get(i);
            if (!(rowObj instanceof Map)) {
                throw new IllegalArgumentException("rows[" + i + "] 必须是对象");
            }
            Map<String, Object> row = (Map<String, Object>) rowObj;
            for (Map.Entry<String, Object> e : row.entrySet()) {
                columns.computeIfAbsent(e.getKey(), k -> {
                    List<Object> l = new ArrayList<>();
                    for (int j = 0; j < i; j++) l.add(null); // 前面的行缺列 -> NULL
                    return l;
                });
            }
            for (Map.Entry<String, List<Object>> e : columns.entrySet()) {
                Object v = row.get(e.getKey());
                e.getValue().add(v == Json.NULL ? null : v);
            }
        }
        int n = rows.size();
        Map<String, Object[]> data = new LinkedHashMap<>();
        for (Map.Entry<String, List<Object>> e : columns.entrySet()) {
            List<Object> values = e.getValue();
            while (values.size() < n) values.add(null);
            data.put(e.getKey(), values.toArray());
        }

        // 确定 schema：已存在表必须匹配；新表用 types 声明或从数据推断
        Catalog.Table existing = catalog.getTable(table);
        Map<String, String> schema = new LinkedHashMap<>();
        if (existing != null) {
            schema.putAll(existing.columns);
            for (String col : data.keySet()) {
                if (!schema.containsKey(col)) {
                    throw new IllegalArgumentException(
                            "列 " + col + " 不在已有表 schema 中: " + schema.keySet());
                }
            }
        } else {
            for (Map.Entry<String, Object[]> e : data.entrySet()) {
                String declared = declaredTypes.get(e.getKey());
                schema.put(e.getKey(), declared != null
                        ? declared : inferType(e.getKey(), e.getValue()));
            }
        }

        // 类型校验 + 规范化
        for (Map.Entry<String, Object[]> e : data.entrySet()) {
            String type = schema.get(e.getKey());
            Object[] values = e.getValue();
            for (int i = 0; i < values.length; i++) {
                Object v = values[i];
                if (v == null) continue;
                if (type.equals(Types.LONG) && !(v instanceof Long)) {
                    throw new IllegalArgumentException(
                            "列 " + e.getKey() + " 声明为 LONG，但 rows[" + i
                                    + "] 不是整数: " + Json.write(v));
                }
                if (type.equals(Types.DOUBLE)) {
                    if (!(v instanceof Number)) {
                        throw new IllegalArgumentException(
                                "列 " + e.getKey() + " 声明为 DOUBLE，但 rows[" + i
                                        + "] 不是数字: " + Json.write(v));
                    }
                    values[i] = ((Number) v).doubleValue();
                }
            }
        }

        boolean computeStats = !Boolean.FALSE.equals(body.get("computeStats"));
        Catalog.IngestResult result = catalog.appendShard(table, schema, data, computeStats);

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("table", table);
        resp.put("shard", result.shardIndex);
        resp.put("rows", result.rowCount);
        resp.put("statsWritten", result.statsWritten);
        resp.put("note", result.statsWritten ? null
                : "该分片统计缺失（computeStats=false），查询时裁剪器必须扫描它");
        sendJson(ex, 200, resp);
    }

    private static String inferType(String col, Object[] values) {
        String inferred = null;
        for (Object v : values) {
            if (v == null) continue;
            String t;
            if (v instanceof Long) {
                t = Types.LONG;
            } else if (v instanceof Number) {
                t = Types.DOUBLE;
            } else {
                throw new IllegalArgumentException(
                        "列 " + col + " 含非数值数据（当前仅支持 LONG / DOUBLE）: "
                                + Json.write(v) + "；可在 types 中声明类型");
            }
            if (inferred == null) {
                inferred = t;
            } else if (!inferred.equals(t)) {
                inferred = Types.DOUBLE; // 整数与小数混合 -> DOUBLE
            }
        }
        if (inferred == null) {
            throw new IllegalArgumentException(
                    "列 " + col + " 全部为 NULL，无法推断类型，请在 types 中声明（LONG/DOUBLE）");
        }
        return inferred;
    }

    private void handleQuery(HttpExchange ex) throws IOException {
        try {
            if (!"POST".equals(ex.getRequestMethod())) {
                sendError(ex, 405, "只支持 POST");
                return;
            }
            byte[] raw = readBody(ex);
            Map<String, Object> body = Json.parseObject(new String(raw, StandardCharsets.UTF_8));
            QueryRequest req = QueryRequest.parse(body, catalog);

            Map<QueryEngine.Mode, QueryEngine.ModeResult> results = engine.execute(req);
            QueryEngine.ModeResult pruned = results.get(QueryEngine.Mode.PRUNED);
            QueryEngine.ModeResult full = results.get(QueryEngine.Mode.FULL_SCAN);

            boolean consistent = pruned.matchedRows == full.matchedRows
                    && pruned.aggregates.equals(full.aggregates);

            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("table", req.table);
            resp.put("filter", filterJson(req.filter));
            resp.put("consistentWithFullScan", consistent);
            if (!consistent) {
                resp.put("warning", "裁剪结果与全扫描不一致（这应当是 bug，请上报）");
            }
            long saved = full.totalBytesRead() - pruned.totalBytesRead();
            resp.put("bytesSaved", saved);
            resp.put("bytesSavedRatio", full.totalBytesRead() == 0 ? null
                    : Math.round(saved * 10000.0 / full.totalBytesRead()) / 100.0);
            resp.put("pruned", modeJson(pruned, req));
            resp.put("fullScan", modeJson(full, req));
            sendJson(ex, 200, resp);
        } catch (IllegalArgumentException | Json.JsonException e) {
            sendError(ex, 400, e.getMessage());
        } catch (Exception e) {
            sendError(ex, 500, "服务器内部错误: " + e);
        }
    }

    private static Map<String, Object> filterJson(colscan.query.Filter f) {
        if (f == null) return null;
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("column", f.column);
        m.put("op", f.op);
        m.put("value", f.value);
        return m;
    }

    private static Map<String, Object> modeJson(QueryEngine.ModeResult r, QueryRequest req) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("mode", r.mode.name());
        m.put("totalRows", r.totalRows);
        m.put("matchedRows", r.matchedRows);
        m.put("scannedShards", r.scannedShards);
        m.put("prunedShards", r.prunedShards);
        m.put("statsBytesRead", r.statsBytes);
        m.put("dataBytesRead", r.dataBytes);
        m.put("totalBytesRead", r.totalBytesRead());
        m.put("aggregates", r.aggregates);
        List<Object> shardList = new ArrayList<>();
        for (QueryEngine.ShardOutcome s : r.shards) {
            Map<String, Object> sm = new LinkedHashMap<>();
            sm.put("shard", s.shard);
            sm.put("scanned", s.scanned);
            sm.put("reason", s.reason);
            sm.put("rowsInShard", s.rowsInShard);
            sm.put("matchedRows", s.matchedRows);
            sm.put("statsBytesRead", s.statsBytesRead);
            sm.put("dataBytesRead", s.dataBytesRead);
            sm.put("totalBytesRead", s.totalBytesRead());
            sm.put("columnsRead", s.columnsRead);
            shardList.add(sm);
        }
        m.put("shards", shardList);
        if (req.returnRows) {
            m.put("sampleRows", r.rows);
            m.put("sampleRowLimit", req.rowLimit);
        }
        return m;
    }
}
