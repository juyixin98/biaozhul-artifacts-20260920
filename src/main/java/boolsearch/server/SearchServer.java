package boolsearch.server;

import boolsearch.Corpus;
import boolsearch.InvertedIndex;
import boolsearch.eval.Evaluator;
import boolsearch.json.Json;
import boolsearch.query.Node;
import boolsearch.query.ParseException;
import boolsearch.query.Parser;
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
import java.util.SortedSet;
import java.util.concurrent.Executors;

/**
 * JSON HTTP 服务（JDK 内置 HttpServer，无外部依赖）。
 *
 * 端点：
 *   GET    /health                -> {"status":"ok"}
 *   POST   /documents             {"id":1,"text":"apple banana"}   新增/覆盖文档
 *   DELETE /documents/{id}                                       删除文档
 *   GET    /documents             -> {"docIds":[...]}            当前文档全集
 *   POST   /corpus                {"docs":200,"seed":42}         重建合成语料（清空索引）
 *   POST   /query                 {"query":"apple AND NOT banana","optimize":true}
 *        -> 200 {"docIds":[...],"count":n,"optimize":true}
 *        -> 400 {"error":"...","position":k}                     解析错误（保留位置）
 */
public final class SearchServer {
    private final InvertedIndex index = new InvertedIndex();
    private final HttpServer server;

    public SearchServer(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/documents", this::handleDocuments);
        server.createContext("/documents/", this::handleDocumentById);
        server.createContext("/corpus", this::handleCorpus);
        server.createContext("/query", this::handleQuery);
        server.setExecutor(Executors.newFixedThreadPool(4));
    }

    public void start() { server.start(); }
    public void stop() { server.stop(0); }
    public int port() { return server.getAddress().getPort(); }

    /** 预载文档（启动时装载合成语料用）。 */
    public void addDoc(int id, String text) { index.addDocument(id, text); }

    // ---------- handlers ----------

    private void handleHealth(HttpExchange ex) throws IOException {
        if (!method(ex, "GET")) return;
        respond(ex, 200, Map.of("status", "ok", "docs", index.docCount()));
    }

    private void handleDocuments(HttpExchange ex) throws IOException {
        switch (ex.getRequestMethod()) {
            case "GET" -> {
                Map<String, Object> r = new LinkedHashMap<>();
                r.put("docIds", new ArrayList<>(index.universe()));
                r.put("count", index.docCount());
                respond(ex, 200, r);
            }
            case "POST" -> {
                Map<String, Object> body = bodyObject(ex);
                if (body == null) return;
                Object id = body.get("id"), text = body.get("text");
                if (!(id instanceof Number) || !(text instanceof String)) {
                    respond(ex, 400, Map.of("error", "需要字段 id（数字）与 text（字符串）", "position", -1));
                    return;
                }
                index.addDocument(((Number) id).intValue(), (String) text);
                respond(ex, 200, Map.of("ok", true, "id", ((Number) id).intValue(), "docs", index.docCount()));
            }
            default -> respond(ex, 405, Map.of("error", "方法不允许", "position", -1));
        }
    }

    private void handleDocumentById(HttpExchange ex) throws IOException {
        if (!method(ex, "DELETE")) return;
        String path = ex.getRequestURI().getPath();
        String idStr = path.substring("/documents/".length());
        final int id;
        try {
            id = Integer.parseInt(idStr);
        } catch (NumberFormatException e) {
            respond(ex, 400, Map.of("error", "无效的文档 id: " + idStr, "position", -1));
            return;
        }
        if (index.deleteDocument(id)) {
            respond(ex, 200, Map.of("ok", true, "deleted", id, "docs", index.docCount()));
        } else {
            respond(ex, 404, Map.of("error", "文档不存在: " + id, "position", -1));
        }
    }

    private void handleCorpus(HttpExchange ex) throws IOException {
        if (!method(ex, "POST")) return;
        Map<String, Object> body = bodyObject(ex);
        if (body == null) return;
        int docs = body.get("docs") instanceof Number n ? n.intValue() : 200;
        long seed = body.get("seed") instanceof Number n ? n.longValue() : 42L;
        for (Integer id : new ArrayList<>(index.universe())) index.deleteDocument(id);
        Corpus.generate(docs, seed).forEach(index::addDocument);
        respond(ex, 200, Map.of("ok", true, "docs", index.docCount(), "seed", seed));
    }

    private void handleQuery(HttpExchange ex) throws IOException {
        if (!method(ex, "POST")) return;
        Map<String, Object> body = bodyObject(ex);
        if (body == null) return;
        Object q = body.get("query");
        if (!(q instanceof String query)) {
            respond(ex, 400, Map.of("error", "需要字段 query（字符串）", "position", -1));
            return;
        }
        boolean optimize = !(body.get("optimize") instanceof Boolean b) || b;
        final Node ast;
        try {
            ast = Parser.parse(query);
        } catch (ParseException e) {
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("error", e.getMessage());
            r.put("position", e.position());
            r.put("query", query);
            respond(ex, 400, r);
            return;
        }
        List<String> trace = new ArrayList<>();
        Evaluator ev = new Evaluator(index, optimize);
        ev.setTrace(trace);
        SortedSet<Integer> result = ev.eval(ast);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("docIds", new ArrayList<>(result));
        r.put("count", result.size());
        r.put("optimize", optimize);
        r.put("trace", trace);
        respond(ex, 200, r);
    }

    // ---------- helpers ----------

    private boolean method(HttpExchange ex, String m) throws IOException {
        if (!ex.getRequestMethod().equals(m)) {
            respond(ex, 405, Map.of("error", "方法不允许，期望 " + m, "position", -1));
            return false;
        }
        return true;
    }

    private Map<String, Object> bodyObject(HttpExchange ex) throws IOException {
        String body = new String(readAll(ex), StandardCharsets.UTF_8);
        try {
            Object v = Json.parse(body);
            if (v instanceof Map<?, ?> m) {
                @SuppressWarnings("unchecked")
                Map<String, Object> cast = (Map<String, Object>) m;
                return cast;
            }
            respond(ex, 400, Map.of("error", "请求体必须是 JSON 对象", "position", -1));
        } catch (Json.JsonException e) {
            respond(ex, 400, Map.of("error", "JSON 解析失败: " + e.getMessage(), "position", e.position()));
        }
        return null;
    }

    private static byte[] readAll(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            return in.readAllBytes();
        }
    }

    private void respond(HttpExchange ex, int status, Map<String, Object> body) throws IOException {
        byte[] bytes = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }
}
