package sessions;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

import sessions.json.Json;
import sessions.json.JsonWriter;
import sessions.service.BadRequestException;
import sessions.service.SessionHttpServer;
import sessions.service.SessionRequestRunner;
import sessions.testing.Assert;
import sessions.testing.Test;

/** 请求运行器与真实 HTTP 服务的端到端测试。 */
public final class ServiceTest {

    private static final String BRIDGE_REQUEST = """
            {
              "gap": 10,
              "allowedLateness": 20,
              "aggregate": "SUM",
              "watermarkStrategy": {"type": "NONE"},
              "input": [
                {"key": "a", "timestamp": 1, "value": 10},
                {"key": "a", "timestamp": 5, "value": 10},
                {"watermark": 15},
                {"key": "a", "timestamp": 21, "value": 100},
                {"key": "a", "timestamp": 25, "value": 100},
                {"watermark": 35},
                {"key": "a", "timestamp": 15, "value": 1000},
                {"watermark": 56}
              ],
              "finish": false
            }
            """;

    @Test("运行器：桥接剧本产生撤回+新增，清除后状态为零")
    @SuppressWarnings("unchecked")
    static void runnerBridgeScenario() {
        Map<String, Object> resp =
                SessionRequestRunner.run((Map<String, Object>) Json.parse(BRIDGE_REQUEST));

        Map<String, Object> stats = (Map<String, Object>) resp.get("stats");
        Assert.assertEquals(5L, stats.get("receivedEvents"), "5 events received");
        Assert.assertEquals(0L, stats.get("droppedLateEvents"), "no drops");
        Assert.assertEquals(0L, stats.get("activeWindowsRetained"), "no active after purge");
        Assert.assertEquals(0L, stats.get("sealedWindowsRetained"), "sealed purged at W=56");

        List<Map<String, Object>> changes = (List<Map<String, Object>>) resp.get("changelog");
        Assert.assertEquals(5, changes.size(), "ADD x2, RETRACT x2, ADD x1");
        Assert.assertEquals("ADD", changes.get(0).get("kind"), "first seal add");
        Assert.assertEquals("ADD", changes.get(1).get("kind"), "second seal add");
        Assert.assertEquals("RETRACT", changes.get(2).get("kind"), "retract window 1");
        Assert.assertEquals(20L, changes.get(2).get("aggregate"), "retracted agg=20");
        Assert.assertEquals("RETRACT", changes.get(3).get("kind"), "retract window 2");
        Assert.assertEquals("ADD", changes.get(4).get("kind"), "merged add");
        Assert.assertEquals(1L, changes.get(4).get("start"), "merged start");
        Assert.assertEquals(25L, changes.get(4).get("end"), "merged end");
        Assert.assertEquals(1220L, changes.get(4).get("aggregate"), "merged sum");

        List<Map<String, Object>> finals = (List<Map<String, Object>>) resp.get("finalResults");
        Assert.assertEquals(1, finals.size(), "single final window");
        Assert.assertEquals(1220L, finals.get(0).get("aggregate"), "final agg");
    }

    @Test("运行器：finish=true（默认）封存全部窗口")
    @SuppressWarnings("unchecked")
    static void finishDefaultSeals() {
        Map<String, Object> req = (Map<String, Object>) Json.parse("""
                {"gap": 3, "input": [
                  {"key": "x", "timestamp": 0, "value": 1},
                  {"key": "x", "timestamp": 10, "value": 2}
                ]}
                """);
        Map<String, Object> resp = SessionRequestRunner.run(req);
        List<Map<String, Object>> finals = (List<Map<String, Object>>) resp.get("finalResults");
        Assert.assertEquals(2, finals.size(), "default finish seals both sessions");
        Assert.assertEquals(1L, finals.get(0).get("aggregate"), "COUNT per window");
    }

    @Test("运行器：BOUNDED 水位线策略自动推进")
    @SuppressWarnings("unchecked")
    static void boundedStrategy() {
        Map<String, Object> req = (Map<String, Object>) Json.parse("""
                {"gap": 5, "allowedLateness": 0,
                 "watermarkStrategy": {"type": "BOUNDED", "maxOutOfOrderness": 2},
                 "input": [
                   {"key": "a", "timestamp": 10, "value": 1},
                   {"key": "a", "timestamp": 1,  "value": 1},
                   {"key": "a", "timestamp": 3,  "value": 1}
                 ]}
                """);
        Map<String, Object> resp = SessionRequestRunner.run(req);
        Map<String, Object> stats = (Map<String, Object>) resp.get("stats");
        // 第二个事件 t=1 时 W=8，门控 W-L=8：t=1 丢弃；第三个 t=3 时 W=8，丢弃。
        Assert.assertEquals(2L, stats.get("droppedLateEvents"),
                "BOUNDED W=maxSeenTs-2 drops events too far behind");
    }

    @Test("运行器：非法请求抛 BadRequestException")
    static void badRequests() {
        expectBad("{\"input\": []}", "missing gap");
        expectBad("{\"gap\": 1, \"input\": {}}", "input not array");
        expectBad("{\"gap\": -1, \"input\": []}", "negative gap");
        expectBad("{\"gap\": 1, \"aggregate\": \"AVG\", \"input\": []}", "bad aggregate");
        expectBad("{\"gap\": 1, \"input\": [{\"timestamp\": 1}]}", "missing key");
    }

    @SuppressWarnings("unchecked")
    private static void expectBad(String json, String why) {
        try {
            SessionRequestRunner.run((Map<String, Object>) Json.parse(json));
            Assert.fail("expected BadRequestException: " + why);
        } catch (BadRequestException expected) {
            // expected
        }
    }

    @Test("HTTP：/health 与 /sessions/run 端到端，错误请求返回 400")
    static void httpEndToEnd() throws IOException, InterruptedException {
        SessionHttpServer server = SessionHttpServer.start(0);
        try {
            HttpClient client = HttpClient.newHttpClient();

            HttpResponse<String> health = client.send(
                    HttpRequest.newBuilder(URI.create(
                            "http://localhost:" + server.port() + "/health")).build(),
                    java.net.http.HttpResponse.BodyHandlers.ofString());
            Assert.assertEquals(200, health.statusCode(), "health 200");
            Assert.assertEquals("ok", ((Map<?, ?>) Json.parse(health.body())).get("status"),
                    "health body");

            HttpResponse<String> ok = client.send(
                    HttpRequest.newBuilder(URI.create(
                            "http://localhost:" + server.port() + "/sessions/run"))
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.ofString(
                                    BRIDGE_REQUEST, StandardCharsets.UTF_8))
                            .build(),
                    HttpResponse.BodyHandlers.ofString());
            Assert.assertEquals(200, ok.statusCode(), "run 200");
            Map<String, Object> body = (Map<String, Object>) Json.parse(ok.body());
            List<?> finals = (List<?>) body.get("finalResults");
            Assert.assertEquals(1, finals.size(), "one merged window over HTTP");

            HttpResponse<String> bad = client.send(
                    HttpRequest.newBuilder(URI.create(
                            "http://localhost:" + server.port() + "/sessions/run"))
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.ofString("not json"))
                            .build(),
                    HttpResponse.BodyHandlers.ofString());
            Assert.assertEquals(400, bad.statusCode(), "invalid JSON -> 400");

            HttpResponse<String> wrongMethod = client.send(
                    HttpRequest.newBuilder(URI.create(
                            "http://localhost:" + server.port() + "/sessions/run"))
                            .GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            Assert.assertEquals(405, wrongMethod.statusCode(), "GET -> 405");
        } finally {
            server.stop();
        }
    }

    @Test("HTTP：响应本身是合法 JSON（生成器与解析器一致）")
    static void responseIsValidJson() throws IOException {
        SessionHttpServer server = SessionHttpServer.start(0);
        try {
            Map<String, Object> resp = SessionRequestRunner.run(
                    (Map<String, Object>) Json.parse(BRIDGE_REQUEST));
            Object reparsed = Json.parse(JsonWriter.write(resp));
            Assert.assertEquals(resp, reparsed, "response JSON round trip");
        } finally {
            server.stop();
        }
    }
}
