package com.tvl.http;

import com.tvl.engine.BatchResult;
import com.tvl.engine.QueryExecutor;
import com.tvl.engine.QueryRequest;
import com.tvl.json.Json;
import com.tvl.json.JsonException;
import com.tvl.json.JsonWriter;
import com.tvl.sql.SqlParseException;
import com.tvl.types.TypeCheckException;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * 纯 JDK HTTP 服务（com.sun.net.httpserver，零外部依赖）。
 *
 * 路由：
 *   GET  /health  -> {"ok":true}
 *   POST /query   -> 三值逻辑列批次查询
 *   GET  /        -> 简要用法说明
 */
public final class QueryHttpServer {

    private final HttpServer server;

    public QueryHttpServer(int port) throws IOException {
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        this.server.createContext("/health", this::handleHealth);
        this.server.createContext("/query", this::handleQuery);
        this.server.createContext("/", this::handleRoot);
        this.server.setExecutor(Executors.newFixedThreadPool(8));
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
    }

    public int getPort() {
        return server.getAddress().getPort();
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) {
            writeJson(ex, 405, ApiCodec.error("METHOD_NOT_ALLOWED", "只支持 GET"));
            return;
        }
        writeJson(ex, 200, Map.of("ok", true, "service", "tvl-query-executor"));
    }

    private void handleRoot(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) {
            writeJson(ex, 405, ApiCodec.error("METHOD_NOT_ALLOWED", "只支持 GET"));
            return;
        }
        String help = """
                {
                  "service": "tvl-query-executor",
                  "endpoints": {
                    "GET /health": "健康检查",
                    "POST /query": "三值逻辑列批次查询（body 为 JSON）"
                  },
                  "sql": "SELECT *|expr [AS alias], ... FROM table [WHERE pred]",
                  "where": "比较 (= <> < <= > >=)、IS [NOT] NULL、AND/OR/NOT、括号、位置参数 ?",
                  "note": "强类型：字符串不与数值/布尔隐式互转；WHERE 只放行 TRUE，UNKNOWN 与 FALSE 一起被过滤"
                }
                """;
        writeBytes(ex, 200, help.getBytes(StandardCharsets.UTF_8));
    }

    private void handleQuery(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            writeJson(ex, 405, ApiCodec.error("METHOD_NOT_ALLOWED", "只支持 POST"));
            return;
        }
        String body;
        try {
            byte[] raw = ex.getRequestBody().readAllBytes();
            body = new String(raw, StandardCharsets.UTF_8);
        } catch (IOException e) {
            writeJson(ex, 400, ApiCodec.error("BAD_REQUEST", "无法读取请求体: " + e.getMessage()));
            return;
        }

        try {
            Object parsed = Json.parse(body);
            QueryRequest request = ApiCodec.decode(parsed);
            QueryExecutor executor = new QueryExecutor();
            QueryExecutor.Plan plan = executor.prepare(request);
            List<BatchResult> results = executor.execute(request);
            writeJson(ex, 200, ApiCodec.encodeResults(
                    results, plan.outputNames(), plan.outputTypes()));
        } catch (JsonException e) {
            writeJson(ex, 400, ApiCodec.error("INVALID_JSON", e.getMessage()));
        } catch (SqlParseException e) {
            writeJson(ex, 400, ApiCodec.error("SQL_PARSE_ERROR", e.getMessage()));
        } catch (TypeCheckException e) {
            writeJson(ex, 422, ApiCodec.error("TYPE_ERROR", e.getMessage()));
        } catch (IllegalArgumentException e) {
            writeJson(ex, 400, ApiCodec.error("INVALID_REQUEST", e.getMessage()));
        } catch (RuntimeException e) {
            writeJson(ex, 500, ApiCodec.error("INTERNAL_ERROR",
                    e.getClass().getSimpleName() + ": " + e.getMessage()));
        }
    }

    private void writeJson(HttpExchange ex, int status, Object payload) throws IOException {
        writeBytes(ex, status, JsonWriter.write(payload).getBytes(StandardCharsets.UTF_8));
    }

    private void writeBytes(HttpExchange ex, int status, byte[] payload) throws IOException {
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(payload);
        }
    }
}
