package incagg.web;

import com.sun.net.httpserver.HttpServer;
import incagg.store.IncrementalViewStore;

import java.math.BigDecimal;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static incagg.TestRunner.*;

/**
 * HTTP 端到端冒烟：在临时端口启动真实 HttpServer，用 JDK HttpClient 打请求。
 * 覆盖：健康检查、单事件/批量、去重、分类变更、删除不存在、diff、400/404/405。
 */
public final class HttpSmokeTest {

    private static String base;
    private static HttpClient client;

    public static void main(String[] args) throws Exception {
        IncrementalViewStore store = new IncrementalViewStore();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", new ApiHandler(store));
        server.start();
        int port = server.getAddress().getPort();
        base = "http://127.0.0.1:" + port;
        client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
        System.out.println("== HTTP 冒烟测试（临时端口 " + port + "） ==");

        try {
            check("GET /health -> 200 UP", () -> {
                HttpResponse<String> r = get("/health");
                assertEquals(200, r.statusCode(), "status");
                assertTrue(r.body().contains("\"UP\""), "body: " + r.body());
            });

            check("POST /admin/reset -> RESET", () ->
                    assertEquals(200, post("/admin/reset", "").statusCode(), "status"));

            check("插入商品 1=BOOKS", () -> {
                HttpResponse<String> r = post("/events",
                        """
                        {"eventId":"h1","type":"PRODUCT_UPSERT","productId":1,"category":"BOOKS"}
                        """);
                assertEquals(200, r.statusCode(), "status");
                assertTrue(r.body().contains("\"status\":\"APPLIED\""), r.body());
            });

            check("批量插入两条订单行 + 1 条重复事件", () -> {
                HttpResponse<String> r = post("/events/batch", """
                        {"events":[
                          {"eventId":"h2","type":"ORDER_UPSERT","orderLineId":101,"productId":1,"qty":2,"amount":10.00},
                          {"eventId":"h3","type":"ORDER_UPSERT","orderLineId":102,"productId":1,"qty":3,"amount":5.50},
                          {"eventId":"h2","type":"ORDER_UPSERT","orderLineId":101,"productId":1,"qty":2,"amount":10.00}
                        ]}
                        """);
                assertEquals(200, r.statusCode(), "status");
                // 前两条 APPLIED，第三条 DUPLICATE；视图 BOOKS qty=5 amount=15.50
                assertTrue(r.body().contains("BOOKS"), r.body());
                String[] lines = r.body().split("\"status\"");
                long applied = count(r.body(), "\"status\":\"APPLIED\"");
                long dup = count(r.body(), "\"status\":\"DUPLICATE\"");
                assertEquals(2, applied, "APPLIED 次数");
                assertEquals(1, dup, "DUPLICATE 次数");
            });

            check("GET /view：BOOKS qty=5 amount=15.50（定点金额）", () -> {
                HttpResponse<String> r = get("/view");
                assertEquals(200, r.statusCode(), "status");
                assertTrue(r.body().contains("\"qty\":5"), r.body());
                assertTrue(r.body().contains("\"amount\":15.50"), r.body());
            });

            check("维表分类变更 BOOKS -> MEDIA", () -> {
                HttpResponse<String> r = post("/events",
                        """
                        {"eventId":"h4","type":"PRODUCT_UPSERT","productId":1,"category":"MEDIA"}
                        """);
                assertEquals(200, r.statusCode(), "status");
                assertTrue(r.body().contains("迁移数量 5"), r.body());
            });

            check("删除不存在的订单行 -> APPLIED 且 ignored", () -> {
                HttpResponse<String> r = post("/events",
                        """
                        {"eventId":"h5","type":"ORDER_DELETE","orderLineId":999}
                        """);
                assertEquals(200, r.statusCode(), "status");
                assertTrue(r.body().contains("\"ignored\":true"), r.body());
                assertTrue(r.body().contains("空操作"), r.body());
            });

            check("GET /view/diff：增量与全量重算一致", () -> {
                HttpResponse<String> r = get("/view/diff");
                assertEquals(200, r.statusCode(), "status");
                assertTrue(r.body().contains("\"consistent\":true"), r.body());
            });

            check("GET /view/recompute：全量重算同为 MEDIA 15.50", () -> {
                HttpResponse<String> r = get("/view/recompute");
                assertTrue(r.body().contains("MEDIA"), r.body());
                assertTrue(r.body().contains("15.50"), r.body());
            });

            check("非法 type -> 400 且状态不变", () -> {
                HttpResponse<String> r = post("/events",
                        """
                        {"eventId":"bad","type":"NOPE","productId":1}
                        """);
                assertEquals(400, r.statusCode(), "status");
                assertTrue(r.body().contains("未知 type"), r.body());
            });

            check("缺字段 -> 400", () -> {
                HttpResponse<String> r = post("/events",
                        """
                        {"eventId":"bad2","type":"ORDER_UPSERT","orderLineId":1}
                        """);
                assertEquals(400, r.statusCode(), "status");
            });

            check("畸形 JSON -> 400", () ->
                    assertEquals(400, post("/events", "{not json").statusCode(), "status"));

            check("未知路径 -> 404", () ->
                    assertEquals(404, get("/nope").statusCode(), "status"));

            check("方法不允许 DELETE /view -> 405", () -> {
                HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/view"))
                        .DELETE().build();
                HttpResponse<String> r = client.send(req, HttpResponse.BodyHandlers.ofString());
                assertEquals(405, r.statusCode(), "status");
            });

            check("错误后视图未被污染，diff 仍一致", () -> {
                HttpResponse<String> r = get("/view/diff");
                assertTrue(r.body().contains("\"consistent\":true"), r.body());
            });
        } finally {
            server.stop(0);
        }

        System.exit(finish());
    }

    // ---------------------------------------------------------------- 工具

    private static HttpResponse<String> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .timeout(Duration.ofSeconds(5)).GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static HttpResponse<String> post(String path, String body) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + path))
                .timeout(Duration.ofSeconds(5));
        if (body.isBlank()) b.POST(HttpRequest.BodyPublishers.noBody());
        else b.header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body));
        return client.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    private static long count(String haystack, String needle) {
        long n = 0;
        int idx = 0;
        while ((idx = haystack.indexOf(needle, idx)) != -1) {
            n++;
            idx += needle.length();
        }
        return n;
    }
}
