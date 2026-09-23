package ij;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

/** HTTP 接口端到端测试：真实启动 ApiServer，走 java.net.http 客户端调用。 */
final class ApiServerTest {

    private final Assert a;
    private final HttpClient client = HttpClient.newHttpClient();
    private final String base;
    private final ApiServer server;

    ApiServerTest(Assert a) throws Exception {
        this.a = a;
        server = new ApiServer(new SessionRegistry(), "127.0.0.1", 0);
        server.start();
        base = "http://127.0.0.1:" + server.port();
        try {
            healthAndErrors();
            fullLifecycle();
            batchActions();
        } finally {
            server.stop();
        }
    }

    private void healthAndErrors() throws Exception {
        HttpResponse<String> h = get("/health");
        a.eq(h.statusCode(), 200L, "health 200");
        a.eq(Json.parseObject(h.body()).get("status"), "ok", "health body");

        HttpResponse<String> missing = get("/sessions/nope");
        a.eq(missing.statusCode(), 404L, "unknown session 404");

        HttpResponse<String> bad = post("/sessions", "{not json}");
        a.eq(bad.statusCode(), 400L, "bad json 400");
    }

    private void fullLifecycle() throws Exception {
        // 创建会话（默认区间 [-2,2]）。
        HttpResponse<String> created = post("/sessions", "{\"sessionId\":\"e2e\"}");
        a.eq(created.statusCode(), 201L, "create 201");
        Map<String, Object> stats = Json.parseObject(created.body());
        a.eq(stats.get("watermarkLeft"), null, "initial watermark is null");

        // 重复创建冲突。
        a.eq(post("/sessions", "{\"sessionId\":\"e2e\"}").statusCode(), 409L, "duplicate 409");

        // 列表包含 e2e。
        Map<String, Object> list = Json.parseObject(get("/sessions").body());
        a.check(((List<?>) list.get("sessions")).contains("e2e"), "session listed");

        // 单事件 + 批量事件。
        a.eq(post("/sessions/e2e/events/left",
                "{\"key\":\"u\",\"ts\":10,\"payload\":{\"src\":\"l1\"}}").statusCode(), 200L, "post event");
        HttpResponse<String> batch = post("/sessions/e2e/events/right",
                "[{\"key\":\"u\",\"ts\":11},{\"key\":\"u\",\"ts\":12},{\"key\":\"v\",\"ts\":10}]");
        a.eq(batch.statusCode(), 200L, "batch events");
        Map<String, Object> batchBody = Json.parseObject(batch.body());
        a.eq(((List<?>) batchBody.get("results")).size(), 3L, "3 event results");

        // 配对：L@10 与 R@11、R@12（闭区间 [-2,+2] 内），v 不配对。
        HttpResponse<String> pairs = get("/sessions/e2e/pairs");
        Map<String, Object> pb = Json.parseObject(pairs.body());
        a.eq(pb.get("count"), 2L, "two pairs via http");

        // payload 默认不返回，带 includePayload 时回显。
        Map<String, Object> firstPair = Json.asObject(
                ((List<?>) pb.get("pairs")).get(0), "pairs[0]");
        a.check(!firstPair.containsKey("leftPayload"), "payload hidden by default");
        Map<String, Object> withPayload = Json.parseObject(
                get("/sessions/e2e/pairs?includePayload=true").body());
        Map<String, Object> p0 = Json.asObject(
                ((List<?>) withPayload.get("pairs")).get(0), "pairs payload");
        a.eq(Json.asObject(p0.get("leftPayload"), "leftPayload").get("src"), "l1",
                "payload echoed when requested");

        // key 过滤。
        a.eq(Json.parseObject(get("/sessions/e2e/pairs?key=v").body()).get("count"), 0L,
                "key filter");

        // 水位回收：双侧推进后状态清空，stats 中能看到回收量。
        HttpResponse<String> wm = post("/sessions/e2e/watermarks/left", "{\"watermark\":100}");
        Map<String, Object> wmBody = Json.parseObject(wm.body());
        a.eq(wmBody.get("reclaimedThisCall"), 0L, "one-sided watermark reclaims nothing");
        HttpResponse<String> wm2 = post("/sessions/e2e/watermarks/right", "{\"watermark\":100}");
        Map<String, Object> stats2 = Json.asObject(
                Json.parseObject(wm2.body()).get("stats"), "stats");
        a.eq(stats2.get("totalReclaimed"), 4L, "all 4 events reclaimed via http");
        a.eq(stats2.get("pairsEmitted"), 2L, "pairsEmitted counter");

        // 迟到事件。
        HttpResponse<String> late = post("/sessions/e2e/events/left", "{\"key\":\"u\",\"ts\":5}");
        Map<String, Object> lateR = Json.asObject(
                ((List<?>) Json.parseObject(late.body()).get("results")).get(0), "late result");
        a.eq(lateR.get("droppedLate"), Boolean.TRUE, "late event flagged over http");

        // 删除。
        a.eq(delete("/sessions/e2e").statusCode(), 200L, "delete session");
        a.eq(get("/sessions/e2e").statusCode(), 404L, "gone after delete");
    }

    private void batchActions() throws Exception {
        post("/sessions", "{\"sessionId\":\"act\",\"lowerBound\":-1,\"upperBound\":1}");
        String req = "{\"actions\":["
                + "{\"type\":\"event\",\"side\":\"left\",\"key\":\"k\",\"ts\":5},"
                + "{\"type\":\"event\",\"side\":\"right\",\"key\":\"k\",\"ts\":5},"
                + "{\"type\":\"event\",\"side\":\"right\",\"key\":\"k\",\"ts\":7},"
                + "{\"type\":\"watermark\",\"side\":\"left\",\"watermark\":100},"
                + "{\"type\":\"watermark\",\"side\":\"right\",\"watermark\":100}"
                + "]}";
        HttpResponse<String> resp = post("/sessions/act/actions", req);
        a.eq(resp.statusCode(), 200L, "actions 200");
        Map<String, Object> body = Json.parseObject(resp.body());
        a.eq(((List<?>) body.get("newPairs")).size(), 1L, "actions produce 1 pair (R@7 out of range)");
        Map<String, Object> stats = Json.asObject(body.get("stats"), "stats");
        a.eq(stats.get("totalReclaimed"), 3L, "actions reclaim all events");
        List<?> actions = (List<?>) body.get("actions");
        Map<String, Object> last = Json.asObject(actions.get(actions.size() - 1), "last action");
        a.eq(last.get("type"), "watermark", "ordered action results");
        a.eq(last.get("reclaimedThisCall"), 3L, "final watermark call reports reclaim delta");
    }

    // ---------------- HTTP 辅助 ----------------

    private HttpResponse<String> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> post(String path, String json) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> delete(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).DELETE().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
