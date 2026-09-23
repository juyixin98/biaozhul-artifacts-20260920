package tumbling;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

import static tumbling.TestRunner.eq;
import static tumbling.TestRunner.assertTrue;

/** 经真实 HTTP 回路验证 Server 路由、批量接口与 JSON 序列化。 */
public final class HttpTest {

    private Server server;
    private HttpClient client;
    private String base;

    private void setUp() throws Exception {
        server = new Server(0, 10L, 2L); // 端口 0 = 由操作系统分配
        server.start();
        base = "http://localhost:" + server.getAddressPort();
        client = HttpClient.newHttpClient();
    }

    private void tearDown() {
        server.close();
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> post(String path, String json) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json)).build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        Map<String, Object> body = Json.parseObject(resp.body());
        if (resp.statusCode() != 200) {
            throw new AssertionError(path + " 返回 " + resp.statusCode() + ": " + resp.body());
        }
        return body;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() != 200) {
            throw new AssertionError(path + " 返回 " + resp.statusCode() + ": " + resp.body());
        }
        return Json.parseObject(resp.body());
    }

    /** 单条 + 批量接入、乱序、水位推进，经 HTTP 验证最终计数。 */
    @TestRunner.Test
    public void ingestWatermarkAndCountOverHttp() throws Exception {
        setUp();
        try {
            // 批量接入乱序事件
            Map<String, Object> batch = post("/events", """
                    {"events":[
                      {"partition":"a","eventId":"e2","eventTime":2},
                      {"partition":"a","eventId":"e7","eventTime":7},
                      {"partition":"a","eventId":"e5","eventTime":5}
                    ]}""");
            eq(((Number) batch.get("count")).longValue(), 3L, "批量返回 3 条结果");

            Map<String, Object> wm = post("/watermarks",
                    "{\"partition\":\"a\",\"watermark\":10}");
            eq(wm.get("globalWatermarkAfter"), 10L, "全局水位推进到 10");
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> emitted = (List<Map<String, Object>>) wm.get("emitted");
            eq(emitted.size(), 1L, "水位 10 触发一个窗口");
            eq(emitted.get(0).get("type"), "fire", "类型为 fire");
            eq(emitted.get(0).get("count"), 3L, "触发计数 3");

            // 晚到修订 + 最终关闭
            post("/events", "{\"partition\":\"a\",\"eventId\":\"e6\",\"eventTime\":6}");
            Map<String, Object> fin = post("/watermarks",
                    "{\"partition\":\"a\",\"watermark\":12}");
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> finEmitted = (List<Map<String, Object>>) fin.get("emitted");
            eq(finEmitted.get(0).get("type"), "final", "[0,10) 最终关闭");
            eq(finEmitted.get(0).get("count"), 4L, "最终计数 4（含晚到修订）");

            // 侧输出视图
            post("/events", "{\"partition\":\"a\",\"eventId\":\"old\",\"eventTime\":1}");
            Map<String, Object> side = get("/side-output");
            @SuppressWarnings("unchecked")
            List<Object> sideList = (List<Object>) side.get("sideOutput");
            eq(sideList.size(), 1L, "侧输出 1 条");
        } finally {
            tearDown();
        }
    }

    /** 空闲分区恢复、批量水位、重复事件、按分区查询窗口。 */
    @TestRunner.Test
    public void idleDuplicateAndQueriesOverHttp() throws Exception {
        setUp();
        try {
            // 先慢后快上报，全局水位被 slow 压在 5。
            post("/watermarks", "{\"partition\":\"slow\",\"watermark\":5}");
            post("/watermarks", "{\"partition\":\"fast\",\"watermark\":100}");
            eq(get("/config").get("globalWatermark"), 5L, "全局水位=活跃分区最小值 5");

            post("/partitions/idle", "{\"partition\":\"slow\",\"idle\":true}");
            eq(get("/config").get("globalWatermark"), 100L, "slow 空闲后全局水位=100");

            // fast 在 wm=100 下发 t=50 的事件，已超容忍 → 直接侧输出；
            // 改发一个当前水位之后的正常事件验证计数与去重。
            post("/events", "{\"partition\":\"fast\",\"eventId\":\"f105\",\"eventTime\":105}");
            Map<String, Object> dup = post("/events",
                    "{\"partition\":\"fast\",\"eventId\":\"f105\",\"eventTime\":106}");
            eq(dup.get("status"), "duplicate", "HTTP 重复事件判定");

            // 批量水位（顺序确定：数组顺序即处理顺序）：slow 恢复并追到 120，fast 到 120。
            Map<String, Object> batchWm = post("/watermarks", """
                    {"watermarks":[
                      {"partition":"fast","watermark":120},
                      {"partition":"slow","watermark":120}
                    ]}""");
            eq(((Number) batchWm.get("count")).longValue(), 2L, "批量水位 2 条结果");
            eq(get("/config").get("globalWatermark"), 120L, "恢复并追上后全局水位=120");

            // 按分区查询窗口：[100,110) final，计数 1（重复未计入）。
            Map<String, Object> wins = get("/windows?partition=fast");
            @SuppressWarnings("unchecked")
            List<Object> list = (List<Object>) wins.get("windows");
            boolean sawFinal = false;
            for (Object o : list) {
                @SuppressWarnings("unchecked")
                Map<String, Object> w = (Map<String, Object>) o;
                if (((Number) w.get("windowStart")).longValue() == 100L
                        && "final".equals(w.get("state"))
                        && ((Number) w.get("count")).longValue() == 1L) sawFinal = true;
            }
            assertTrue(sawFinal, "[100,110) final 且计数 1（重复未计入）");

            // reset 后状态清空
            post("/reset", "{}");
            eq(get("/snapshot").get("globalWatermark"), Long.MIN_VALUE, "reset 后水位归位");
            eq(((Number) ((Map<?, ?>) get("/snapshot").get("config")).get("windowSize")).longValue(),
                    10L, "reset 保留配置");

            // reset 同时改配置
            Map<String, Object> reset2 = post("/reset",
                    "{\"windowSize\":7,\"allowedLateness\":3}");
            eq(((Map<?, ?>) reset2.get("config")).get("windowSize"), 7L, "reset 可改窗口大小");
        } finally {
            tearDown();
        }
    }

    /** 无参 /windows：跨分区合并，且每条都带 partition 归属与 state。 */
    @TestRunner.Test
    public void allWindowsCarryPartitionAndState() throws Exception {
        setUp();
        try {
            post("/events", "{\"partition\":\"a\",\"eventId\":\"a1\",\"eventTime\":1}");
            post("/events", "{\"partition\":\"b\",\"eventId\":\"b1\",\"eventTime\":3}");
            post("/watermarks", "{\"partition\":\"a\",\"watermark\":10}");
            post("/watermarks", "{\"partition\":\"b\",\"watermark\":12}"); // b 容忍0? 不，容忍2→wm12 final

            Map<String, Object> wins = get("/windows");
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> list = (List<Map<String, Object>>) wins.get("windows");
            eq(list.size(), 2L, "两个分区各一个窗口");
            java.util.Set<String> keys = new java.util.TreeSet<>();
            for (Map<String, Object> w : list) {
                assertTrue(w.containsKey("partition"), "窗口条目带 partition");
                assertTrue(w.containsKey("state"), "窗口条目带 state");
                keys.add(w.get("partition") + ":" + w.get("state") + ":" + w.get("count"));
            }
            assertTrue(keys.contains("a:fired:1"), "a 窗口为 fired（wm=10 触发但容忍期未过）");
            assertTrue(keys.contains("b:final:1"), "b 窗口为 final（wm=12 容忍期结束）");
        } finally {
            tearDown();
        }
    }

    /** 回归：批量事件/水位非法时必须整体失败且不留状态（不部分执行）。 */
    @TestRunner.Test
    public void batchIsAtomicOnValidationError() throws Exception {
        setUp();
        try {
            // 第一条合法、第二条缺 eventTime → 整体 400，第一条也不得落库。
            int code = rawStatus("/events", """
                    {"events":[
                      {"partition":"a","eventId":"ok","eventTime":1},
                      {"partition":"a","eventId":"bad"}
                    ]}""");
            eq(code, 400L, "坏批量返回 400");
            Map<String, Object> snap = get("/snapshot");
            @SuppressWarnings("unchecked")
            List<Object> parts = (List<Object>) snap.get("partitions");
            eq(parts.size(), 0L, "400 时没有任何分区/事件落库（原子）");

            // 批量水位：第二条水位给小数 → 400，第一条不得推进。
            int codeWm = rawStatus("/watermarks", """
                    {"watermarks":[
                      {"partition":"a","watermark":10},
                      {"partition":"a","watermark":9.5}
                    ]}""");
            eq(codeWm, 400L, "坏水位批量返回 400");
            eq(get("/config").get("globalWatermark"), Long.MIN_VALUE, "400 时水位未推进（原子）");

            // 合法批量仍正常工作，且失败后可重试不被误判重复。
            Map<String, Object> ok = post("/events",
                    "{\"events\":[{\"partition\":\"a\",\"eventId\":\"ok\",\"eventTime\":1}]}");
            eq(ok.get("count"), 1L, "原子失败后重试合法批量成功");
        } finally {
            tearDown();
        }
    }

    /** 错误路径：坏 JSON、未知路由、方法不允许。 */
    @TestRunner.Test
    public void errorPaths() throws Exception {
        setUp();
        try {
            int code = rawStatus("/events", "{not json");
            eq(code, 400L, "坏 JSON 返回 400");
            int code404 = rawStatusGet("/nope");
            eq(code404, 404L, "未知路径 404");
            int code405 = rawStatusGet("/events");
            eq(code405, 405L, "GET /events 返回 405");
        } finally {
            tearDown();
        }
    }

    private int rawStatus(String path, String json) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json)).build();
        return client.send(req, HttpResponse.BodyHandlers.ofString()).statusCode();
    }

    private int rawStatusGet(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString()).statusCode();
    }
}
