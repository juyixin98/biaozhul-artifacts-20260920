package vecq;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 纯后端 JSON HTTP 入口（JDK 内置 {@link HttpServer}，不引入 Web 框架）。
 *
 * 路由：
 *   GET  /health         存活探针
 *   GET  /tables         列出目录中已注册的表
 *   POST /query          请求体为查询 JSON；200 返回结果，400 返回错误对象，500 内部错误
 *
 * 错误形态统一为 {"ok":false,"error":"..."}。
 */
public final class VecqServer {

    private final QueryEngine engine;
    private HttpServer server;

    public VecqServer(QueryEngine engine) {
        this.engine = engine;
    }

    public void start(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", this::health);
        server.createContext("/tables", this::tables);
        server.createContext("/query", this::query);
        server.setExecutor(null);
        server.start();
    }

    public int port() {
        return server == null ? -1 : server.getAddress().getPort();
    }

    public void stop() {
        if (server != null) server.stop(0);
    }

    private void health(HttpExchange ex) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("ok", true);
        m.put("service", "vecq");
        writeJson(ex, 200, m);
    }

    private void tables(HttpExchange ex) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("ok", true);
        java.util.List<Object> names = new java.util.ArrayList<>();
        for (Map.Entry<String, Table> e : engine.catalog().tables().entrySet()) {
            Map<String, Object> t = new LinkedHashMap<>();
            t.put("name", e.getKey());
            t.put("rowCount", e.getValue().rowCount());
            t.put("columns", e.getValue().columnNames());
            names.add(t);
        }
        m.put("tables", names);
        writeJson(ex, 200, m);
    }

    private void query(HttpExchange ex) throws IOException {
        try {
            if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
                writeJson(ex, 405, error("只支持 POST /query"));
                return;
            }
            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            if (body.isBlank()) {
                writeJson(ex, 400, error("请求体为空，期望查询 JSON"));
                return;
            }
            Object parsed;
            try {
                parsed = Json.parse(body);
            } catch (Json.JsonException e) {
                writeJson(ex, 400, error("JSON 解析失败: " + e.getMessage()));
                return;
            }
            QueryResult result;
            try {
                result = engine.execute(parsed);
            } catch (InvalidQueryException | InvalidSelectionException e) {
                writeJson(ex, 400, error(e.getMessage()));
                return;
            }
            Exporter.exportAll(result);
            writeJson(ex, 200, result.toResponseJson());
        } catch (Throwable t) {
            writeJson(ex, 500, error("内部错误: " + t.getClass().getSimpleName()
                    + ": " + t.getMessage()));
        }
    }

    private static Map<String, Object> error(String msg) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("ok", false);
        m.put("error", msg);
        return m;
    }

    private static void writeJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] bytes = Json.writePretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }
}
