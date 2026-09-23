package topk;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.Map;

/** HTTP 集成测试：真实启动 HttpServer，走网络接口验证。 */
public final class HttpApiTest {

    static HttpClient client = HttpClient.newHttpClient();
    static String base;

    public static void runAll() throws Exception {
        TopKService service = new TopKService(1000);
        HttpApi api = new HttpApi(service, 0); // 随机端口
        api.start();
        base = "http://127.0.0.1:" + api.port();
        try {
            health();
            insertAndQuery();
            duplicateRejected();
            retractViaHttp();
            windowSlideOutViaHttp();
            badRequests();
            System.out.println("PASS HttpApiTest.* (6 groups)");
        } finally {
            api.stop();
        }
    }

    static void health() throws Exception {
        Resp r = get("/health");
        Assert.eq(200, r.code, "health code");
        Assert.contains(r.body, "\"status\":\"ok\"", "health body");
    }

    static void insertAndQuery() throws Exception {
        Resp r = post("/events", "{\"eventId\":\"h1\",\"group\":\"g\",\"key\":\"a\",\"delta\":5,\"ts\":1000}");
        Assert.eq(200, r.code, "insert code");
        Assert.contains(r.body, "\"status\":\"applied\"", "insert body");

        post("/events", "{\"eventId\":\"h2\",\"group\":\"g\",\"key\":\"b\",\"delta\":5,\"ts\":1000}");
        post("/events", "{\"eventId\":\"h3\",\"group\":\"g\",\"key\":\"c\",\"delta\":9,\"ts\":1000}");

        r = get("/topk?group=g&k=2&now=1000");
        Assert.eq(200, r.code, "topk code");
        // c=9 第一；a、b 并列 5 分，按 key 升序 a 在前
        Assert.eq(
                "{\"group\":\"g\",\"k\":2,\"windowMs\":1000,\"watermark\":1000,\"count\":2,"
                        + "\"items\":[{\"rank\":1,\"key\":\"c\",\"score\":9},{\"rank\":2,\"key\":\"a\",\"score\":5}]}",
                r.body, "topk body");

        r = get("/ranking?group=g&now=1000");
        Assert.contains(r.body, "\"rank\":2,\"key\":\"a\",\"score\":5", "ranking a");
        Assert.contains(r.body, "\"rank\":3,\"key\":\"b\",\"score\":5", "ranking b");
    }

    static void duplicateRejected() throws Exception {
        Resp r = post("/events", "{\"eventId\":\"h1\",\"group\":\"g\",\"key\":\"z\",\"delta\":1,\"ts\":1000}");
        Assert.eq(409, r.code, "duplicate status code");
        Assert.contains(r.body, "duplicate_event_id", "duplicate body");
    }

    static void retractViaHttp() throws Exception {
        Resp r = post("/retract", "{\"eventId\":\"h3\"}");
        Assert.eq(200, r.code, "retract code");
        Assert.contains(r.body, "\"status\":\"retracted\"", "retract body");
        // 重复撤回：幂等
        r = post("/retract", "{\"eventId\":\"h3\"}");
        Assert.contains(r.body, "\"status\":\"not_active\"", "retract again");
        r = post("/retract", "{\"eventId\":\"nope\"}");
        Assert.contains(r.body, "\"status\":\"not_active\"", "retract unknown");
        // c 被撤回后，a 升到第一
        r = get("/topk?group=g&k=1&now=1000");
        Assert.contains(r.body, "\"rank\":1,\"key\":\"a\"", "top1 after retract");
    }

    static void windowSlideOutViaHttp() throws Exception {
        // 窗口 1000；h1/h2 ts=1000，now=2000 时滑出
        Resp r = get("/ranking?group=g&now=2000");
        Assert.eq(
                "{\"group\":\"g\",\"windowMs\":1000,\"watermark\":2000,\"count\":0,\"items\":[]}",
                r.body, "all expired");
        // 过期后再撤回：不重复扣减（排名仍为空，分数不会变负）
        post("/retract", "{\"eventId\":\"h1\"}");
        r = get("/ranking?group=g&now=2000");
        Assert.contains(r.body, "\"count\":0", "no double deduct after expiry");
    }

    static void badRequests() throws Exception {
        Resp r = post("/events", "{\"eventId\":\"x\"}");
        Assert.eq(400, r.code, "missing fields -> 400");
        r = post("/events", "not json");
        Assert.eq(400, r.code, "bad json -> 400");
        r = get("/topk?k=1");
        Assert.eq(400, r.code, "missing group -> 400");
        r = get("/nonexistent");
        Assert.eq(404, r.code, "unknown path -> 404");
    }

    record Resp(int code, String body) {}

    static Resp get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        return new Resp(resp.statusCode(), resp.body());
    }

    static Resp post(String path, String json) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .POST(HttpRequest.BodyPublishers.ofString(json))
                .header("Content-Type", "application/json")
                .build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        return new Resp(resp.statusCode(), resp.body());
    }
}
