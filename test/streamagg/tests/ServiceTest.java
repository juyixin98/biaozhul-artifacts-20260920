package streamagg.tests;

import java.math.BigDecimal;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Map;

import streamagg.core.ManualClock;
import streamagg.core.ManualScheduler;
import streamagg.json.Json;
import streamagg.json.JsonParser;
import streamagg.json.JsonWriter;
import streamagg.service.HttpService;

/**
 * 端到端测试：在随机空闲端口启动真实 HttpService（注入 ManualClock/ManualScheduler），
 * 通过 HTTP 跑验收场景：先撤销后新增、多次更正、重放/对账。
 */
public final class ServiceTest {

    private static HttpService service;
    private static HttpClient client;
    private static String base;

    public static void main(String[] args) throws Exception {
        service = new HttpService(0, new ManualClock(1_700_000_000_000L),
                new ManualScheduler(), 0L);
        service.start();
        base = "http://localhost:" + service.getPort();
        client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

        TestFramework t = new TestFramework();
        try {
            testHealth(t);
            testValidation(t);
            testAcceptanceScenario(t);
            testBatch(t);
            testReplayAndReconcileEndpoints(t);
            testReset(t);
            testUnknownRoute(t);
        } finally {
            service.close();
        }

        int code = t.summary("ServiceTest");
        if (code != 0) {
            System.exit(code);
        }
    }

    private static void testHealth(TestFramework t) throws Exception {
        t.test("GET /health 200", () -> {
            HttpResponse<String> resp = get("/health");
            t.eq(resp.statusCode(), 200, "状态码");
            Map<String, Object> body = obj(resp);
            t.eq(body.get("status"), "ok", "status 字段");
            t.eq(((BigDecimal) body.get("timeMillis")).longValue(), 1_700_000_000_000L,
                    "时间取自注入时钟");
        });
    }

    private static void testValidation(TestFramework t) throws Exception {
        t.test("非法请求返回 400", () -> {
            HttpResponse<String> resp = post("/events", "{not json");
            t.eq(resp.statusCode(), 400, "非法 JSON -> 400");
            t.eq(obj(resp).get("ok"), Boolean.FALSE, "错误体 ok=false");

            HttpResponse<String> resp2 = post("/events",
                    "{\"eventId\":\"e1\",\"op\":\"BOGUS\"}");
            t.eq(resp2.statusCode(), 400, "未知 op -> 400");

            HttpResponse<String> resp3 = post("/events",
                    "{\"eventId\":\"e1\",\"op\":\"RETRACT\",\"value\":1}");
            t.eq(resp3.statusCode(), 400, "RETRACT 带 value -> 400");

            HttpResponse<String> resp4 = post("/events",
                    "{\"eventId\":\"e1\",\"op\":\"ADD\",\"key\":\"k\"}");
            t.eq(resp4.statusCode(), 400, "ADD 缺 value -> 400");
        });
    }

    /** 验收场景（经网络）：先撤销后新增 + 多次更正（含乱序）+ 幂等。 */
    private static void testAcceptanceScenario(TestFramework t) throws Exception {
        t.test("HTTP 验收：先撤销后新增、多次更正、乱序、幂等", () -> {
            postOk("/events", op("e1", "RETRACT", null, null, 2, "r1"));
            Map<String, Object> retractDup = obj(postOk("/events",
                    op("e1", "RETRACT", null, null, 2, "r1")));
            t.eq(retractDup.get("status"), "DUPLICATE", "重复撤销幂等");

            Map<String, Object> addResp = obj(postOk("/events",
                    op("e1", "ADD", "k1", "10", 1, "a1")));
            t.eq(addResp.get("status"), "APPLIED", "ADD 到达并级联撤销");
            checkSum(t, "k1", 0, "0", "撤销级联后为 0");

            postOk("/events", op("e1", "ADD", "k1", "10", 3, "a2")); // 重新新增
            checkSum(t, "k1", 1, "10", "重新计入");

            // 更正乱序：v6 先到缓存，v5、v4 后补
            t.eq(obj(postOk("/events", op("e1", "CORRECT", "k1", "60", 6, "c6"))).get("status"),
                    "BUFFERED", "v6 缓存");
            checkSum(t, "k1", 1, "10", "缓存期间不变");
            t.eq(obj(postOk("/events", op("e1", "CORRECT", "k1", "50", 5, "c5"))).get("status"),
                    "BUFFERED", "v5 缓存");
            Map<String, Object> v4 = obj(postOk("/events",
                    op("e1", "CORRECT", "k1", "40", 4, "c4")));
            t.eq(v4.get("status"), "APPLIED", "v4 到达级联 v5、v6");
            checkSum(t, "k1", 1, "60", "最终取 v6 的值");

            // 改键
            postOk("/events", op("e1", "CORRECT", "k2", "60", 7, "c7"));
            checkSum(t, "k1", 0, "0", "旧键清空");
            checkSum(t, "k2", 1, "60", "新键持有");

            // 账本
            Map<String, Object> ledger = obj(get("/ledger"));
            List<?> entries = (List<?>) ledger.get("entries");
            t.eq(entries.size(), 1, "账本 1 条存活");
            Map<?, ?> only = (Map<?, ?>) entries.get(0);
            t.eq(only.get("key"), "k2", "账本 key");
            t.eq(((BigDecimal) only.get("value")).compareTo(new BigDecimal("60")), 0, "账本 value");
            t.eq(((BigDecimal) only.get("version")).longValue(), 7L, "账本版本");
        });
    }

    private static void testBatch(TestFramework t) throws Exception {
        t.test("POST /events/batch 顺序提交并返回逐条结果", () -> {
            postOk("/reset", "{}");
            String batch = JsonWriter.write(List.of(
                    Map.of("eventId", "b1", "op", "ADD", "key", "x", "value", new BigDecimal("1")),
                    Map.of("eventId", "b2", "op", "ADD", "key", "x", "value", new BigDecimal("2")),
                    Map.of("eventId", "b1", "op", "CORRECT", "key", "x",
                            "value", new BigDecimal("5"), "version", 2),
                    Map.of("eventId", "bad", "op", "ADD")
            ));
            HttpResponse<String> resp = postOk("/events/batch", batch);
            Map<String, Object> body = obj(resp);
            t.eq(((BigDecimal) body.get("received")).intValue(), 4, "接收 4 条");
            t.eq(((BigDecimal) body.get("errors")).intValue(), 1, "1 条错误不影响其余");
            checkSum(t, "x", 2, "7", "批量聚合正确");
        });
    }

    private static void testReplayAndReconcileEndpoints(TestFramework t) throws Exception {
        t.test("/reconcile 与 /replay 均返回 consistent=true", () -> {
            Map<String, Object> rec = obj(postOk("/reconcile", "{}"));
            t.eq(rec.get("consistent"), Boolean.TRUE, "对账一致");
            Map<String, Object> rep = obj(postOk("/replay", "{}"));
            t.eq(rep.get("consistent"), Boolean.TRUE, "重放一致");
            // /journal 有 3 条成功记录 + 批量里的记录
            Map<String, Object> journal = obj(get("/journal"));
            List<?> entries = (List<?>) journal.get("entries");
            t.check(entries.size() >= 3, "日志包含至少 3 条，实际 " + entries.size());
        });
    }

    private static void testReset(TestFramework t) throws Exception {
        t.test("POST /reset 清空状态", () -> {
            postOk("/reset", "{}");
            checkSumAbsent(t, "x", "重置后键消失");
            Map<String, Object> ledger = obj(get("/ledger"));
            t.eq(((List<?>) ledger.get("entries")).size(), 0, "账本清空");
        });
    }

    private static void testUnknownRoute(TestFramework t) throws Exception {
        t.test("未知端点 404", () -> {
            HttpResponse<String> resp = get("/nope");
            t.eq(resp.statusCode(), 404, "404");
        });
    }

    // ------------------------------------------------------------------

    private static String op(String eventId, String type, String key, String value,
                             long version, String opId) {
        Map<String, Object> m = Json.object();
        m.put("eventId", eventId);
        m.put("op", type);
        if (key != null) {
            m.put("key", key);
        }
        if (value != null) {
            m.put("value", new BigDecimal(value));
        }
        m.put("version", version);
        if (opId != null) {
            m.put("opId", opId);
        }
        return JsonWriter.write(m);
    }

    private static void checkSum(TestFramework t, String key, long count, String sum, String label)
            throws Exception {
        Map<String, Object> body = obj(get("/stats?key=" + key));
        @SuppressWarnings("unchecked")
        Map<String, Object> stats = (Map<String, Object>) body.get("stats");
        t.eq(((BigDecimal) stats.get("count")).longValue(), count, label + " count");
        t.eq(((BigDecimal) stats.get("sum")).compareTo(new BigDecimal(sum)), 0, label + " sum");
    }

    private static void checkSumAbsent(TestFramework t, String key, String label) throws Exception {
        Map<String, Object> body = obj(get("/stats"));
        @SuppressWarnings("unchecked")
        Map<String, Object> all = (Map<String, Object>) body.get("stats");
        t.eq(all.containsKey(key), false, label);
    }

    private static HttpResponse<String> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static HttpResponse<String> post(String path, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static HttpResponse<String> postOk(String path, String body) throws Exception {
        HttpResponse<String> resp = post(path, body);
        if (resp.statusCode() >= 400) {
            throw new AssertionError(path + " 返回 " + resp.statusCode() + ": " + resp.body());
        }
        return resp;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> obj(HttpResponse<String> resp) {
        return (Map<String, Object>) JsonParser.parse(resp.body());
    }
}
