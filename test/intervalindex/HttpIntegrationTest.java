package intervalindex;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * 用 JDK 内置 java.net.http 客户端对真实 HTTP 服务做端到端测试。
 */
@SuppressWarnings("unchecked")
public class HttpIntegrationTest {

    private static HttpServerApp app;
    private static HttpClient client;
    private static String base;

    public static void run() throws Exception {
        app = new HttpServerApp(new IntervalStore());
        int port = app.start("127.0.0.1", 0);
        base = "http://127.0.0.1:" + port;
        client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

        health();
        crudFlow();
        overlapAndCoverage();
        validationErrors();
        methodAndPathErrors();

        app.stop();
        System.out.println("HttpIntegrationTest OK");
    }

    private static void health() throws Exception {
        HttpResponse<String> r = get("/health");
        Check.eq(r.statusCode(), 200, "health status");
        Map<String, Object> body = Json.parseObject(r.body());
        Check.eq(body.get("status"), "ok", "health body");
    }

    private static void crudFlow() throws Exception {
        // 初始为空
        Map<String, Object> empty = getJson("/intervals");
        Check.eq(empty.get("distinctIntervals"), 0L, "empty list distinct");
        Check.eq(((List<?>) empty.get("intervals")).size(), 0, "empty list size");

        // 插入
        Map<String, Object> ins = postJson("/intervals", """
                {"lo":1,"hi":5}""");
        Check.eq(ins.get("inserted"), 1L, "insert response");
        Check.eq(ins.get("currentCount"), 1L, "currentCount after insert");

        // 再插两份相同区间
        ins = postJson("/intervals", """
                {"lo":1,"hi":5,"count":2}""");
        Check.eq(ins.get("inserted"), 2L, "insert two copies");
        Check.eq(ins.get("currentCount"), 3L, "currentCount after duplicate insert");
        Check.eq(ins.get("distinctIntervals"), 1L, "one distinct");
        Check.eq(ins.get("totalIntervals"), 3L, "three total");

        // 列出
        Map<String, Object> listing = getJson("/intervals");
        List<?> arr = (List<?>) listing.get("intervals");
        Check.eq(arr.size(), 1, "listing one distinct");
        Map<String, Object> row = (Map<String, Object>) arr.get(0);
        Check.eq(row.get("lo"), 1L, "listed lo");
        Check.eq(row.get("hi"), 5L, "listed hi");
        Check.eq(row.get("count"), 3L, "listed count");

        // 删一份（JSON body）
        Map<String, Object> del = deleteJson("/intervals", """
                {"lo":1,"hi":5}""");
        Check.eq(del.get("removed"), 1L, "delete one");
        Check.eq(del.get("remainingCount"), 2L, "remaining after delete");

        // 删光（query string 形式）
        del = deleteRaw("/intervals?lo=1&hi=5&count=10");
        Check.eq(del.get("removed"), 2L, "delete remainder via query string");
        Check.eq(del.get("totalIntervals"), 0L, "zero total after drain");

        // 再删 → 404
        HttpResponse<String> missing = send("DELETE", "/intervals", """
                {"lo":1,"hi":5}""");
        Check.eq(missing.statusCode(), 404, "delete missing -> 404");
        Map<String, Object> errBody = Json.parseObject(missing.body());
        Check.that(errBody.get("error") instanceof String, "404 has error message");
    }

    private static void overlapAndCoverage() throws Exception {
        postJson("/intervals", """
                {"lo":0,"hi":10}""");
        postJson("/intervals", """
                {"lo":2,"hi":8,"count":2}""");
        postJson("/intervals", """
                {"lo":10,"hi":20}"""); // 与 [0,10) 相邻

        // 覆盖计数
        Check.eq(getJson("/intervals/coverage?t=0").get("coverage"), 1L, "coverage t=0");
        Check.eq(getJson("/intervals/coverage?t=2").get("coverage"), 3L, "coverage t=2 (dup)");
        Check.eq(getJson("/intervals/coverage?t=8").get("coverage"), 1L, "coverage t=8");
        Check.eq(getJson("/intervals/coverage?t=10").get("coverage"), 1L, "coverage t=10 adjacent");
        Check.eq(getJson("/intervals/coverage?t=20").get("coverage"), 0L, "coverage t=20");

        // POST body 形式的覆盖计数
        Map<String, Object> covPost = postJson("/intervals/coverage", """
                {"t":5}""");
        Check.eq(covPost.get("coverage"), 3L, "coverage via POST t=5");

        // 交集
        Map<String, Object> ov = postJson("/intervals/overlap", """
                {"lo":8,"hi":12}""");
        Check.eq(ov.get("matchCount"), 2L, "overlap count: [0,10) and [10,20) (not [2,8))");
        List<?> overlaps = (List<?>) ov.get("overlaps");
        Check.eq(overlaps.size(), 2, "grouped overlap size");

        // 查询区间正好与重复区间相同
        ov = postJson("/intervals/overlap", """
                {"lo":2,"hi":8}""");
        Check.eq(ov.get("matchCount"), 3L, "exact match counts duplicates + outer");

        // 不与任何区间相交（落在相邻缝隙）
        ov = postJson("/intervals/overlap", """
                {"lo":-5,"hi":0}""");
        Check.eq(ov.get("matchCount"), 0L, "query before all -> no matches");
    }

    private static void validationErrors() throws Exception {
        // 空区间
        HttpResponse<String> r = send("POST", "/intervals", """
                {"lo":3,"hi":3}""");
        Check.eq(r.statusCode(), 400, "empty interval -> 400");
        Check.that(Json.parseObject(r.body()).get("error").toString().contains("lo must be < hi"),
                "empty interval error text");

        // 逆序区间
        r = send("POST", "/intervals", """
                {"lo":9,"hi":2}""");
        Check.eq(r.statusCode(), 400, "reversed interval -> 400");

        // 缺字段
        r = send("POST", "/intervals", """
                {"lo":1}""");
        Check.eq(r.statusCode(), 400, "missing hi -> 400");

        // 非法 JSON
        r = send("POST", "/intervals", "not-json");
        Check.eq(r.statusCode(), 400, "garbage body -> 400");

        // 空 body
        r = send("POST", "/intervals", "");
        Check.eq(r.statusCode(), 400, "empty body -> 400");

        // 小数端点
        r = send("POST", "/intervals", """
                {"lo":1.5,"hi":2}""");
        Check.eq(r.statusCode(), 400, "fractional endpoint -> 400");

        // 字段类型错
        r = send("POST", "/intervals", """
                {"lo":"1","hi":2}""");
        Check.eq(r.statusCode(), 400, "string lo -> 400");

        // count <= 0
        r = send("POST", "/intervals", """
                {"lo":1,"hi":2,"count":0}""");
        Check.eq(r.statusCode(), 400, "count=0 -> 400");

        // 交集查询非法区间
        r = send("POST", "/intervals/overlap", """
                {"lo":5,"hi":5}""");
        Check.eq(r.statusCode(), 400, "empty overlap query -> 400");

        // 覆盖计数缺 t
        r = send("GET", "/intervals/coverage", null);
        Check.eq(r.statusCode(), 400, "coverage without t -> 400");
        r = send("GET", "/intervals/coverage?t=abc", null);
        Check.eq(r.statusCode(), 400, "coverage with bad t -> 400");
    }

    private static void methodAndPathErrors() throws Exception {
        HttpResponse<String> r = send("PUT", "/intervals", "{}");
        Check.eq(r.statusCode(), 405, "PUT /intervals -> 405");
        r = send("GET", "/nope", null);
        Check.eq(r.statusCode(), 404, "unknown path -> 404");
        r = send("POST", "/health", null);
        Check.eq(r.statusCode(), 405, "POST /health -> 405");
    }

    // ---- HTTP 辅助 ----

    private static HttpResponse<String> get(String path) throws IOException, InterruptedException {
        return send("GET", path, null);
    }

    private static Map<String, Object> getJson(String path) throws Exception {
        HttpResponse<String> r = get(path);
        Check.eq(r.statusCode(), 200, "GET " + path + " status");
        return Json.parseObject(r.body());
    }

    private static Map<String, Object> postJson(String path, String body) throws Exception {
        HttpResponse<String> r = send("POST", path, body);
        Check.eq(r.statusCode(), 200, "POST " + path + " status, body=" + r.body());
        return Json.parseObject(r.body());
    }

    private static Map<String, Object> deleteJson(String path, String body) throws Exception {
        HttpResponse<String> r = send("DELETE", path, body);
        Check.eq(r.statusCode(), 200, "DELETE " + path + " status, body=" + r.body());
        return Json.parseObject(r.body());
    }

    private static Map<String, Object> deleteRaw(String pathWithQuery) throws Exception {
        HttpResponse<String> r = send("DELETE", pathWithQuery, null);
        Check.eq(r.statusCode(), 200, "DELETE " + pathWithQuery + " status, body=" + r.body());
        return Json.parseObject(r.body());
    }

    private static HttpResponse<String> send(String method, String path, String body)
            throws IOException, InterruptedException {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + path))
                .timeout(Duration.ofSeconds(10));
        if (body == null) {
            b.method(method, HttpRequest.BodyPublishers.noBody());
        } else {
            b.header("Content-Type", "application/json")
                    .method(method, HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8));
        }
        return client.send(b.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }
}
