package com.example.edcand;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * 本地 JSON HTTP 服务（纯后端，无前端，无外部服务调用）。
 *
 * <p>基于 JDK 内置 {@link HttpServer}，启动时加载 {@link CorpusGenerator} 合成语料。
 *
 * <h3>接口</h3>
 * <ul>
 *   <li>{@code GET  /health} → {@code {"status":"ok"}}</li>
 *   <li>{@code GET  /stats}  → 词项数、q、规范化形式</li>
 *   <li>{@code POST /distance}  body {@code {"a":"...","b":"...","normalization":"NFC"}}
 *       → {@code {"distance":n,"codePointLengths":[la,lb],"normalization":"NFC"}}</li>
 *   <li>{@code POST /search} body
 *       {@code {"query":"...","threshold":2,"normalization":"NFC"}}
 *       → 匹配结果与筛选统计（候选数、精算次数等）</li>
 * </ul>
 * 所有请求/响应均为 {@code application/json; charset=utf-8}。
 */
public final class SearchServer {

    private final CandidateIndex index;
    private final List<String> corpus;
    private final TextNormalization defaultNorm;
    private HttpServer server;

    public SearchServer(List<String> corpus, TextNormalization norm) {
        this.corpus = List.copyOf(corpus);
        this.defaultNorm = norm == null ? TextNormalization.NFC : norm;
        this.index = CandidateIndex.build(this.corpus, this.defaultNorm);
    }

    /** 用默认合成语料与 NFC 启动。 */
    public static SearchServer withDefaultCorpus() {
        return new SearchServer(CorpusGenerator.defaultCorpus(), TextNormalization.NFC);
    }

    /**
     * 启动服务。
     *
     * @param port 0 表示由操作系统分配端口（测试用），返回实际端口见 {@link #getPort()}
     */
    public void start(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/stats", this::handleStats);
        server.createContext("/distance", this::handleDistance);
        server.createContext("/search", this::handleSearch);
        server.setExecutor(Executors.newFixedThreadPool(4));
        server.start();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
    }

    public int getPort() {
        return server == null ? -1 : server.getAddress().getPort();
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        sendJson(ex, 200, Map.of("status", "ok"));
    }

    private void handleStats(HttpExchange ex) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("totalTerms", index.size());
        body.put("q", CandidateIndex.Q);
        body.put("normalization", defaultNorm.name());
        body.put("unit", "unicode_codepoint");
        sendJson(ex, 200, body);
    }

    private void handleDistance(HttpExchange ex) throws IOException {
        try {
            Map<String, Object> req = readJsonBody(ex);
            String a = requireString(req, "a");
            String b = requireString(req, "b");
            TextNormalization norm = parseNorm(req.get("normalization"));
            String na = norm.apply(a);
            String nb = norm.apply(b);
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("distance", Levenshtein.distance(na, nb));
            body.put("codePointLengths", List.of(CodePoints.length(na), CodePoints.length(nb)));
            body.put("normalizedA", na);
            body.put("normalizedB", nb);
            body.put("normalization", norm.name());
            sendJson(ex, 200, body);
        } catch (IllegalArgumentException e) {
            sendError(ex, 400, e.getMessage());
        }
    }

    private void handleSearch(HttpExchange ex) throws IOException {
        try {
            Map<String, Object> req = readJsonBody(ex);
            String query = requireString(req, "query");
            int threshold = requireInt(req, "threshold");
            if (threshold < 0) {
                throw new IllegalArgumentException("threshold must be >= 0");
            }
            // 服务索引按 defaultNorm 构建；请求指定其他形式时为该请求重建视图。
            TextNormalization norm = parseNorm(req.get("normalization"));
            CandidateIndex active = index;
            if (norm != defaultNorm) {
                active = CandidateIndex.build(corpus, norm);
            }
            SearchOutcome out = active.search(query, threshold);

            List<Map<String, Object>> matches = new java.util.ArrayList<>();
            for (SearchOutcome.Match m : out.matches()) {
                Map<String, Object> mm = new LinkedHashMap<>();
                mm.put("term", m.term());
                if (m.original() != null) {
                    mm.put("original", m.original());
                }
                mm.put("distance", m.distance());
                matches.add(mm);
            }
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("query", norm.apply(query));
            body.put("threshold", threshold);
            body.put("normalization", norm.name());
            body.put("matches", matches);
            body.put("totalTerms", out.totalTerms());
            body.put("passedLengthGate", out.passedLengthGate());
            body.put("candidates", out.candidates());
            body.put("exactLevenshteinCalls", out.scannedExact());
            sendJson(ex, 200, body);
        } catch (IllegalArgumentException e) {
            sendError(ex, 400, e.getMessage());
        }
    }

    // ---------- HTTP 辅助 ----------

    private static Map<String, Object> readJsonBody(HttpExchange ex) throws IOException {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            throw new IllegalArgumentException("POST required");
        }
        byte[] raw = ex.getRequestBody().readAllBytes();
        if (raw.length == 0) {
            throw new IllegalArgumentException("empty request body");
        }
        Object parsed = Json.parse(new String(raw, StandardCharsets.UTF_8));
        if (!(parsed instanceof Map)) {
            throw new IllegalArgumentException("request body must be a JSON object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) parsed;
        return m;
    }

    private static String requireString(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (!(v instanceof String s)) {
            throw new IllegalArgumentException("missing or non-string field: " + key);
        }
        return s;
    }

    private static int requireInt(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v instanceof Number n) {
            int i = n.intValue();
            if (i != n.doubleValue()) {
                throw new IllegalArgumentException("field must be integer: " + key);
            }
            return i;
        }
        throw new IllegalArgumentException("missing or non-integer field: " + key);
    }

    private static TextNormalization parseNorm(Object v) {
        if (v == null) {
            return TextNormalization.NFC;
        }
        if (!(v instanceof String s)) {
            throw new IllegalArgumentException("normalization must be a string");
        }
        try {
            return TextNormalization.parse(s);
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException(
                    "normalization must be one of NONE,NFC,NFD,NFKC,NFKD");
        }
    }

    private static void sendError(HttpExchange ex, int code, String message) throws IOException {
        sendJson(ex, code, Map.of("error", message == null ? "bad request" : message));
    }

    private static void sendJson(HttpExchange ex, int code, Object body) throws IOException {
        byte[] data = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, data.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(data);
        }
    }
}
