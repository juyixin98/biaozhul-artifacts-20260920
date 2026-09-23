package cdcrebuild.http;

import cdcrebuild.codec.Json;
import cdcrebuild.engine.CdcEngine;
import cdcrebuild.engine.SemanticException;
import cdcrebuild.model.Event;
import cdcrebuild.model.ValidationException;

import com.sun.net.httpserver.Headers;
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

/**
 * 基于 JDK 内置 com.sun.net.httpserver 的 HTTP 接口（无任何第三方依赖）。
 *
 * 路由：
 *   POST /v1/events                 投递一条变更事件
 *   GET  /v1/status                 消费位置 / 暂停原因 / 半事务列表
 *   GET  /v1/tables                 所有已提交表（深拷贝快照）
 *   GET  /v1/tables/{name}          单表
 *   GET  /v1/tables/{name}/row/{k}  按主键取行（k 为 JSON 编码，如 1 或 %22alice%22）
 *   GET  /v1/log?from=&to=          已结束事务（提交/回滚）审计
 *   GET  /healthz                   存活探针
 */
public final class ApiServer implements AutoCloseable {

    private final HttpServer server;
    private final CdcEngine engine;

    private ApiServer(HttpServer server, CdcEngine engine) {
        this.server = server;
        this.engine = engine;
    }

    public static ApiServer start(int port, CdcEngine engine) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        ApiServer api = new ApiServer(server, engine);
        server.createContext("/", api::handle);
        server.setExecutor(Executors.newFixedThreadPool(8));
        server.start();
        return api;
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public void stop() {
        server.stop(0);
    }

    @Override
    public void close() {
        stop();
    }

    // ------------------------------------------------------------ 路由

    private void handle(HttpExchange ex) {
        try {
            route(ex);
        } catch (ValidationException ve) {
            respond(ex, 400, error(400, ve.getMessage()));
        } catch (SemanticException se) {
            respond(ex, 409, error(409, se.getMessage()));
        } catch (IllegalStateException ise) {
            respond(ex, 500, error(500, "引擎错误: " + ise.getMessage()));
        } catch (Exception e) {
            respond(ex, 500, error(500, "内部错误: " + e));
        } finally {
            ex.close();
        }
    }

    private void route(HttpExchange ex) throws IOException {
        String method = ex.getRequestMethod();
        String path = ex.getRequestURI().getRawPath();

        if ("GET".equals(method) && "/healthz".equals(path)) {
            Map<String, Object> ok = new LinkedHashMap<>();
            ok.put("ok", true);
            respond(ex, 200, ok);
            return;
        }
        if ("POST".equals(method) && "/v1/events".equals(path)) {
            postEvent(ex);
            return;
        }
        if ("GET".equals(method) && "/v1/status".equals(path)) {
            respond(ex, 200, engine.status());
            return;
        }
        if ("GET".equals(method) && "/v1/tables".equals(path)) {
            respond(ex, 200, engine.snapshot());
            return;
        }
        if ("GET".equals(method)) {
            String prefix = "/v1/tables/";
            if (path.startsWith(prefix)) {
                String rest = pathDecode(path.substring(prefix.length()));
                int slash = rest.indexOf('/');
                if (slash < 0) {
                    returnTable(ex, rest);
                    return;
                }
                String table = rest.substring(0, slash);
                String tail = rest.substring(slash + 1);
                if (tail.startsWith("row/")) {
                    String keyJson = pathDecode(tail.substring(4));
                    returnRow(ex, table, keyJson);
                    return;
                }
            }
            if ("/v1/log".equals(path)) {
                queryLog(ex);
                return;
            }
        }
        respond(ex, 404, error(404, "无此路由: " + method + " " + path));
    }

    private void postEvent(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonObject(ex);
        // 结构非法（含缺失主键列等）-> 400；状态机冲突 -> 409（由统一处理层转换）
        Event event = Event.fromMap(body);
        CdcEngine.IngestResult result = engine.ingest(event);
        Map<String, Object> out = result.toMap();
        out.put("event", event.toMap());
        int httpStatus;
        switch (result.status) {
            case "DURABLE":
                httpStatus = 200;
                break;
            case "DUPLICATE":
                httpStatus = 200; // 重复位置幂等成功
                break;
            case "BUFFERED":
                httpStatus = 202; // 已缓冲但消费因缺口暂停
                break;
            default:
                httpStatus = 200;
        }
        respond(ex, httpStatus, out);
    }

    private void returnTable(HttpExchange ex, String table) {
        List<Map<String, Object>> rows = engine.tableSnapshot(table);
        if (rows == null) {
            respond(ex, 404, error(404, "表不存在: " + table));
            return;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("table", table);
        body.put("count", rows.size());
        body.put("rows", rows);
        respond(ex, 200, body);
    }

    private void returnRow(HttpExchange ex, String table, String keyJson) {
        Map<String, Object> row = engine.rowByKey(table, keyJson);
        if (row == null) {
            respond(ex, 404, error(404, "行不存在: " + table + "#" + keyJson));
            return;
        }
        respond(ex, 200, row);
    }

    private void queryLog(HttpExchange ex) {
        Map<String, String> params = queryParams(ex);
        long from = params.containsKey("from") ? parsePos(params.get("from")) : 0L;
        long to = params.containsKey("to")
                ? parsePos(params.get("to")) : Long.MAX_VALUE;
        if (to < from) {
            throw new ValidationException("to 不能小于 from");
        }
        List<CdcEngine.TxnRecord> records = engine.queryLog(from, to);
        List<Object> items = new ArrayList<>();
        for (CdcEngine.TxnRecord r : records) {
            items.add(engine.recordToMap(r));
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("from", from);
        body.put("to", to);
        body.put("count", items.size());
        body.put("transactions", items);
        respond(ex, 200, body);
    }

    private static long parsePos(String s) {
        try {
            long v = Long.parseLong(s);
            if (v < 0) {
                throw new NumberFormatException();
            }
            return v;
        } catch (NumberFormatException e) {
            throw new ValidationException("位置必须是非负整数: " + s);
        }
    }

    // ------------------------------------------------------------ 工具

    private static Map<String, Object> readJsonObject(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            String raw = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            if (raw.isBlank()) {
                throw new ValidationException("请求体为空，需要 JSON 事件对象");
            }
            Object parsed;
            try {
                parsed = Json.parse(raw);
            } catch (IllegalArgumentException je) {
                throw new ValidationException("JSON 解析失败: " + je.getMessage());
            }
            if (!(parsed instanceof Map)) {
                throw new ValidationException("请求体必须是 JSON 对象");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) parsed;
            return m;
        }
    }

    private static void respond(HttpExchange ex, int status, Object body) {
        byte[] bytes = Json.pretty(body).getBytes(StandardCharsets.UTF_8);
        Headers h = ex.getResponseHeaders();
        h.set("Content-Type", "application/json; charset=utf-8");
        try {
            ex.sendResponseHeaders(status, bytes.length);
            try (OutputStream out = ex.getResponseBody()) {
                out.write(bytes);
            }
        } catch (IOException ignored) {
            // 客户端断开等情况无需处理
        }
    }

    private static Map<String, Object> error(int status, String message) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("error", true);
        m.put("status", status);
        m.put("message", message);
        return m;
    }

    private static Map<String, String> queryParams(HttpExchange ex) {
        Map<String, String> out = new LinkedHashMap<>();
        String q = ex.getRequestURI().getRawQuery();
        if (q == null || q.isEmpty()) {
            return out;
        }
        for (String pair : q.split("&")) {
            int i = pair.indexOf('=');
            String k = i < 0 ? pair : pair.substring(0, i);
            String v = i < 0 ? "" : pair.substring(i + 1);
            out.put(pathDecode(k), pathDecode(v));
        }
        return out;
    }

    private static String pathDecode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }
}
