package topk;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * HTTP 端到端冒烟测试：进程内启动真实 {@link TopKHttpServer}（端口 0 自动分配），
 * 用 JDK 内置 java.net.http.HttpClient 打真实请求。
 *
 * 覆盖：插入/撤回/查询完整链路、并列名次、负增量、K 大于元素数、窗口滑出、
 * 幂等撤回、迟到事件、重复 ID、分组隔离、400/404/405/409/422 状态码、健康检查。
 */
public final class HttpSmokeTest {

    private static HttpClient client;

    public static void main(String[] args) throws Exception {
        TopKHttpServer srv = new TopKHttpServer(new TopKService(10_000), 0);
        srv.start();
        client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
        int port = srv.port();
        String base = "http://127.0.0.1:" + port;

        TestHarness t = new TestHarness("HTTP 端到端冒烟");
        int rc = 1;
        try {
            t.add("GET /health", () -> {
                HttpResponse<String> r = get(base + "/health");
                TestHarness.eq(r.statusCode(), 200, "health 200");
                Map<String, Object> b = Json.parseObject(r.body());
                TestHarness.eq(b.get("status"), "ok", "status=ok");
                TestHarness.eq(((Number) b.get("windowMs")).longValue(), 10_000L, "windowMs 回显");
            });

            t.add("完整链路：并列名次/负增量/K 大于元素数/滑出/幂等撤回", () -> {
                String g = "/groups/demo";
                // ts=1000 同 5 分三件，另加一件 8 分
                expectStatus(post(base + g + "/events",
                        "{\"eventId\":\"e1\",\"itemId\":\"beta\",\"delta\":5,\"ts\":1000}"), 200);
                expectStatus(post(base + g + "/events",
                        "{\"eventId\":\"e2\",\"itemId\":\"alpha\",\"delta\":5,\"ts\":1001}"), 200);
                expectStatus(post(base + g + "/events",
                        "{\"eventId\":\"e3\",\"itemId\":\"gamma\",\"delta\":5,\"ts\":1002}"), 200);
                expectStatus(post(base + g + "/events",
                        "{\"eventId\":\"e4\",\"itemId\":\"delta\",\"delta\":8,\"ts\":1003}"), 200);

                Map<String, Object> top = parse(get(base + g + "/topk?k=2&ts=2000"));
                @SuppressWarnings("unchecked")
                List<Map<String, Object>> items = (List<Map<String, Object>>) top.get("items");
                TestHarness.eq(items.size(), 2, "k=2 返回 2 行");
                TestHarness.eq(items.get(0).get("itemId"), "delta", "第 1 名 delta");
                TestHarness.eq(items.get(1).get("itemId"), "alpha", "并列按 ID：alpha 在 beta 前");

                // 负增量
                expectStatus(post(base + g + "/events",
                        "{\"eventId\":\"e5\",\"itemId\":\"alpha\",\"delta\":-9,\"ts\":2001}"), 200);
                top = parse(get(base + g + "/topk?k=100&ts=3000")); // K 大于元素数
                items = asItems(top);
                TestHarness.eq(top.get("count"), 4, "K=100 只返回全部 4 个");
                TestHarness.eq(items.get(3).get("itemId"), "alpha", "alpha 被 -9 拉到 -4 垫底");
                TestHarness.eq(((Number) items.get(3).get("score")).longValue(), -4L, "alpha 分数 -4");

                // 幂等撤回
                HttpResponse<String> r1 = post(base + g + "/retract",
                        "{\"eventId\":\"e5\",\"ts\":4000}");
                expectStatus(r1, 200);
                TestHarness.eq(Json.parseObject(r1.body()).get("status"), "RETRACTED", "首次撤回");
                HttpResponse<String> r2 = post(base + g + "/retract",
                        "{\"eventId\":\"e5\",\"ts\":4001}");
                expectStatus(r2, 200);
                TestHarness.eq(Json.parseObject(r2.body()).get("status"), "ALREADY_RETRACTED",
                        "重复撤回幂等，不二次扣减");
                top = parse(get(base + g + "/topk?k=10&ts=4002"));
                items = asItems(top);
                TestHarness.eq(items.get(1).get("itemId"), "alpha", "撤回 -9 后 alpha 回到 5 分并列区");

                // 窗口滑出：水位 11000，左边界 1000 -> e1(beta,ts=1000) 滑出
                top = parse(get(base + g + "/topk?k=10&ts=11000"));
                items = asItems(top);
                TestHarness.eq(items.size(), 3, "beta 滑出后剩 3 个 item");
                for (Map<String, Object> row : items) {
                    TestHarness.check(!"beta".equals(row.get("itemId")), "beta 已不在榜");
                }
            });

            t.add("错误码：400 缺字段/坏 JSON、400 k 非法、404 撤回未知、404 路径、405 方法、409 重复、422 迟到",
                    () -> {
                        String g = "/groups/err";
                        // 400 缺 delta
                        expectStatus(post(base + g + "/events",
                                "{\"eventId\":\"x\",\"itemId\":\"a\",\"ts\":1}"), 400);
                        // 400 坏 JSON
                        expectStatus(post(base + g + "/events", "{not json"), 400);
                        // 400 delta 非整数
                        expectStatus(post(base + g + "/events",
                                "{\"eventId\":\"x\",\"itemId\":\"a\",\"delta\":1.5,\"ts\":1}"), 400);
                        // 200 正常（水位推进到 1000，窗口左边界 = 1000-10000 = -9000）
                        expectStatus(post(base + g + "/events",
                                "{\"eventId\":\"x\",\"itemId\":\"a\",\"delta\":5,\"ts\":1000}"), 200);
                        // 409 重复
                        expectStatus(post(base + g + "/events",
                                "{\"eventId\":\"x\",\"itemId\":\"a\",\"delta\":5,\"ts\":1000}"), 409);
                        // 422 迟到：ts 恰好等于左边界 -9000（闭开边界，判为过期）
                        expectStatus(post(base + g + "/events",
                                "{\"eventId\":\"late\",\"itemId\":\"a\",\"delta\":5,\"ts\":-9000}"), 422);
                        // 404 撤回未知
                        expectStatus(post(base + g + "/retract",
                                "{\"eventId\":\"nope\",\"ts\":2000}"), 404);
                        // 400 k 缺失 / 非数字
                        expectStatus(get(base + g + "/topk"), 400);
                        expectStatus(get(base + g + "/topk?k=abc"), 400);
                        // 404 路径
                        expectStatus(get(base + "/nope"), 404);
                        // 405 方法不允许
                        expectStatus(delete(base + g + "/events"), 405);
                    });

            t.add("分组隔离：两个组的排名互不影响", () -> {
                expectStatus(post(base + "/groups/A/events",
                        "{\"eventId\":\"e1\",\"itemId\":\"x\",\"delta\":10,\"ts\":1000}"), 200);
                expectStatus(post(base + "/groups/B/events",
                        "{\"eventId\":\"e1\",\"itemId\":\"x\",\"delta\":1,\"ts\":1000}"), 200);
                Map<String, Object> a = parse(get(base + "/groups/A/topk?k=1&ts=1000"));
                Map<String, Object> b = parse(get(base + "/groups/B/topk?k=1&ts=1000"));
                TestHarness.eq(((Number) asItems(a).get(0).get("score")).longValue(), 10L, "A 组 10 分");
                TestHarness.eq(((Number) asItems(b).get(0).get("score")).longValue(), 1L, "B 组 1 分");
            });

            t.add("snapshot 返回完整排序与内部计数", () -> {
                Map<String, Object> s = parse(get(base + "/groups/demo/snapshot?ts=11000"));
                TestHarness.eq(s.get("activeItems"), 3, "snapshot activeItems");
                TestHarness.check(s.get("items") instanceof List, "snapshot 含完整排序");
            });
            // 必须在服务器仍然运行期间执行测试（不能放到 finally 之后——那时服务器已停止）
            rc = t.run();
        } finally {
            srv.stop();
        }
        System.exit(rc);
    }

    // ---------------- HTTP 辅助 ----------------

    private static HttpResponse<String> get(String url) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url)).GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> post(String url, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> delete(String url) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url)).DELETE().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static void expectStatus(HttpResponse<String> r, int expected) {
        TestHarness.eq(r.statusCode(), expected,
                "HTTP 状态码 (body=" + r.body() + ")");
    }

    private static Map<String, Object> parse(HttpResponse<String> r) {
        TestHarness.eq(r.statusCode(), 200, "HTTP 200, body=" + r.body());
        return Json.parseObject(r.body());
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> asItems(Map<String, Object> top) {
        return (List<Map<String, Object>>) top.get("items");
    }
}
