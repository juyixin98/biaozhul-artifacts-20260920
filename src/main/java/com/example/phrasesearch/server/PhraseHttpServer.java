package com.example.phrasesearch.server;

import com.example.phrasesearch.json.Json;
import com.example.phrasesearch.json.JsonException;
import com.example.phrasesearch.service.PhraseService;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 基于 JDK 内置 HttpServer 的 JSON HTTP 服务（无任何第三方依赖）。
 *
 * 路由：
 *   GET  /health                       -> {"status":"ok"}
 *   GET  /config                       -> 分析器/停用词/slop 语义说明
 *   GET  /docs                         -> 全部文档的全局 token 流与位置
 *   POST /search  {"query","slop","field"}
 *   GET  /search?q=...&slop=...&field=...
 *   POST /analyze {"text":"..."}
 *   GET  /analyze?text=...
 */
public final class PhraseHttpServer {

    private final PhraseService service;
    private HttpServer server;
    private java.util.concurrent.ExecutorService executor;

    public PhraseHttpServer(PhraseService service) {
        this.service = service;
    }

    public void start(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/config", this::handleConfig);
        server.createContext("/docs", this::handleDocs);
        server.createContext("/search", this::handleSearch);
        server.createContext("/analyze", this::handleAnalyze);
        // 外部传入的线程池不会随 server.stop() 关闭，需自己持有并在 stop() 中 shutdown，
        // 否则非守护工作线程会让 JVM 一直存活（测试中观察到挂住）。
        executor = java.util.concurrent.Executors.newFixedThreadPool(4);
        server.setExecutor(executor);
        server.start();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
            server = null;
        }
        if (executor != null) {
            executor.shutdownNow();
            executor = null;
        }
    }

    public int getPort() {
        return server == null ? -1 : server.getAddress().getPort();
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "ok");
        body.put("docs", service.index().size());
        writeJson(ex, 200, body);
    }

    private void handleConfig(HttpExchange ex) throws IOException {
        writeJson(ex, 200, service.buildConfigResponse());
    }

    private void handleDocs(HttpExchange ex) throws IOException {
        writeJson(ex, 200, service.buildDocsResponse());
    }

    private void handleAnalyze(HttpExchange ex) throws IOException {
        try {
            Map<String, Object> params = readParams(ex);
            String text = strParam(params, "text", "");
            writeJson(ex, 200, service.buildAnalyzeResponse(text));
        } catch (IllegalArgumentException e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    private void handleSearch(HttpExchange ex) throws IOException {
        try {
            Map<String, Object> params = readParams(ex);
            String query = strParam(params, "query",
                    strParam(params, "q", null));
            if (query == null) {
                throw new IllegalArgumentException("missing required parameter: query");
            }
            int slop = intParam(params, "slop", 0);
            if (slop < 0) {
                throw new IllegalArgumentException("slop must be >= 0");
            }
            String field = strParam(params, "field", null);
            if (field != null && (field.isBlank() || "_all_cross_field".equals(field))) {
                field = null; // 显式的“跨字段”标记也接受
            }
            if (field != null && !service.index().allDocs().isEmpty()
                    && !service.index().doc(0).fieldOrder().contains(field)) {
                // 未知字段不是硬错误（可能只是没有数据），返回空命中前先给出 400 更利于排障
                throw new IllegalArgumentException(
                        "unknown field: " + field + "; known fields are title, body");
            }

            long t0 = System.nanoTime();
            PhraseService.SearchResult result = service.search(query, slop, field);
            long elapsedMs = (System.nanoTime() - t0) / 1_000_000;
            writeJson(ex, 200, service.buildSearchResponse(result, elapsedMs));
        } catch (IllegalArgumentException | JsonException e) {
            writeError(ex, 400, e.getMessage());
        }
    }

    // ---------- 请求解析 ----------

    private static Map<String, Object> readParams(HttpExchange ex) throws IOException {
        Map<String, Object> params = new HashMap<>();
        // query string 对 GET/POST 都解析一遍，方便 curl 调试
        String rawQuery = ex.getRequestURI().getRawQuery();
        if (rawQuery != null && !rawQuery.isBlank()) {
            params.putAll(parseQueryString(rawQuery));
        }
        String method = ex.getRequestMethod();
        if ("POST".equals(method)) {
            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            if (!body.isBlank()) {
                String contentType = ex.getRequestHeaders().getFirst("Content-Type");
                if (contentType != null && contentType.contains("application/json")) {
                    Object parsed = Json.parse(body);
                    if (parsed instanceof Map<?, ?> map) {
                        for (Map.Entry<?, ?> e : map.entrySet()) {
                            params.put(String.valueOf(e.getKey()), e.getValue());
                        }
                    } else {
                        throw new IllegalArgumentException("request body must be a JSON object");
                    }
                } else {
                    // 也容忍 form-urlencoded 的 POST
                    params.putAll(parseQueryString(body));
                }
            }
        }
        return params;
    }

    private static Map<String, String> parseQueryString(String raw) {
        Map<String, String> map = new HashMap<>();
        for (String pair : raw.split("&")) {
            if (pair.isEmpty()) {
                continue;
            }
            int eq = pair.indexOf('=');
            String key = eq < 0 ? pair : pair.substring(0, eq);
            String val = eq < 0 ? "" : pair.substring(eq + 1);
            map.put(URLDecoder.decode(key, StandardCharsets.UTF_8),
                    URLDecoder.decode(val, StandardCharsets.UTF_8));
        }
        return map;
    }

    private static String strParam(Map<String, Object> params, String name, String dflt) {
        Object v = params.get(name);
        if (v == null) {
            return dflt;
        }
        return String.valueOf(v);
    }

    private static int intParam(Map<String, Object> params, String name, int dflt) {
        Object v = params.get(name);
        if (v == null) {
            return dflt;
        }
        if (v instanceof Number n) {
            return n.intValue();
        }
        try {
            return Integer.parseInt(String.valueOf(v).trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("parameter '" + name + "' must be an integer");
        }
    }

    // ---------- 响应 ----------

    private static void writeError(HttpExchange ex, int status, String message) throws IOException {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("error", true);
        err.put("status", status);
        err.put("message", message);
        writeJson(ex, status, err);
    }

    private static void writeJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] bytes = Json.writePretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.getResponseHeaders().set("Content-Length", String.valueOf(bytes.length));
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }
}
