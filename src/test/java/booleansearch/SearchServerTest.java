package booleansearch;

import booleansearch.corpus.SyntheticCorpus;
import booleansearch.index.InvertedIndex;
import booleansearch.json.Json;
import booleansearch.server.SearchHttpServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.Map;

import static booleansearch.TestFramework.assertEquals;
import static booleansearch.TestFramework.assertTrue;

/**
 * 端到端 HTTP 测试：真实启动 {@link SearchHttpServer}，用 JDK HttpClient 发请求，
 * 覆盖检索、错误位置透传、文档增删、404/405 等。
 */
public final class SearchServerTest {

    private static HttpClient client;

    public static void run() throws Exception {
        TestFramework.reset();
        TestFramework.section("SearchServerTest: 启动与基本端点");

        InvertedIndex index = new InvertedIndex();
        SyntheticCorpus.loadInto(index);
        SearchHttpServer server = new SearchHttpServer(index, "127.0.0.1", 0);
        server.start();
        client = HttpClient.newHttpClient();
        try {
            String base = "http://127.0.0.1:" + server.getPort();

            HttpResponse<String> health = get(base + "/health");
            assertEquals(200, health.statusCode(), "GET /health 200");
            assertTrue(health.body().contains("\"ok\""), "健康检查返回 ok");

            HttpResponse<String> stats = get(base + "/stats");
            assertEquals(200, stats.statusCode(), "GET /stats 200");
            Map<?, ?> statsBody = (Map<?, ?>) Json.parse(stats.body());
            assertEquals(12.0, ((Number) statsBody.get("documents")).doubleValue(), "语料 12 篇文档");
            assertEquals(0.0, ((Number) statsBody.get("deletedDocuments")).doubleValue(), "初始无删除");

            HttpResponse<String> docs = get(base + "/documents");
            assertEquals(200, docs.statusCode(), "GET /documents 200");
            assertTrue(docs.body().contains("猫与咖啡"), "文档列表包含中文标题");

            TestFramework.section("SearchServerTest: /search 成功与错误位置");

            HttpResponse<String> ok = post(base + "/search",
                    "{\"query\": \"zebra AND kayak AND glacier\"}");
            assertEquals(200, ok.statusCode(), "合法查询 200");
            Map<?, ?> okBody = (Map<?, ?>) Json.parse(ok.body());
            assertEquals(Boolean.TRUE, okBody.get("resultsIdentical"), "两种计划结果一致");
            assertEquals(1.0, ((Number) okBody.get("hitCount")).doubleValue(),
                    "zebra AND kayak AND glacier 命中 1 篇（文档11，三个稀有词）");
            assertTrue(okBody.get("normalizedAst").toString().contains("AND"), "返回规范化 AST");
            Map<?, ?> cost = (Map<?, ?>) okBody.get("costComparison");
            assertTrue(((Number) cost.get("probeDeltaOptimizedMinusNaive")).longValue() <= 0,
                    "优化探测增量 ≤ 0");

            // 语法错误：位置 8（"(cat" 长度为 4，前 4 字符是 "(cat"，EOF 在位置 4）— 用缺右括号
            HttpResponse<String> badQuery = post(base + "/search",
                    "{\"query\": \"(cat AND dog\"}");
            assertEquals(400, badQuery.statusCode(), "语法错误返回 400");
            Map<?, ?> badBody = (Map<?, ?>) Json.parse(badQuery.body());
            assertEquals(12L, ((Number) badBody.get("position")).longValue(),
                    "缺右括号的错误位置透传到 JSON");
            assertTrue(badBody.get("error").toString().contains("右括号"), "错误信息可读");

            // 空查询
            HttpResponse<String> emptyQuery = post(base + "/search", "{\"query\": \"\"}");
            assertEquals(400, emptyQuery.statusCode(), "空查询 400");

            // 缺少字段
            HttpResponse<String> missing = post(base + "/search", "{}");
            assertEquals(400, missing.statusCode(), "缺少 query 字段 400");

            // 请求体 JSON 语法错误（位置透传）
            HttpResponse<String> badJson = post(base + "/search", "{not-json");
            assertEquals(400, badJson.statusCode(), "非法 JSON 400");
            Map<?, ?> badJsonBody = (Map<?, ?>) Json.parse(badJson.body());
            assertTrue(((Number) badJsonBody.get("position")).longValue() >= 0,
                    "JSON 错误同样带位置");

            // 非对象 JSON
            HttpResponse<String> arrJson = post(base + "/search", "[1,2,3]");
            assertEquals(400, arrJson.statusCode(), "数组体 400");

            // 方法不允许
            assertEquals(405, get(base + "/search").statusCode(), "GET /search -> 405");

            TestFramework.section("SearchServerTest: 文档增删与 NOT 全集变化");

            HttpResponse<String> added = post(base + "/documents",
                    "{\"title\": \"新增\", \"text\": \"brandnewterm\"}");
            assertEquals(201, added.statusCode(), "POST /documents 201");
            long newId = ((Number) ((Map<?, ?>) Json.parse(added.body())).get("id")).longValue();
            assertEquals(13L, newId, "新文档 id 接续为 13");

            String findNew = "{\"query\": \"brandnewterm\"}";
            assertEquals(1.0, ((Number) ((Map<?, ?>) Json.parse(post(base + "/search", findNew).body()))
                    .get("hitCount")).doubleValue(), "新文档可被检索到");

            // 删除文档 11（唯一含 zebra 的文档），zebra 变未知结果空，NOT 全集缩小
            HttpResponse<String> deleted = delete(base + "/documents/11");
            assertEquals(200, deleted.statusCode(), "DELETE 文档 11 成功");
            HttpResponse<String> deletedAgain = delete(base + "/documents/11");
            assertEquals(404, deletedAgain.statusCode(), "重复删除 404");

            Map<?, ?> zebraAfter = (Map<?, ?>) Json.parse(
                    post(base + "/search", "{\"query\": \"zebra\"}").body());
            assertEquals(0.0, ((Number) zebraAfter.get("hitCount")).doubleValue(),
                    "删除后 zebra 命中 0");

            Map<?, ?> statsAfter = (Map<?, ?>) Json.parse(get(base + "/stats").body());
            assertEquals(12.0, ((Number) statsAfter.get("documents")).doubleValue(),
                    "加入 1 篇又删除 1 篇后存活 12 篇");
            assertEquals(1.0, ((Number) statsAfter.get("deletedDocuments")).doubleValue(),
                    "已删除 1 篇");
            assertEquals(13.0, ((Number) statsAfter.get("totalDocumentsEver")).doubleValue(),
                    "历史全集含新增与删除，共 13 篇");

            // 非法 id
            HttpResponse<String> badId = delete(base + "/documents/abc");
            assertEquals(400, badId.statusCode(), "非法 id 400");

            // 未知路由 404（HttpServer 默认 404）
            assertEquals(404, get(base + "/nope").statusCode(), "未知路径 404");
        } finally {
            server.stop();
        }

        boolean ok = TestFramework.finish();
        if (!ok) {
            throw new AssertionError("SearchServerTest 存在失败");
        }
    }

    private static HttpResponse<String> get(String url) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(url)).GET().build();
        return client.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> post(String url, String body) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        return client.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> delete(String url) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create(url)).DELETE().build();
        return client.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }
}
