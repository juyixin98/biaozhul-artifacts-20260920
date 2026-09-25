package drvb.tests;

import drvb.http.RuleHttpServer;
import drvb.json.Json;
import drvb.service.DynamicRuleService;
import drvb.time.ManualClock;
import drvb.time.ManualScheduler;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;
import java.util.Map;

import static drvb.tests.Asserts.assertEquals;
import static drvb.tests.Asserts.assertFalse;
import static drvb.tests.Asserts.assertTrue;

/** 真实启动 HTTP 服务做回环测试，验证 JSON 输入输出、状态码与完整业务流程。 */
public class HttpApiTest {

    private HttpClient client;
    private String base;
    private RuleHttpServer http;
    private DynamicRuleService svc;

    private void start(long start, long allowedLateness, long retention) throws Exception {
        ManualClock clock = new ManualClock(start);
        svc = new DynamicRuleService(clock, new ManualScheduler(clock),
                allowedLateness, retention);
        http = new RuleHttpServer(svc);
        int port = http.start(0);
        base = "http://127.0.0.1:" + port;
        client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    }

    private void stop() {
        http.stop();
    }

    private HttpResponse<String> post(String path, String json) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static Map<String, Object> obj(String json) {
        return Json.asObject(Json.parse(json));
    }

    @Test
    public void fullWorkflowOverHttp() throws Exception {
        start(100_000L, 0L, 1000L);
        try {
            // health
            HttpResponse<String> health = get("/health");
            assertEquals(200, health.statusCode(), "health 200");
            assertTrue(health.body().contains("\"clockMode\": \"manual\""),
                    "health 显示 manual 时钟");

            // 发布 v1 @0、v2 @100
            HttpResponse<String> p1 = post("/rules/publish",
                    "{\"versionId\":\"v1\",\"effectiveFrom\":0,"
                            + "\"predicate\":{\"op\":\"gte\",\"field\":\"amount\",\"value\":100}}");
            assertEquals(201, p1.statusCode(), "发布 v1 201");
            HttpResponse<String> p2 = post("/rules/publish",
                    "{\"versionId\":\"v2\",\"effectiveFrom\":100,"
                            + "\"predicate\":{\"op\":\"gte\",\"field\":\"amount\",\"value\":200}}");
            assertEquals(201, p2.statusCode(), "发布 v2 201");

            // 重复边界 -> 409 稳定错误码
            HttpResponse<String> badBoundary = post("/rules/publish",
                    "{\"versionId\":\"v3\",\"effectiveFrom\":100,"
                            + "\"predicate\":{\"op\":\"true\"}}");
            assertEquals(409, badBoundary.statusCode(), "边界冲突 409");
            assertTrue(badBoundary.body().contains("BAD_EFFECTIVE_FROM"), "错误码在响应体");

            // 坏 JSON -> 400
            HttpResponse<String> badJson = post("/events", "{not-json");
            assertEquals(400, badJson.statusCode(), "坏 JSON 400");
            assertTrue(badJson.body().contains("INVALID_JSON"), "INVALID_JSON 错误码");

            // 非法规则谓词 -> 400
            HttpResponse<String> badRule = post("/rules/publish",
                    "{\"versionId\":\"vx\",\"effectiveFrom\":200,"
                            + "\"predicate\":{\"op\":\"nope\"}}");
            assertEquals(400, badRule.statusCode(), "非法规则 400");

            // 事件：eventTime=50（v1）命中；eventTime=150（v2）不命中
            HttpResponse<String> e1 = post("/events",
                    "{\"eventId\":\"e1\",\"eventTime\":50,\"payload\":{\"amount\":150}}");
            assertEquals(200, e1.statusCode(), "事件 200");
            Map<String, Object> r1 = obj(Json.write(obj(e1.body()).get("result")));
            assertEquals("MATCHED", r1.get("status"), "e1 v1 命中");
            assertEquals("v1", r1.get("ruleVersionId"), "e1 用 v1");

            HttpResponse<String> e2 = post("/events",
                    "{\"eventId\":\"e2\",\"eventTime\":150,\"payload\":{\"amount\":150}}");
            Map<String, Object> r2 = obj(Json.write(obj(e2.body()).get("result")));
            assertEquals("FILTERED_OUT", r2.get("status"), "e2 v2 下未命中");
            assertEquals("v2", r2.get("ruleVersionId"), "e2 用 v2");

            // 缺失版本：eventTime=-1（首边界为 0）——业务拒绝，HTTP 仍 200，逐条标记
            HttpResponse<String> e3 = post("/events",
                    "{\"eventId\":\"e3\",\"eventTime\":-1,\"payload\":{\"amount\":999}}");
            Map<String, Object> r3 = obj(Json.write(obj(e3.body()).get("result")));
            assertEquals("REJECTED", r3.get("status"), "缺失版本被拒绝");
            assertEquals("MISSING_RULE_VERSION", r3.get("rejectReason"), "拒绝原因");
            assertFalse(r3.containsKey("ruleVersionId"), "拒绝结果无规则版本");

            // 批量事件（含乱序与拒绝）
            HttpResponse<String> batch = post("/events",
                    "[{\"eventId\":\"b1\",\"eventTime\":150,\"payload\":{\"amount\":250}},"
                            + "{\"eventId\":\"b2\",\"eventTime\":50,\"payload\":{\"amount\":50}},"
                            + "{\"eventId\":\"b3\",\"eventTime\":-9,\"payload\":{\"amount\":1}}]");
            assertEquals(200, batch.statusCode(), "批量 200");
            Map<String, Object> bm = obj(batch.body());
            assertEquals(3L, ((Number) bm.get("count")).longValue(), "批量 3 条");
            assertEquals(1L, ((Number) bm.get("rejectedCount")).longValue(), "其中 1 条拒绝");
            List<Object> results = Json.asArray(bm.get("results"));
            assertEquals("MATCHED", obj(Json.write(results.get(0))).get("status"), "b1 v2 命中");
            assertEquals("FILTERED_OUT", obj(Json.write(results.get(1))).get("status"),
                    "b2 v1 未命中");
            assertEquals("REJECTED", obj(Json.write(results.get(2))).get("status"),
                    "b3 拒绝");

            // 查询接口
            HttpResponse<String> lateQ = get("/results?status=MATCHED");
            Map<String, Object> qm = obj(lateQ.body());
            assertEquals(2L, ((Number) qm.get("count")).longValue(), "MATCHED 共 2 条（e1,b1）");
            HttpResponse<String> rejQ = get("/results?status=REJECTED");
            assertEquals(2L, ((Number) obj(rejQ.body()).get("count")).longValue(),
                    "REJECTED 共 2 条（e3,b3）");

            // 回滚：v1 自事件时间 200 起重新生效
            HttpResponse<String> rb = post("/rules/rollback",
                    "{\"versionId\":\"v1\",\"effectiveFrom\":200,\"note\":\"http 回滚\"}");
            assertEquals(201, rb.statusCode(), "回滚 201");
            HttpResponse<String> e4 = post("/events",
                    "{\"eventId\":\"e4\",\"eventTime\":250,\"payload\":{\"amount\":150}}");
            Map<String, Object> r4 = obj(Json.write(obj(e4.body()).get("result")));
            assertEquals("v1", r4.get("ruleVersionId"), "250 走回滚后的 v1");

            // GC：检查前提 -> 不满足；tick 推进处理时间不会改变水位线，需要推进事件时间
            HttpResponse<String> chk1 = post("/reclaim",
                    "{\"mode\":\"check\",\"versionId\":\"v2\"}");
            assertFalse(Json.asBoolean(obj(chk1.body()).get("eligible")),
                    "事件时间未到，v2 不可回收");
            post("/events",
                    "{\"eventId\":\"wm\",\"eventTime\":2000,\"payload\":{\"amount\":1}}");
            // WM=2000, retention=1000 -> gate=1000；v2 区间 [100,200) 结束于 200 <= 1000
            HttpResponse<String> chk2 = post("/reclaim",
                    "{\"mode\":\"check\",\"versionId\":\"v2\"}");
            assertTrue(Json.asBoolean(obj(chk2.body()).get("eligible")),
                    "闸门 1000 >= 区间结束 200，可回收");

            // 强制回收一个不满足前提的版本 -> 409
            HttpResponse<String> force = post("/reclaim",
                    "{\"mode\":\"one\",\"versionId\":\"v1\"}");
            assertEquals(409, force.statusCode(), "当前版本 v1 回收被拒 409");
            assertTrue(force.body().contains("RECLAIM_NOT_ELIGIBLE"), "回收前提错误码");

            // 自动扫描回收 v2
            HttpResponse<String> rec = post("/reclaim", "{\"mode\":\"eligible\"}");
            List<Object> reclaimed = Json.asArray(obj(rec.body()).get("reclaimed"));
            assertTrue(reclaimed.contains("v2"), "v2 被回收");

            // tick 推进手动时钟并触发调度（未注册周期任务时 firedTasks 为空数组）
            HttpResponse<String> tick = post("/admin/tick", "{\"advanceBy\":5000}");
            assertEquals(200, tick.statusCode(), "tick 200");
            assertEquals(105_000L,
                    ((Number) obj(tick.body()).get("processingTime")).longValue(),
                    "处理时间推进 5000");

            // /state 总览包含墓碑与三条绑定
            HttpResponse<String> state = get("/state");
            Map<String, Object> sm = obj(state.body());
            assertEquals(3, Json.asArray(sm.get("bindings")).size(), "三条绑定（含回滚）");
            boolean tombHasV2 = false;
            for (Object o : Json.asArray(sm.get("reclaimedVersions"))) {
                if ("v2".equals(obj(Json.write(o)).get("id"))) {
                    tombHasV2 = true;
                }
            }
            assertTrue(tombHasV2, "state 中 v2 列为墓碑");

            // 回收后晚到事件 -> 明确 RECLAIMED_RULE_VERSION
            HttpResponse<String> lateAfter = post("/events",
                    "{\"eventId\":\"lateAfter\",\"eventTime\":150,\"payload\":{\"amount\":250}}");
            Map<String, Object> lar = obj(Json.write(obj(lateAfter.body()).get("result")));
            assertEquals("REJECTED", lar.get("status"), "回收后晚到事件被拒绝");
            assertEquals("RECLAIMED_RULE_VERSION", lar.get("rejectReason"),
                    "原因为版本已回收，非套用最新 v1");

            // 404 与调度器状态
            assertEquals(404, get("/nope").statusCode(), "未知路由 404");
            HttpResponse<String> sched = get("/admin/scheduler");
            assertEquals(200, sched.statusCode(), "调度器状态 200");
        } finally {
            stop();
        }
    }

    @Test
    public void autoReclaimTaskFiresOnTick() throws Exception {
        start(0L, 0L, 0L);
        try {
            svc.enableAutoReclaim(1000L);
            post("/rules/publish",
                    "{\"versionId\":\"v1\",\"effectiveFrom\":0,\"predicate\":{\"op\":\"true\"}}");
            post("/rules/publish",
                    "{\"versionId\":\"v2\",\"effectiveFrom\":100,\"predicate\":{\"op\":\"true\"}}");
            post("/events", "{\"eventId\":\"e\",\"eventTime\":100,\"payload\":{}}");
            // WM=100, gate=100 >= v1 区间结束 100 -> v1 可回收，但尚未触发任务
            assertFalse(svc.table().isReclaimed("v1"), "tick 前 v1 存活");

            HttpResponse<String> tick = post("/admin/tick", "{\"advanceTo\":1000}");
            Map<String, Object> tm = obj(tick.body());
            List<Object> fired = Json.asArray(tm.get("firedTasks"));
            assertTrue(fired.contains("auto-reclaim-versions"), "tick 补跑自动回收任务");
            assertTrue(svc.table().isReclaimed("v1"), "tick 后 v1 已被自动回收");

            HttpResponse<String> sched = get("/admin/scheduler");
            assertTrue(sched.body().contains("\"runCount\": 1"), "任务运行次数为 1");
        } finally {
            stop();
        }
    }
}
