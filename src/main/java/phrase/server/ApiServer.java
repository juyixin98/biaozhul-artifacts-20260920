package phrase.server;

import phrase.core.Token;
import phrase.json.Json;
import phrase.search.DocMatch;
import phrase.search.Match;
import phrase.search.PhraseQuery;
import phrase.search.SearchResult;
import phrase.search.SearchService;

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
import java.util.concurrent.Executors;

/**
 * 基于 JDK 内置 com.sun.net.httpserver 的纯本地 JSON HTTP 服务，无第三方依赖。
 *
 * <ul>
 *   <li>POST /search —— 短语检索；</li>
 *   <li>POST /analyze —— 查看分析器分词与位置；</li>
 *   <li>GET  /corpus —— 查看合成语料；</li>
 *   <li>GET  /health —— 健康检查。</li>
 * </ul>
 */
public final class ApiServer implements AutoCloseable {

    private final HttpServer server;
    private final SearchService service;

    public ApiServer(SearchService service, int port) throws IOException {
        this.service = service;
        this.server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        this.server.createContext("/", this::dispatch);
        this.server.setExecutor(Executors.newFixedThreadPool(4));
    }

    public void start() {
        server.start();
    }

    /** 实际监听端口（构造时传 0 可由系统分配）。 */
    public int port() {
        return server.getAddress().getPort();
    }

    @Override
    public void close() {
        server.stop(0);
    }

    private void dispatch(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (path) {
                case "/health" -> {
                    requireGet(ex, method);
                    Map<String, Object> body = new LinkedHashMap<>();
                    body.put("status", "ok");
                    writeJson(ex, 200, body);
                }
                case "/corpus" -> {
                    requireGet(ex, method);
                    writeJson(ex, 200, corpusBody());
                }
                case "/analyze" -> {
                    requirePost(ex, method);
                    handleAnalyze(ex);
                }
                case "/search" -> {
                    requirePost(ex, method);
                    handleSearch(ex);
                }
                default -> writeError(ex, 404, "not found: " + path);
            }
        } catch (IllegalArgumentException e) {
            writeError(ex, 400, e.getMessage());
        } catch (Exception e) {
            writeError(ex, 500, "internal error: " + e);
        } finally {
            ex.close();
        }
    }

    private void requireGet(HttpExchange ex, String method) {
        if (!method.equals("GET")) {
            throw new IllegalArgumentException("method " + method + " not allowed, use GET");
        }
    }

    private void requirePost(HttpExchange ex, String method) {
        if (!method.equals("POST")) {
            throw new IllegalArgumentException("method " + method + " not allowed, use POST");
        }
    }

    private Map<String, Object> corpusBody() {
        Map<String, Object> root = new LinkedHashMap<>();
        List<Object> docs = new ArrayList<>();
        for (String id : service.index("standard").docIds()) {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("id", id);
            d.put("fields", new LinkedHashMap<>(service.index("standard").doc(id).fields()));
            docs.add(d);
        }
        root.put("documents", docs);
        return root;
    }

    private void handleAnalyze(HttpExchange ex) throws IOException {
        Map<?, ?> req = readJsonBody(ex);
        String analyzer = strOrDefault(req, "analyzer", "standard");
        String text = strOrDefault(req, "text", "");
        List<Token> tokens = service.analyze(analyzer, text);

        Map<String, Object> root = new LinkedHashMap<>();
        root.put("analyzer", analyzer);
        root.put("text", text);
        List<Object> arr = new ArrayList<>();
        for (Token t : tokens) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("term", t.term());
            m.put("position", t.position());
            arr.add(m);
        }
        root.put("tokens", arr);
        writeJson(ex, 200, root);
    }

    private void handleSearch(HttpExchange ex) throws IOException {
        Map<?, ?> req = readJsonBody(ex);
        String analyzer = strOrDefault(req, "analyzer", "standard");
        int slop = intOrDefault(req, "slop", 0);
        if (slop < 0 || slop > PhraseQuery.MAX_SLOP) {
            throw new IllegalArgumentException(
                    "slop must be between 0 and " + PhraseQuery.MAX_SLOP);
        }
        String field = req.containsKey("field") && req.get("field") != null
                ? String.valueOf(req.get("field")) : null;
        String query = req.containsKey("query") && req.get("query") != null
                ? String.valueOf(req.get("query")) : null;
        List<String> terms = null;
        Object termsObj = req.get("terms");
        if (termsObj instanceof List<?> list) {
            terms = new ArrayList<>();
            for (Object o : list) {
                terms.add(String.valueOf(o));
            }
            if (terms.isEmpty()) {
                throw new IllegalArgumentException("terms must not be an empty array");
            }
        }
        if ((query == null || query.isBlank()) && terms == null) {
            throw new IllegalArgumentException("request must contain 'query' or 'terms'");
        }

        SearchResult result = service.search(analyzer, query, terms, slop, field);
        writeJson(ex, 200, toJson(result));
    }

    static Map<String, Object> toJson(SearchResult r) {
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("analyzer", r.analyzer());
        root.put("slop", r.slop());
        root.put("field", r.field());
        root.put("queryTerms", r.queryTerms());
        root.put("totalHits", r.totalHits());
        root.put("truncated", r.truncated());
        List<Object> hits = new ArrayList<>();
        for (DocMatch dm : r.hits()) {
            Map<String, Object> h = new LinkedHashMap<>();
            h.put("docId", dm.docId());
            h.put("matchCount", dm.matchCount());
            h.put("truncated", dm.truncated());
            List<Object> ms = new ArrayList<>();
            for (Match m : dm.matches()) {
                Map<String, Object> mm = new LinkedHashMap<>();
                mm.put("positions", m.positions());
                mm.put("fields", m.fields());
                mm.put("start", m.start());
                mm.put("end", m.end());
                mm.put("crossField", m.crossField());
                ms.add(mm);
            }
            h.put("matches", ms);
            hits.add(h);
        }
        root.put("hits", hits);
        return root;
    }

    private Map<?, ?> readJsonBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        if (bytes.length == 0) {
            throw new IllegalArgumentException("request body must be a JSON object");
        }
        Object parsed;
        try {
            parsed = Json.parse(new String(bytes, StandardCharsets.UTF_8));
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException("invalid JSON body: " + e.getMessage());
        }
        if (!(parsed instanceof Map<?, ?> map)) {
            throw new IllegalArgumentException("request body must be a JSON object");
        }
        return map;
    }

    private static String strOrDefault(Map<?, ?> m, String key, String def) {
        Object v = m.get(key);
        return v == null ? def : String.valueOf(v);
    }

    private static int intOrDefault(Map<?, ?> m, String key, int def) {
        Object v = m.get(key);
        if (v == null) {
            return def;
        }
        if (v instanceof Number n) {
            return n.intValue();
        }
        try {
            return Integer.parseInt(String.valueOf(v));
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(key + " must be an integer");
        }
    }

    private void writeError(HttpExchange ex, int code, String message) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("error", true);
        m.put("status", code);
        m.put("message", message == null ? "" : message);
        writeJson(ex, code, m);
    }

    private void writeJson(HttpExchange ex, int code, Object body) throws IOException {
        byte[] data = Json.pretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, data.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(data);
        }
    }
}
