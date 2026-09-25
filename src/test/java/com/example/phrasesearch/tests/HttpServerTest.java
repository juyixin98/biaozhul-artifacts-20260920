package com.example.phrasesearch.tests;

import com.example.phrasesearch.analyze.StandardAnalyzer;
import com.example.phrasesearch.corpus.SyntheticCorpus;
import com.example.phrasesearch.index.Index;
import com.example.phrasesearch.json.Json;
import com.example.phrasesearch.server.PhraseHttpServer;
import com.example.phrasesearch.service.PhraseService;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

/**
 * HTTP 服务端到端测试：真实启动 JDK HttpServer（随机端口），
 * 用 JDK HttpClient 发 GET/POST，校验状态码与 JSON 响应体。
 */
final class HttpServerTest {

    private HttpServerTest() {
    }

    private static HttpClient client;
    private static PhraseHttpServer server;
    private static String base;

    static void run() {
        try {
            setUp();
            health();
            getSearchExactPhrase();
            getSearchWithSlopAndRepeatedTerm();
            postSearchJson();
            fieldScopedExcludesCrossField();
            analyzeEndpoint();
            docsEndpointShowsPositions();
            configEndpointStatesStopwordPolicy();
            errors();
        } catch (Exception e) {
            throw new RuntimeException(e);
        } finally {
            if (server != null) {
                server.stop();
            }
        }
    }

    private static void setUp() throws Exception {
        Index index = new Index(new StandardAnalyzer());
        SyntheticCorpus.documents().forEach(index::addDocument);
        PhraseService service = new PhraseService(index);
        server = new PhraseHttpServer(service);
        server.start(0);
        base = "http://127.0.0.1:" + server.getPort();
        client = HttpClient.newHttpClient();
    }

    private static HttpResponse<String> get(String path) throws Exception {
        return client.send(HttpRequest.newBuilder(URI.create(base + path)).GET().build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> postJson(String path, String body) throws Exception {
        return client.send(HttpRequest.newBuilder(URI.create(base + path))
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                        .build(),
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> json(HttpResponse<String> r) {
        return (Map<String, Object>) Json.parse(r.body());
    }

    private static void health() throws Exception {
        HttpResponse<String> r = get("/health");
        Assert.equals("GET /health 状态码 200", 200, r.statusCode());
        Assert.equals("GET /health status=ok", "ok", json(r).get("status"));
    }

    private static void getSearchExactPhrase() throws Exception {
        HttpResponse<String> r = get("/search?q=" + java.net.URLEncoder.encode(
                "quick brown fox", StandardCharsets.UTF_8) + "&slop=0");
        Assert.equals("精确短语搜索 200", 200, r.statusCode());
        Map<String, Object> body = json(r);
        Assert.equals("精确短语: 1 篇文档命中", 1L, body.get("totalDocsMatched"));
        Assert.equals("精确短语: 2 次出现", 2L, body.get("totalOccurrences"));
        List<?> docs = (List<?>) body.get("documents");
        Map<?, ?> doc = (Map<?, ?>) docs.get(0);
        Assert.equals("命中文档 doc1", "doc1", doc.get("docId"));
    }

    private static void getSearchWithSlopAndRepeatedTerm() throws Exception {
        HttpResponse<String> r0 = get("/search?q=" + java.net.URLEncoder.encode("that that",
                StandardCharsets.UTF_8) + "&slop=0");
        Assert.equals("that that slop0: doc3 出现 2 次",
                2L, ((Map<?, ?>) ((List<?>) json(r0).get("documents")).get(0)).get("occurrences"));

        HttpResponse<String> r5 = get("/search?q=" + java.net.URLEncoder.encode("that that",
                StandardCharsets.UTF_8) + "&slop=5");
        Map<?, ?> b5 = json(r5);
        Assert.equals("that that slop5: 8 次出现", 8L, b5.get("totalOccurrences"));

        // 校验实际位置被返回且每次命中位置互不相同
        Map<?, ?> doc = (Map<?, ?>) ((List<?>) b5.get("documents")).get(0);
        List<?> matches = (List<?>) doc.get("matches");
        for (Object o : matches) {
            List<?> positions = (List<?>) ((Map<?, ?>) o).get("positions");
            long g0 = ((Number) ((Map<?, ?>) positions.get(0)).get("globalPosition")).longValue();
            long g1 = ((Number) ((Map<?, ?>) positions.get(1)).get("globalPosition")).longValue();
            Assert.isTrue("HTTP: 重复词两个 globalPosition 不同", g0 != g1);
            Assert.isTrue("HTTP: 重复词位置递增", g0 < g1);
        }
    }

    private static void postSearchJson() throws Exception {
        HttpResponse<String> r = postJson("/search",
                "{\"query\":\"phrase search\",\"slop\":2}");
        Assert.equals("POST /search 200", 200, r.statusCode());
        Map<?, ?> body = json(r);
        Assert.equals("POST 跨字段: 2 次出现（含跨字段边界命中）",
                2L, body.get("totalOccurrences"));
        List<?> docs = (List<?>) body.get("documents");
        Map<?, ?> doc = (Map<?, ?>) docs.get(0);
        List<?> matches = (List<?>) doc.get("matches");
        boolean hasCross = matches.stream().anyMatch(m ->
                Boolean.TRUE.equals(((Map<?, ?>) m).get("crossField")));
        Assert.isTrue("POST 结果含 crossField=true 的命中", hasCross);
    }

    private static void fieldScopedExcludesCrossField() throws Exception {
        HttpResponse<String> r = postJson("/search",
                "{\"query\":\"phrase search\",\"slop\":2,\"field\":\"body\"}");
        Assert.equals("body 限定搜索 200", 200, r.statusCode());
        Assert.equals("body 限定: 0 篇文档命中", 0L, json(r).get("totalDocsMatched"));
    }

    private static void analyzeEndpoint() throws Exception {
        HttpResponse<String> r = postJson("/analyze", "{\"text\":\"The Quick, Fox!\"}");
        Map<?, ?> body = json(r);
        List<?> tokens = (List<?>) body.get("tokens");
        Assert.equals("/analyze token 数（停用词保留）", 3, tokens.size());
        Assert.equals("/analyze positionCount", 3L, body.get("positionCount"));
        Assert.equals("/analyze 首词", "the",
                ((Map<?, ?>) tokens.get(0)).get("term"));
    }

    private static void docsEndpointShowsPositions() throws Exception {
        HttpResponse<String> r = get("/docs");
        Map<?, ?> body = json(r);
        Assert.equals("/docs docCount=8", 8L, body.get("docCount"));
        List<?> docs = (List<?>) body.get("documents");
        Map<?, ?> doc3 = (Map<?, ?>) docs.get(2);
        List<?> tokens = (List<?>) doc3.get("tokens");
        // 前两个 token 即 title 的 that/that，全局位置 0/1
        Assert.equals("/docs doc3 首词 that", "that",
                ((Map<?, ?>) tokens.get(0)).get("term"));
        Assert.equals("/docs doc3 首词全局位置 0", 0L,
                ((Map<?, ?>) tokens.get(0)).get("globalPosition"));
    }

    private static void configEndpointStatesStopwordPolicy() throws Exception {
        HttpResponse<String> r = get("/config");
        Map<?, ?> body = json(r);
        Assert.equals("/config 停用词策略",
                "kept_stopwords_occupy_positions", body.get("stopwordMode"));
        Assert.equals("/config 分析器", "standard", body.get("analyzer"));
    }

    private static void errors() throws Exception {
        HttpResponse<String> missing = postJson("/search", "{\"slop\":0}");
        Assert.equals("缺 query 参数 -> 400", 400, missing.statusCode());

        HttpResponse<String> badSlop = get("/search?q=fox&slop=-1");
        Assert.equals("负 slop -> 400", 400, badSlop.statusCode());

        HttpResponse<String> badField = get("/search?q=fox&field=nope");
        Assert.equals("未知字段 -> 400", 400, badField.statusCode());

        HttpResponse<String> badJson = postJson("/search", "{not json");
        Assert.equals("非法 JSON -> 400", 400, badJson.statusCode());
    }
}
