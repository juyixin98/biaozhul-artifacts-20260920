package com.bm25pager.api;

import com.bm25pager.index.IndexManager;
import com.bm25pager.index.IndexSnapshot;
import com.bm25pager.index.SnapshotExpiredException;
import com.bm25pager.json.Json;
import com.bm25pager.model.Document;
import com.bm25pager.search.InvalidCursorException;
import com.bm25pager.search.SearchResult;
import com.bm25pager.search.SearchService;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 基于 JDK 内置 HttpServer 的 JSON HTTP 服务（无任何第三方依赖）。
 *
 * 路由：
 *   GET  /health                      健康检查
 *   POST /search                      首页检索   {"query": "...", "pageSize": 10}
 *   POST /search/continue             翻页       {"cursor": "..."}
 *   POST /documents/upsert            新增/更新  {"docId","content","metadata"?}（默认自动 commit）
 *   DELETE /documents/{docId}         删除文档（默认自动 commit）
 *   POST /admin/commit                显式发布新快照
 *   POST /admin/compact?keep=1        驱逐旧快照，仅保留最近 keep 个（演示快照过期）
 *   GET  /admin/status                当前版本、保留的版本列表、文档数等
 */
public final class ApiServer {

    private final IndexManager indexManager;
    private final SearchService searchService;
    private final HttpServer server;
    private final java.util.concurrent.ExecutorService executor;

    public ApiServer(IndexManager indexManager, int port) throws IOException {
        this.indexManager = indexManager;
        this.searchService = new SearchService(indexManager);
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        this.server.createContext("/", this::route);
        this.executor = java.util.concurrent.Executors.newFixedThreadPool(8);
        this.server.setExecutor(this.executor);
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
        executor.shutdownNow();
    }

    public int port() {
        return server.getAddress().getPort();
    }

    // ---------------------------------------------------------------------
    // 路由
    // ---------------------------------------------------------------------

    private void route(HttpExchange exchange) throws IOException {
        try {
            String method = exchange.getRequestMethod();
            String path = exchange.getRequestURI().getPath();

            if ("GET".equals(method) && "/health".equals(path)) {
                handleHealth(exchange);
            } else if ("POST".equals(method) && "/search".equals(path)) {
                handleSearch(exchange);
            } else if ("POST".equals(method) && "/search/continue".equals(path)) {
                handleContinue(exchange);
            } else if ("POST".equals(method) && "/documents/upsert".equals(path)) {
                handleUpsert(exchange);
            } else if ("DELETE".equals(method) && path.startsWith("/documents/")) {
                handleDelete(exchange, path.substring("/documents/".length()));
            } else if ("POST".equals(method) && "/admin/commit".equals(path)) {
                handleCommit(exchange);
            } else if ("POST".equals(method) && "/admin/compact".equals(path)) {
                handleCompact(exchange);
            } else if ("GET".equals(method) && "/admin/status".equals(path)) {
                handleStatus(exchange);
            } else {
                sendError(exchange, 404, "NOT_FOUND", "no route for " + method + " " + path);
            }
        } catch (ApiException ae) {
            sendError(exchange, ae.status, ae.code, ae.getMessage());
        } catch (SnapshotExpiredException see) {
            Map<String, Object> extra = new LinkedHashMap<>();
            extra.put("requestedVersion", see.requestedVersion());
            extra.put("currentVersion", see.currentVersion());
            sendError(exchange, 410, "SNAPSHOT_EXPIRED", see.getMessage(), extra);
        } catch (InvalidCursorException ice) {
            sendError(exchange, 400, "INVALID_CURSOR", ice.getMessage());
        } catch (Exception e) {
            sendError(exchange, 500, "INTERNAL_ERROR", e.toString());
        }
    }

    // ---------------------------------------------------------------------
    // 处理器
    // ---------------------------------------------------------------------

    private void handleHealth(HttpExchange exchange) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "ok");
        body.put("version", indexManager.currentVersion());
        sendJson(exchange, 200, body);
    }

    private void handleSearch(HttpExchange exchange) throws IOException {
        Map<String, Object> req = readJsonObject(exchange);
        Object queryRaw = req.get("query");
        // query 允许为空串/空白（返回空结果集），但字段必须存在且为字符串
        if (!(queryRaw instanceof String s)) {
            throw new ApiException(400, "BAD_REQUEST", "field 'query' must be a string");
        }
        Integer pageSize = asOptionalInt(req.get("pageSize"), "pageSize");
        SearchResult result = searchService.search(s, pageSize);
        sendJson(exchange, 200, render(result));
    }

    private void handleContinue(HttpExchange exchange) throws IOException {
        Map<String, Object> req = readJsonObject(exchange);
        String cursor = asString(req.get("cursor"), "cursor");
        SearchResult result = searchService.searchAfter(cursor);
        sendJson(exchange, 200, render(result));
    }

    private void handleUpsert(HttpExchange exchange) throws IOException {
        Map<String, Object> req = readJsonObject(exchange);
        String docId = asString(req.get("docId"), "docId");
        Object contentRaw = req.get("content");
        String content = contentRaw == null ? "" : String.valueOf(contentRaw);

        Map<String, String> metadata = new LinkedHashMap<>();
        Object metaRaw = req.get("metadata");
        if (metaRaw instanceof Map<?, ?> metaMap) {
            for (Map.Entry<?, ?> e : metaMap.entrySet()) {
                metadata.put(String.valueOf(e.getKey()),
                        e.getValue() == null ? "" : String.valueOf(e.getValue()));
            }
        }

        indexManager.upsert(new Document(docId, content, metadata));
        long version = indexManager.commit();

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("docId", docId);
        body.put("version", version);
        body.put("committed", true);
        sendJson(exchange, 200, body);
    }

    private void handleDelete(HttpExchange exchange, String rawDocId) throws IOException {
        String docId = urlDecode(rawDocId);
        boolean removed = indexManager.delete(docId);
        long version = indexManager.commit();

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("docId", docId);
        body.put("found", removed);
        body.put("version", version);
        sendJson(exchange, 200, body);
    }

    private void handleCommit(HttpExchange exchange) throws IOException {
        long version = indexManager.commit();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("version", version);
        sendJson(exchange, 200, body);
    }

    private void handleCompact(HttpExchange exchange) throws IOException {
        Map<String, String> params = queryParams(exchange);
        int keep = parseIntOrDefault(params.get("keep"), 1);
        int removed = indexManager.compact(keep);

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("removedSnapshots", removed);
        body.put("retainedVersions", indexManager.retainedVersions());
        body.put("currentVersion", indexManager.currentVersion());
        sendJson(exchange, 200, body);
    }

    private void handleStatus(HttpExchange exchange) throws IOException {
        IndexSnapshot snapshot = indexManager.currentSnapshot();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("currentVersion", snapshot.version());
        body.put("retainedVersions", indexManager.retainedVersions());
        body.put("docCount", snapshot.docCount());
        body.put("termCount", snapshot.termCount());
        body.put("totalLength", snapshot.totalLength());
        body.put("avgDocLength", snapshot.avgDocLength());
        sendJson(exchange, 200, body);
    }

    // ---------------------------------------------------------------------
    // 响应渲染
    // ---------------------------------------------------------------------

    private Map<String, Object> render(SearchResult result) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("version", result.version());
        body.put("page", result.page());
        body.put("pageSize", result.pageSize());
        body.put("totalHits", result.totalHits());
        body.put("hits", SearchService.renderHits(result.hits()));
        body.put("nextCursor", result.nextCursor());
        body.put("hasMore", result.hasMore());
        return body;
    }

    // ---------------------------------------------------------------------
    // IO 与参数解析辅助
    // ---------------------------------------------------------------------

    private Map<String, Object> readJsonObject(HttpExchange exchange) throws IOException {
        byte[] payload = exchange.getRequestBody().readAllBytes();
        if (payload.length == 0) {
            throw new ApiException(400, "BAD_REQUEST", "request body is empty; expected JSON object");
        }
        final Object parsed;
        try {
            parsed = Json.parse(new String(payload, StandardCharsets.UTF_8));
        } catch (Json.JsonException je) {
            throw new ApiException(400, "BAD_REQUEST", "invalid JSON: " + je.getMessage());
        }
        if (!(parsed instanceof Map<?, ?>)) {
            throw new ApiException(400, "BAD_REQUEST", "request body must be a JSON object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> cast = (Map<String, Object>) parsed;
        return cast;
    }

    private void sendJson(HttpExchange exchange, int status, Object body) throws IOException {
        byte[] bytes = Json.writePretty(body).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(bytes);
        }
    }

    private void sendError(HttpExchange exchange, int status, String code, String message)
            throws IOException {
        sendError(exchange, status, code, message, null);
    }

    private void sendError(HttpExchange exchange, int status, String code, String message,
                           Map<String, Object> extra) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", code);
        body.put("message", message);
        if (extra != null) {
            body.putAll(extra);
        }
        sendJson(exchange, status, body);
    }

    private static String asString(Object value, String field) {
        if (!(value instanceof String s) || s.isEmpty()) {
            throw new ApiException(400, "BAD_REQUEST", "field '" + field + "' must be a non-empty string");
        }
        return s;
    }

    private static Integer asOptionalInt(Object value, String field) {
        if (value == null) {
            return null;
        }
        if (value instanceof Number n) {
            return n.intValue();
        }
        throw new ApiException(400, "BAD_REQUEST", "field '" + field + "' must be an integer");
    }

    private static int parseIntOrDefault(String raw, int fallback) {
        if (raw == null) {
            return fallback;
        }
        try {
            return Integer.parseInt(raw);
        } catch (NumberFormatException nfe) {
            throw new ApiException(400, "BAD_REQUEST", "query parameter must be an integer: " + raw);
        }
    }

    private static Map<String, String> queryParams(HttpExchange exchange) {
        Map<String, String> out = new LinkedHashMap<>();
        String raw = exchange.getRequestURI().getRawQuery();
        if (raw == null || raw.isEmpty()) {
            return out;
        }
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String key = eq < 0 ? pair : pair.substring(0, eq);
            String value = eq < 0 ? "" : pair.substring(eq + 1);
            out.put(urlDecode(key), urlDecode(value));
        }
        return out;
    }

    private static String urlDecode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private static final class ApiException extends RuntimeException {
        private static final long serialVersionUID = 1L;

        final int status;
        final String code;

        ApiException(int status, String code, String message) {
            super(message);
            this.status = status;
            this.code = code;
        }
    }
}
