package com.example.tvl.http;

import com.example.tvl.engine.Batch;
import com.example.tvl.engine.QueryService;
import com.example.tvl.engine.Schema;
import com.example.tvl.engine.SemanticException;
import com.example.tvl.json.Json;
import com.example.tvl.json.JsonException;
import com.example.tvl.sql.SqlParseException;
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
 * 纯 JDK HTTP 服务（com.sun.net.httpserver）。
 *
 * <pre>
 * POST /query   执行列批次查询
 * GET  /        接口说明与请求样例
 * GET  /health  健康检查
 * </pre>
 */
public final class HttpServerMain {

    private static final int PORT = Integer.getInteger("server.port", 8080);
    private static final int BACKLOG = 0;

    private final QueryService service = new QueryService();

    public static void main(String[] args) throws IOException {
        HttpServer server = new HttpServerMain().start(PORT);
        // 阻塞 main 线程，靠进程信号退出
        try {
            Thread.currentThread().join();
        } catch (InterruptedException e) {
            server.stop(0);
            Thread.currentThread().interrupt();
        }
    }

    public HttpServer start(int port) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), BACKLOG);
        server.createContext("/query", this::handleQuery);
        server.createContext("/health", this::handleHealth);
        server.createContext("/", this::handleRoot);
        java.util.concurrent.atomic.AtomicInteger seq = new java.util.concurrent.atomic.AtomicInteger();
        java.util.concurrent.ThreadPoolExecutor pool = new java.util.concurrent.ThreadPoolExecutor(
                8, 8, 30L, java.util.concurrent.TimeUnit.SECONDS,
                new java.util.concurrent.LinkedBlockingQueue<>(),
                r -> {
                    Thread t = new Thread(r, "http-worker-" + seq.incrementAndGet());
                    t.setDaemon(true); // 守护线程：server.stop 后不阻止 JVM 退出
                    return t;
                });
        server.setExecutor(pool);
        server.start();
        int actual = server.getAddress().getPort();
        System.out.println("三值逻辑查询执行器已启动: http://localhost:" + actual);
        System.out.println("POST /query 执行查询；GET / 查看接口说明。");
        return server;
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        writeJson(ex, 200, Map.of("status", "ok"));
    }

    private void handleRoot(HttpExchange ex) throws IOException {
        if (!ex.getRequestURI().getPath().equals("/")) {
            writeJson(ex, 404, errorBody("NOT_FOUND", "不存在的路径: " + ex.getRequestURI().getPath()));
            return;
        }
        if (!"GET".equalsIgnoreCase(ex.getRequestMethod())) {
            writeJson(ex, 405, errorBody("METHOD_NOT_ALLOWED", "仅支持 GET"));
            return;
        }
        Map<String, Object> help = new LinkedHashMap<>();
        help.put("service", "three-valued-logic-batch-query");
        help.put("endpoint", "POST /query");
        help.put("contentType", "application/json");
        help.put("requestFields", Map.of(
                "sql", "SELECT 列 FROM 表 [WHERE 表达式]；参数用 ? 占位",
                "schema", "{columns:[{name,type}]}；type ∈ INTEGER|FLOAT|TEXT",
                "params", "可选，按 ? 出现顺序的 [{type,value}]，value=null 表示 NULL",
                "batches", "[{rows:[[按模式排列的值...], ...]}]，可为空数组",
                "includeTvl", "可选，默认 true；返回每行 TRUE/FALSE/UNKNOWN 对照"));
        help.put("sample", sampleRequest());
        writeJson(ex, 200, help);
    }

    private void handleQuery(HttpExchange ex) throws IOException {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            writeJson(ex, 405, errorBody("METHOD_NOT_ALLOWED", "仅支持 POST"));
            return;
        }
        String body;
        try {
            body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException e) {
            writeJson(ex, 400, errorBody("BAD_REQUEST", "无法读取请求体: " + e.getMessage()));
            return;
        }

        Object parsed;
        try {
            parsed = Json.parse(body);
        } catch (JsonException e) {
            writeJson(ex, 400, errorBody("JSON_ERROR", e.getMessage()));
            return;
        }
        if (!(parsed instanceof Map<?, ?> req)) {
            writeJson(ex, 400, errorBody("JSON_ERROR", "请求体必须是 JSON 对象"));
            return;
        }

        try {
            Object sqlObj = req.get("sql");
            if (!(sqlObj instanceof String sql)) {
                throw new SemanticException("缺少字符串字段 sql");
            }
            Object schemaObj = req.get("schema");
            if (!(schemaObj instanceof Map<?, ?>)) {
                throw new SemanticException("缺少对象字段 schema");
            }
            Object colsObj = ((Map<?, ?>) schemaObj).get("columns");
            if (!(colsObj instanceof List<?> cols)) {
                throw new SemanticException("schema.columns 必须是数组");
            }
            Schema schema = Schema.from(cols);

            Object paramsObj = req.get("params");
            if (paramsObj != null && !(paramsObj instanceof List<?>)) {
                throw new SemanticException("params 必须是数组");
            }
            Object batchesObj = req.get("batches");
            if (batchesObj != null && !(batchesObj instanceof List<?>)) {
                throw new SemanticException("batches 必须是数组");
            }
            List<?> rawBatches = batchesObj == null ? List.of() : (List<?>) batchesObj;
            boolean includeTvl = boolOrDefault(req.get("includeTvl"), true);

            QueryService.Compiled compiled = service.compile(sql, schema, (List<?>) paramsObj);

            List<Batch> batches = new ArrayList<>(rawBatches.size());
            for (int i = 0; i < rawBatches.size(); i++) {
                Object bObj = rawBatches.get(i);
                if (!(bObj instanceof Map<?, ?> bm)) {
                    throw new SemanticException("batches[" + i + "] 必须是 {rows:[...]} 对象");
                }
                Object rowsObj = bm.get("rows");
                if (!(rowsObj instanceof List<?> rows)) {
                    throw new SemanticException("batches[" + i + "].rows 必须是数组");
                }
                batches.add(Batch.fromRows(schema, rows));
            }

            List<Map<String, Object>> results = service.executeBatches(compiled, batches, includeTvl);
            int totalSelected = results.stream().mapToInt(r -> (Integer) r.get("selected")).sum();

            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("ok", true);
            resp.put("batchCount", results.size());
            resp.put("totalSelected", totalSelected);
            resp.put("results", results);
            writeJson(ex, 200, resp);
        } catch (SqlParseException e) {
            writeJson(ex, 400, errorBody("SQL_PARSE_ERROR", e.getMessage()));
        } catch (SemanticException e) {
            writeJson(ex, 400, errorBody("SEMANTIC_ERROR", e.getMessage()));
        } catch (IllegalArgumentException e) {
            writeJson(ex, 400, errorBody("BAD_REQUEST", e.getMessage()));
        }
    }

    private static boolean boolOrDefault(Object v, boolean dflt) {
        if (v == null) {
            return dflt;
        }
        if (v instanceof Boolean b) {
            return b;
        }
        throw new SemanticException("includeTvl 必须是布尔值");
    }

    private static Map<String, Object> errorBody(String kind, String message) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("ok", false);
        m.put("errorKind", kind);
        m.put("error", message);
        return m;
    }

    private static void writeJson(HttpExchange ex, int status, Object payload) throws IOException {
        byte[] bytes = Json.writePretty(payload).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> sampleRequest() {
        Map<String, Object> schema = Map.of("columns", List.of(
                Map.of("name", "id", "type", "INTEGER"),
                Map.of("name", "score", "type", "FLOAT"),
                Map.of("name", "name", "type", "TEXT")));
        List<Map<String, Object>> batches = List.of(
                Map.of("rows", List.of(
                        List.of(1, 9.5, "alice"),
                        List.of(2, null, "bob"),
                        List.of(3, 7.0, null))),
                Map.of("rows", List.of(
                        List.of(4, 6.0, "dave"),
                        List.of(5, 8.25, "erin"))));
        Map<String, Object> sample = new LinkedHashMap<>();
        sample.put("sql", "SELECT id, name FROM people WHERE score >= ? AND name IS NOT NULL");
        sample.put("schema", schema);
        sample.put("params", List.of(Map.of("type", "FLOAT", "value", 8.0)));
        sample.put("batches", batches);
        sample.put("includeTvl", true);
        return sample;
    }
}
