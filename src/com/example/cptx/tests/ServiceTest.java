package com.example.cptx.tests;

import com.example.cptx.core.Json;
import com.example.cptx.service.CheckpointHttpServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * HTTP 服务测试：真实启动 JDK HttpServer（随机端口），
 * 走网络验证“发送事件 → 注入故障 → 500 崩溃 → /recover → 最终一致”。
 */
public final class ServiceTest {

    private ServiceTest() {}

    public static void run(TestRunner t) throws Exception {
        Path dir = TestDirs.create("service");
        CheckpointHttpServer server = new CheckpointHttpServer(dir, 5);
        server.start(0);
        int port = server.getAddressPort();
        HttpClient client = HttpClient.newHttpClient();

        try {
            health(t, client, port, "RUNNING");
            sendEvents(t, client, port, 20);
            Map<String, Object> st1 = status(t, client, port);
            t.eq(st1.get("nextOffset"), 20L, "服务：20 条后 nextOffset=20");
            t.eq(st1.get("lastCheckpointId"), 4L, "服务：每 5 条 -> 4 个检查点");

            // 在第 5 个检查点（offset 25）的 OUTPUT_COMMIT 崩溃：先发 5 条触发
            armFault(t, client, port, "OUTPUT_COMMIT", 5);
            postEventsExpectCrash(t, client, port, TestFixtures.payloads(5), "OUTPUT_COMMIT");

            health(t, client, port, "CRASHED");

            // 崩溃后写请求应被拒绝（503）
            int blocked = postRaw(client, port, "/events",
                    "{\"events\":[{\"key\":\"Z\",\"value\":9.0}]}").statusCode();
            t.eq(blocked, 503, "崩溃后写入被拒绝（503）");

            // 恢复
            Map<String, Object> rec = postJson(client, port, "/recover", "{}");
            t.eq(rec.get("recovered"), true, "服务：恢复成功");
            t.eq(rec.get("resumedToOffset"), 25L, "服务：恢复重放到 25");

            Map<String, Object> st2 = status(t, client, port);
            t.eq(st2.get("nextOffset"), 25L, "服务：恢复后 nextOffset=25，无重复");
            Map<String, Object> summary = Json.obj(st2.get("summary"));
            t.eq(Json.obj(summary.get("A")).get("count"), TestFixtures.expectedCount(25, "A"),
                    "服务：恢复后 A 的条数正确");
            t.check(!summary.containsKey("Z"), "崩溃后被拒绝的 Z 未进入系统");

            // 恢复后继续追加 12 条并尾部 flush，全部 37 条等价于基线
            sendEvents(t, client, port, 12);
            postJson(client, port, "/checkpoint", "{}");
            Map<String, Object> st3 = status(t, client, port);
            t.eq(st3.get("nextOffset"), 37L, "服务：最终 nextOffset=37");
            t.eq(st3.get("lastCheckpointId"), 8L, "服务：最终 8 个检查点");
            t.eq(Json.obj(Json.obj(st3.get("summary")).get("A")).get("count"),
                    TestFixtures.expectedCount(37, "A"), "服务：最终 A 条数");

            // 非法输入 400
            HttpResponse<String> bad = postRaw(client, port, "/events", "{\"events\":[]}");
            t.eq(bad.statusCode(), 400, "空 events 返回 400");
            HttpResponse<String> badJson = postRaw(client, port, "/events", "not-json");
            t.eq(badJson.statusCode(), 400, "非法 JSON 返回 400");
        } finally {
            server.stop();
        }
    }

    private static void health(TestRunner t, HttpClient c, int port, String expect) throws Exception {
        HttpResponse<String> r = get(c, port, "/healthz");
        Map<String, Object> b = Json.obj(Json.parse(r.body()));
        t.eq(b.get("state"), expect, "/healthz state=" + expect);
    }

    private static void sendEvents(TestRunner t, HttpClient c, int port, int n) throws Exception {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("events", TestFixtures.payloads(n));
        HttpResponse<String> r = postRaw(c, port, "/events", Json.write(req));
        t.eq(r.statusCode(), 200, "POST /events 200（" + n + " 条）");
    }

    private static void postEventsExpectCrash(TestRunner t, HttpClient c, int port,
                                              List<Map<String, Object>> events, String phase) throws Exception {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("events", events);
        HttpResponse<String> r = postRaw(c, port, "/events", Json.write(req));
        t.eq(r.statusCode(), 500, "注入故障返回 500");
        Map<String, Object> b = Json.obj(Json.parse(r.body()));
        t.eq(b.get("phase"), phase, "崩溃阶段=" + phase);
    }

    private static Map<String, Object> status(TestRunner t, HttpClient c, int port) throws Exception {
        HttpResponse<String> r = get(c, port, "/status");
        t.eq(r.statusCode(), 200, "GET /status 200");
        return Json.obj(Json.parse(r.body()));
    }

    private static void armFault(TestRunner t, HttpClient c, int port, String phase, long id) throws Exception {
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("phase", phase);
        req.put("checkpointId", id);
        HttpResponse<String> r = postRaw(c, port, "/faults", Json.write(req));
        t.eq(r.statusCode(), 200, "POST /faults 200");
    }

    private static HttpResponse<String> get(HttpClient c, int port, String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path)).GET().build();
        return c.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static Map<String, Object> postJson(HttpClient c, int port, String path, String body) throws Exception {
        return Json.obj(Json.parse(postRaw(c, port, path, body).body()));
    }

    private static HttpResponse<String> postRaw(HttpClient c, int port, String path, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        return c.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }
}
