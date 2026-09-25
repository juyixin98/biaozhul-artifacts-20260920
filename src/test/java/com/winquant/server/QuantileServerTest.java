package com.winquant.server;

import com.winquant.ManualClock;
import com.winquant.json.Json;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

/** 使用 ManualClock 的服务端到端测试，覆盖空窗口、过期、批量提交。 */
class QuantileServerTest {

    private QuantileServer server;
    private HttpClient client;
    private int port;

    @BeforeEach
    void setUp() throws IOException {
        server = new QuantileServer(0, 10, new ManualClock(100)); // 窗口 (now-10, now]
        server.start();
        port = server.port();
        client = HttpClient.newHttpClient();
    }

    @AfterEach
    void tearDown() {
        server.stop();
    }

    @Test
    void emptyWindowThenEventsThenExpiry() throws Exception {
        // 初始空窗口
        Map<String, Object> q0 = get("/median");
        assertEquals(0L, ((Number) q0.get("count")).longValue());
        assertNull(q0.get("value"));

        // 单个事件
        Map<String, Object> r1 = post("/events", "{\"timestamp\":100,\"value\":2}");
        assertEquals(1L, ((Number) r1.get("accepted")).longValue());
        assertEquals(0L, ((Number) r1.get("late")).longValue());

        // 批量事件（含同时间戳、负值）
        Map<String, Object> r2 = post("/events",
                "{\"events\":[{\"timestamp\":100,\"value\":4},{\"timestamp\":95,\"value\":-10}]}");
        assertEquals(2L, ((Number) r2.get("accepted")).longValue());

        // 窗口 (90,100]：{-10,2,4}，median=2
        Map<String, Object> q1 = get("/median");
        assertEquals(3L, ((Number) q1.get("count")).longValue());
        assertEquals(2.0, ((Number) q1.get("value")).doubleValue());

        // q=0 与 q=1
        assertEquals(-10.0, ((Number) get("/quantile?q=0").get("value")).doubleValue());
        assertEquals(4.0, ((Number) get("/quantile?q=1").get("value")).doubleValue());

        // 推进时钟到 105：(95,105]，ts=95 过期 → {2,4}，median=3
        Map<String, Object> adv = post("/advance", "{\"now\":105}");
        assertEquals(105L, ((Number) adv.get("now")).longValue());
        Map<String, Object> q2 = get("/median");
        assertEquals(2L, ((Number) q2.get("count")).longValue());
        assertEquals(3.0, ((Number) q2.get("value")).doubleValue());

        // 迟到事件被拒绝
        Map<String, Object> r3 = post("/events", "{\"timestamp\":90,\"value\":99}");
        assertEquals(0L, ((Number) r3.get("accepted")).longValue());
        assertEquals(1L, ((Number) r3.get("late")).longValue());

        // stats
        Map<String, Object> stats = get("/stats");
        assertEquals(105L, ((Number) stats.get("now")).longValue());
        assertEquals(95L, ((Number) stats.get("windowStart")).longValue());
        assertEquals(1L, ((Number) stats.get("lateEvents")).longValue());

        // health
        assertEquals(200, client.send(
                HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/health")).GET().build(),
                HttpResponse.BodyHandlers.ofString()).statusCode());
    }

    @Test
    void invalidRequestsReturn400() throws Exception {
        assertEquals(400, postRaw("/events", "not-json").statusCode());
        assertEquals(400, client.send(
                HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/events"))
                        .POST(HttpRequest.BodyPublishers.ofString("{\"timestamp\":1}"))
                        .header("Content-Type", "application/json").build(),
                HttpResponse.BodyHandlers.ofString()).statusCode());
        assertEquals(400, client.send(
                HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/quantile?q=5"))
                        .GET().build(),
                HttpResponse.BodyHandlers.ofString()).statusCode());
    }

    @Test
    void quantilesArrayShapeNotRequired() throws Exception {
        // 全重复值：任意分位都是同一个值
        post("/events", "{\"events\":["
                + "{\"timestamp\":100,\"value\":7},{\"timestamp\":100,\"value\":7},"
                + "{\"timestamp\":100,\"value\":7},{\"timestamp\":100,\"value\":7}]}");
        for (double q : List.of(0.1, 0.5, 0.9)) {
            assertEquals(7.0, ((Number) get("/quantile?q=" + q).get("value")).doubleValue());
        }
    }

    private Map<String, Object> get(String path) throws IOException, InterruptedException {
        HttpResponse<String> resp = client.send(
                HttpRequest.newBuilder(URI.create("http://localhost:" + port + path)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        assertEquals(200, resp.statusCode(), resp.body());
        return Json.parseObject(resp.body());
    }

    private Map<String, Object> post(String path, String body) throws IOException, InterruptedException {
        HttpResponse<String> resp = postRaw(path, body);
        assertEquals(200, resp.statusCode(), resp.body());
        return Json.parseObject(resp.body());
    }

    private HttpResponse<String> postRaw(String path, String body) throws IOException, InterruptedException {
        return client.send(
                HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                        .POST(HttpRequest.BodyPublishers.ofString(body))
                        .header("Content-Type", "application/json").build(),
                HttpResponse.BodyHandlers.ofString());
    }
}
