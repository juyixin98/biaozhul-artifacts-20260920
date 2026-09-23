package com.example.intervalindex.http;

import com.example.intervalindex.testsupport.Asserts;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

/**
 * 启动真实 {@link IntervalServer}（随机端口）进行 HTTP 端到端测试，
 * 客户端使用 JDK 内置 java.net.http.HttpClient，无第三方依赖。
 */
final class HttpApiTest {

    private static HttpClient http;
    private static String base;

    static void run() throws Exception {
        IntervalServer server = new IntervalServer(0);
        server.start();
        base = "http://localhost:" + server.getPort();
        http = HttpClient.newHttpClient();
        try {
            testHealth();
            testInsertValidation();
            testInsertNestedAdjacentDuplicate();
            testOverlapApi();
            testCountApi();
            testDeleteApi();
            testAllAndUnknownRoutes();
        } finally {
            server.stop();
        }
        System.out.println("HttpApiTest: all tests passed");
    }

    static void testHealth() throws Exception {
        HttpResponse<String> r = get("/health");
        Asserts.assertEquals(200L, r.statusCode(), "health status");
        Map<?, ?> body = decode(r);
        Asserts.assertEquals("ok", body.get("status"), "health body");
    }

    static void testInsertValidation() throws Exception {
        Asserts.assertEquals(400L, post("/intervals", "{\"start\":5,\"end\":5}").statusCode(),
                "empty interval rejected with 400");
        Asserts.assertEquals(400L, post("/intervals", "{\"start\":9,\"end\":3}").statusCode(),
                "reversed interval rejected with 400");
        Asserts.assertEquals(400L, post("/intervals", "{\"start\":1}").statusCode(),
                "missing field rejected with 400");
        Asserts.assertEquals(400L, post("/intervals", "not json").statusCode(),
                "malformed JSON rejected with 400");
    }

    static void testInsertNestedAdjacentDuplicate() throws Exception {
        // 嵌套
        postOk("{\"start\":0,\"end\":100}");
        postOk("{\"start\":10,\"end\":90}");
        postOk("{\"start\":40,\"end\":60}");
        // 相邻
        postOk("{\"start\":-10,\"end\":0}");
        postOk("{\"start\":100,\"end\":110}");
        // 重复
        long d1 = postOk("{\"start\":200,\"end\":300}");
        long d2 = postOk("{\"start\":200,\"end\":300}");
        Asserts.assertTrue(d1 != d2, "duplicate intervals get distinct ids");
    }

    static void testOverlapApi() throws Exception {
        HttpResponse<String> r = get("/intervals?start=45&end=46");
        Asserts.assertEquals(200L, r.statusCode(), "overlap status");
        Map<?, ?> body = decode(r);
        Asserts.assertEquals(3L, ((Number) body.get("count")).longValue(),
                "point query in innermost hits 3 nested");

        Asserts.assertEquals(0L, ((Number) decode(get("/intervals?start=-20&end=-10")).get("count")).longValue(),
                "touch at [-10,0) start: 0 hits");
        Asserts.assertEquals(0L, ((Number) decode(get("/intervals?start=110&end=120")).get("count")).longValue(),
                "touch at [100,110) end: 0 hits");
        Asserts.assertEquals(1L, ((Number) decode(get("/intervals?start=0&end=10")).get("count")).longValue(),
                "[0,10) overlaps only [0,100), not [-10,0)");
        Asserts.assertEquals(400L, get("/intervals?start=5&end=5").statusCode(),
                "empty query interval rejected");
        Asserts.assertEquals(400L, get("/intervals?start=1").statusCode(),
                "missing end param rejected");
    }

    static void testCountApi() throws Exception {
        Asserts.assertEquals(3L, countAt(50), "coverage depth 50 = 3");
        Asserts.assertEquals(2L, countAt(85), "coverage depth 85 = 2");
        Asserts.assertEquals(1L, countAt(-5), "coverage depth -5 = 1");
        // 100 是 outer 的开右端，但又是 [100,110) 的闭起点，故恰好被 1 个区间覆盖
        Asserts.assertEquals(1L, countAt(100), "point 100 covered only by [100,110)");
        Asserts.assertEquals(400L, get("/intervals/count").statusCode(), "missing at param rejected");
    }

    static void testDeleteApi() throws Exception {
        // 找出一个 id 删除
        Map<?, ?> all = decode(get("/intervals/all"));
        List<?> list = (List<?>) all.get("intervals");
        int before = list.size();
        Map<?, ?> first = (Map<?, ?>) list.get(0);
        long id = ((Number) first.get("id")).longValue();

        HttpResponse<String> del = delete("/intervals/" + id);
        Asserts.assertEquals(200L, del.statusCode(), "delete status");
        Asserts.assertEquals(true, ((Map<?, ?>) decode(del)).get("deleted"), "delete flag");

        Map<?, ?> after = decode(get("/intervals/all"));
        Asserts.assertEquals(before - 1L, ((Number) after.get("count")).longValue(), "size decreased");

        Asserts.assertEquals(404L, delete("/intervals/" + id).statusCode(), "second delete 404");
        Asserts.assertEquals(404L, delete("/intervals/99999999").statusCode(), "unknown id 404");
        Asserts.assertEquals(400L, delete("/intervals/abc").statusCode(), "non-numeric id 400");
    }

    static void testAllAndUnknownRoutes() throws Exception {
        HttpResponse<String> r = get("/intervals/all");
        Asserts.assertEquals(200L, r.statusCode(), "all status");
        Asserts.assertTrue(((Map<?, ?>) decode(r)).get("intervals") instanceof List, "all has list");
        Asserts.assertEquals(404L, get("/nope").statusCode(), "unknown route 404");
        // /intervals/123 只允许 DELETE
        Asserts.assertEquals(404L, get("/intervals/123").statusCode(), "GET item not routed -> 404");
    }

    // ------------------------------------------------------------------

    private static long countAt(long t) throws Exception {
        return ((Number) decode(get("/intervals/count?at=" + t)).get("count")).longValue();
    }

    /** @return 插入后分配的 id */
    private static long postOk(String json) throws Exception {
        HttpResponse<String> r = post("/intervals", json);
        Asserts.assertEquals(201L, r.statusCode(), "insert status: " + r.body());
        return ((Number) decode(r).get("id")).longValue();
    }

    private static HttpResponse<String> get(String path) throws Exception {
        return http.send(HttpRequest.newBuilder(URI.create(base + path)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private static HttpResponse<String> post(String path, String body) throws Exception {
        return http.send(HttpRequest.newBuilder(URI.create(base + path))
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(body)).build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private static HttpResponse<String> delete(String path) throws Exception {
        return http.send(HttpRequest.newBuilder(URI.create(base + path)).DELETE().build(),
                HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> decode(HttpResponse<String> r) {
        return (Map<String, Object>) JsonAccess.parse(r.body());
    }

    /** 跨包复用主代码的 Json 解析器。 */
    static final class JsonAccess {
        static Object parse(String s) {
            return com.example.intervalindex.json.Json.parse(s);
        }
    }
}
