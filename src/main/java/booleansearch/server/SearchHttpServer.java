package booleansearch.server;

import booleansearch.index.InvertedIndex;
import booleansearch.json.Json;
import booleansearch.json.JsonParseException;
import booleansearch.model.Document;
import booleansearch.query.QueryParseException;
import booleansearch.search.EvalResult;
import booleansearch.search.SearchEngine;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 本地 JSON HTTP 服务，仅使用 JDK 自带的 {@link HttpServer}，不依赖任何外部服务。
 *
 * <p>路由：
 * <pre>
 *  GET  /health                        存活检查
 *  GET  /stats                         索引统计
 *  GET  /documents                     文档列表
 *  POST /search        {"query": ...}  布尔检索（同时返回朴素/优化两版计划）
 *  POST /documents     {"title": ..., "text": ...}   添加文档
 *  DELETE /documents/{id}             删除文档（软删除，NOT 全集随之缩小）
 * </pre>
 * 错误统一返回 {"error": ..., "position": ...}，位置为查询串/JSON 中的 0 基偏移。
 */
public final class SearchHttpServer {

    private final HttpServer server;
    private final InvertedIndex index;
    private final SearchEngine engine;

    public SearchHttpServer(InvertedIndex index, String host, int port) throws IOException {
        this.index = index;
        this.engine = new SearchEngine(index);
        this.server = HttpServer.create(new InetSocketAddress(host, port), 0);
        this.server.createContext("/health", this::handleHealth);
        this.server.createContext("/stats", this::handleStats);
        this.server.createContext("/documents", this::handleDocuments);
        this.server.createContext("/search", this::handleSearch);
        this.server.setExecutor(java.util.concurrent.Executors.newFixedThreadPool(8));
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(1);
    }

    public int getPort() {
        return server.getAddress().getPort();
    }

    // ---------- 路由 ----------

    private void handleHealth(HttpExchange ex) throws IOException {
        sendJson(ex, 200, Map.of("status", "ok"));
    }

    private void handleStats(HttpExchange ex) throws IOException {
        sendJson(ex, 200, index.stats());
    }

    private void handleDocuments(HttpExchange ex) throws IOException {
        try {
            if (ex.getRequestMethod().equals("GET")) {
                List<Object> docs = index.listDocuments().stream()
                        .map(d -> {
                            Map<String, Object> m = new LinkedHashMap<>();
                            m.put("id", d.id());
                            m.put("title", d.title());
                            m.put("text", d.text());
                            return m;
                        })
                        .map(Object.class::cast)
                        .toList();
                sendJson(ex, 200, Map.of("documents", docs, "count", docs.size()));
                return;
            }
            if (ex.getRequestMethod().equals("POST")) {
                Map<String, Object> body = Json.asObject(Json.parse(readBody(ex)));
                String title = String.valueOf(body.getOrDefault("title", ""));
                String text = Json.getString(body, "text");
                int id = index.addDocument(title, text);
                Map<String, Object> resp = new LinkedHashMap<>();
                resp.put("added", true);
                resp.put("id", id);
                sendJson(ex, 201, resp);
                return;
            }
            if (ex.getRequestMethod().equals("DELETE")) {
                String tail = ex.getRequestURI().getPath().substring("/documents".length());
                String idPart = tail.startsWith("/") ? tail.substring(1) : tail;
                int id;
                try {
                    id = Integer.parseInt(idPart);
                } catch (NumberFormatException e) {
                    sendError(ex, 400, "文档 id 必须是整数，实际为 \"" + idPart + "\"", -1);
                    return;
                }
                boolean removed = index.delete(id);
                if (!removed) {
                    sendError(ex, 404, "文档不存在或已被删除: " + id, -1);
                    return;
                }
                sendJson(ex, 200, Map.of("deleted", true, "id", id));
                return;
            }
            sendError(ex, 405, "不支持的方法 " + ex.getRequestMethod(), -1);
        } catch (JsonParseException e) {
            sendError(ex, 400, "请求体 JSON 错误: " + e.getMessage(), e.position());
        } catch (Json.BadRequestException e) {
            sendError(ex, 400, e.getMessage(), -1);
        }
    }

    private void handleSearch(HttpExchange ex) throws IOException {
        if (!ex.getRequestMethod().equals("POST")) {
            sendError(ex, 405, "/search 只支持 POST", -1);
            return;
        }
        try {
            Map<String, Object> body = Json.asObject(Json.parse(readBody(ex)));
            String query = Json.getString(body, "query");
            SearchEngine.SearchOutcome outcome = engine.search(query);
            sendJson(ex, 200, renderOutcome(outcome));
        } catch (JsonParseException e) {
            sendError(ex, 400, "请求体 JSON 错误: " + e.getMessage(), e.position());
        } catch (Json.BadRequestException e) {
            sendError(ex, 400, e.getMessage(), -1);
        } catch (QueryParseException e) {
            sendError(ex, 400, "查询语法错误: " + e.getMessage(), e.position());
        }
    }

    // ---------- 响应渲染 ----------

    private static Map<String, Object> renderOutcome(SearchEngine.SearchOutcome o) {
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("query", o.query());
        root.put("normalizedAst", o.normalizedAst());
        root.put("universeSize", o.universeSize());
        root.put("hitCount", o.hits().size());
        root.put("hits", o.hits());
        root.put("resultsIdentical", o.resultsIdentical());
        root.put("naivePlan", renderEval(o.naive()));
        root.put("optimizedPlan", renderEval(o.optimized()));
        Map<String, Object> cost = new LinkedHashMap<>();
        cost.put("naiveMembershipProbes", o.naive().membershipProbes());
        cost.put("optimizedMembershipProbes", o.optimized().membershipProbes());
        cost.put("probeDeltaOptimizedMinusNaive", o.probeDelta());
        cost.put("naivePostingReads", o.naive().postReads());
        cost.put("optimizedPostingReads", o.optimized().postReads());
        root.put("costComparison", cost);
        return root;
    }

    private static Map<String, Object> renderEval(EvalResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("docIds", r.docIds());
        m.put("hitCount", r.docIds().size());
        m.put("postingReads", r.postReads());
        m.put("membershipProbes", r.membershipProbes());
        m.put("trace", r.trace());
        return m;
    }

    // ---------- HTTP 基础 ----------

    private static String readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private static void sendJson(HttpExchange ex, int status, Object payload) throws IOException {
        byte[] data = Json.pretty(payload).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(data);
        }
    }

    private static void sendError(HttpExchange ex, int status, String message, int position)
            throws IOException {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("error", message);
        err.put("position", position);
        sendJson(ex, status, err);
    }
}
