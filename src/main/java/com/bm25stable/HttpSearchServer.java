package com.bm25stable;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * JSON HTTP 服务，基于 JDK 内置 com.sun.net.httpserver，无任何外部依赖。
 *
 * <p>路由：
 * <ul>
 *   <li>GET  /health —— 存活探针</li>
 *   <li>GET  /search?q=...&pageSize=...&cursor=... —— 稳定分页检索</li>
 *   <li>POST /documents —— 新增/覆盖文档 {"id": "...", "text": "..."}</li>
 *   <li>POST /documents/bulk —— 批量 {"documents": [{"id","text"}, ...]}</li>
 *   <li>DELETE /documents/{id} —— 删除文档</li>
 *   <li>GET  /snapshots —— 当前保留的快照版本列表</li>
 * </ul>
 */
public final class HttpSearchServer {

    private final SearchEngine engine;
    private final HttpServer server;

    public HttpSearchServer(SearchEngine engine, int port) throws IOException {
        this.engine = engine;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/search", this::handleSearch);
        server.createContext("/documents", this::handleDocuments);
        server.createContext("/snapshots", this::handleSnapshots);
        // 兜底：未注册路径统一返回 JSON 404（最长前缀匹配，不会拦截上面的路径）
        server.createContext("/", exchange ->
                sendError(exchange, 404, "NOT_FOUND",
                        "no route for " + exchange.getRequestMethod() + " " + exchange.getRequestURI().getPath()));
        server.setExecutor(Executors.newFixedThreadPool(4));
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
    }

    public int port() {
        return server.getAddress().getPort();
    }

    // ------------------------------------------------------------------
    // 路由处理
    // ------------------------------------------------------------------

    private void handleHealth(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "ok");
        body.put("version", engine.currentVersion());
        body.put("docCount", engine.docCount());
        sendJson(exchange, 200, body);
    }

    private void handleSearch(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }
        try {
            Map<String, String> params = queryParams(exchange);
            String query = params.get("q");
            if (query == null || query.isBlank()) {
                throw new SearchException.BadRequest("missing required parameter: q");
            }
            int pageSize = 10;
            String ps = params.get("pageSize");
            if (ps != null && !ps.isBlank()) {
                try {
                    pageSize = Integer.parseInt(ps.trim());
                } catch (NumberFormatException e) {
                    throw new SearchException.BadRequest("pageSize must be an integer, got '" + ps + "'");
                }
            }
            String cursor = params.get("cursor");
            SearchResult result = engine.search(query, pageSize,
                    (cursor == null || cursor.isBlank()) ? null : cursor);
            sendJson(exchange, 200, searchResponseJson(result));
        } catch (SearchException e) {
            sendError(exchange, e.status(), e.code(), e.getMessage());
        } catch (Exception e) {
            sendError(exchange, 500, "INTERNAL_ERROR", String.valueOf(e));
        }
    }

    private void handleDocuments(HttpExchange exchange) throws IOException {
        String path = exchange.getRequestURI().getPath();
        try {
            if (path.equals("/documents") || path.equals("/documents/")) {
                switch (exchange.getRequestMethod()) {
                    case "POST" -> handleUpsert(exchange);
                    default -> sendError(exchange, 405, "METHOD_NOT_ALLOWED",
                            "method " + exchange.getRequestMethod() + " not allowed on " + path);
                }
            } else if (path.equals("/documents/bulk")) {
                if (!exchange.getRequestMethod().equals("POST")) {
                    sendError(exchange, 405, "METHOD_NOT_ALLOWED",
                            "method " + exchange.getRequestMethod() + " not allowed on " + path);
                    return;
                }
                handleBulkUpsert(exchange);
            } else if (path.startsWith("/documents/")) {
                if (!exchange.getRequestMethod().equals("DELETE")) {
                    sendError(exchange, 405, "METHOD_NOT_ALLOWED",
                            "method " + exchange.getRequestMethod() + " not allowed on " + path);
                    return;
                }
                String id = URLDecoder.decode(path.substring("/documents/".length()), StandardCharsets.UTF_8);
                handleDelete(exchange, id);
            } else {
                sendError(exchange, 404, "NOT_FOUND", "no route for " + path);
            }
        } catch (SearchException e) {
            sendError(exchange, e.status(), e.code(), e.getMessage());
        } catch (IllegalArgumentException e) {
            sendError(exchange, 400, "BAD_REQUEST", e.getMessage());
        } catch (Exception e) {
            sendError(exchange, 500, "INTERNAL_ERROR", String.valueOf(e));
        }
    }

    private void handleUpsert(HttpExchange exchange) throws IOException {
        Map<String, Object> body = Json.parseObject(readBody(exchange));
        Object id = body.get("id");
        if (!(id instanceof String s) || s.isBlank()) {
            throw new SearchException.BadRequest("field 'id' is required and must be a non-blank string");
        }
        Object text = body.get("text");
        if (text != null && !(text instanceof String)) {
            throw new SearchException.BadRequest("field 'text' must be a string");
        }
        int version = engine.upsert((String) id, (String) text);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);
        resp.put("id", id);
        resp.put("version", version);
        sendJson(exchange, 200, resp);
    }

    private void handleBulkUpsert(HttpExchange exchange) throws IOException {
        Map<String, Object> body = Json.parseObject(readBody(exchange));
        Object docs = body.get("documents");
        if (!(docs instanceof List<?> list)) {
            throw new SearchException.BadRequest("field 'documents' is required and must be an array");
        }
        Map<String, String> batch = new LinkedHashMap<>();
        for (Object item : list) {
            if (!(item instanceof Map<?, ?> m)) {
                throw new SearchException.BadRequest("each document must be an object with 'id' and 'text'");
            }
            Object id = m.get("id");
            Object text = m.get("text");
            if (!(id instanceof String s) || s.isBlank()) {
                throw new SearchException.BadRequest("each document requires a non-blank string 'id'");
            }
            if (text != null && !(text instanceof String)) {
                throw new SearchException.BadRequest("field 'text' must be a string");
            }
            batch.put((String) id, (String) text);
        }
        int version = engine.upsertAll(batch);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);
        resp.put("count", batch.size());
        resp.put("version", version);
        sendJson(exchange, 200, resp);
    }

    private void handleDelete(HttpExchange exchange, String id) throws IOException {
        boolean deleted = engine.delete(id);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("ok", true);
        resp.put("deleted", deleted);
        resp.put("version", engine.currentVersion());
        sendJson(exchange, 200, resp);
    }

    private void handleSnapshots(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }
        List<Map<String, Object>> versions = new ArrayList<>();
        for (int v : engine.retainedVersions()) {
            IndexSnapshot snap = engine.snapshotAt(v);
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("version", v);
            item.put("docCount", snap.docCount());
            item.put("createdAtMillis", snap.createdAtMillis());
            versions.add(item);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("currentVersion", engine.currentVersion());
        body.put("maxRetained", SearchEngine.MAX_RETAINED_SNAPSHOTS);
        body.put("snapshots", versions);
        sendJson(exchange, 200, body);
    }

    // ------------------------------------------------------------------
    // 响应组装
    // ------------------------------------------------------------------

    private Map<String, Object> searchResponseJson(SearchResult result) {
        List<Map<String, Object>> hits = new ArrayList<>();
        for (SearchHit hit : result.hits()) {
            Map<String, Object> h = new LinkedHashMap<>();
            h.put("docId", hit.docId());
            h.put("score", hit.score());
            h.put("text", result.snapshot().textOf(hit.docId()));
            hits.add(h);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("snapshotVersion", result.snapshotVersion());
        body.put("totalHits", result.totalHits());
        body.put("offset", result.offset());
        body.put("pageSize", result.pageSize());
        body.put("hits", hits);
        body.put("nextCursor", result.nextCursor());
        body.put("hasMore", result.hasMore());
        return body;
    }

    // ------------------------------------------------------------------
    // 工具
    // ------------------------------------------------------------------

    private boolean requireMethod(HttpExchange exchange, String method) throws IOException {
        if (!exchange.getRequestMethod().equals(method)) {
            sendError(exchange, 405, "METHOD_NOT_ALLOWED",
                    "method " + exchange.getRequestMethod() + " not allowed, expected " + method);
            return false;
        }
        return true;
    }

    private static Map<String, String> queryParams(HttpExchange exchange) {
        Map<String, String> params = new LinkedHashMap<>();
        String raw = exchange.getRequestURI().getRawQuery();
        if (raw == null || raw.isEmpty()) {
            return params;
        }
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String key = eq < 0 ? pair : pair.substring(0, eq);
            String value = eq < 0 ? "" : pair.substring(eq + 1);
            params.put(urlDecode(key), urlDecode(value));
        }
        return params;
    }

    private static String urlDecode(String s) {
        return URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private static String readBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private void sendJson(HttpExchange exchange, int status, Map<String, Object> body) throws IOException {
        byte[] payload = Json.stringify(body).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, payload.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(payload);
        }
    }

    private void sendError(HttpExchange exchange, int status, String code, String message) throws IOException {
        Map<String, Object> error = new LinkedHashMap<>();
        error.put("code", code);
        error.put("message", message);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", error);
        sendJson(exchange, status, body);
    }
}
