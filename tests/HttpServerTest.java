package tests;

import streammatch.json.Json;
import streammatch.model.EngineConfig;
import streammatch.model.EngineMode;
import streammatch.model.LatePolicy;
import streammatch.model.MatchPolicy;
import streammatch.service.ApiServer;
import streammatch.service.MatchingService;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

@SuppressWarnings("unchecked")
public class HttpServerTest extends TestCase {

    private ApiServer server;
    private MatchingService service;
    private HttpClient client;
    private String base;

    @Override
    protected void run() throws Exception {
        EngineConfig cfg = new EngineConfig(EngineMode.EVENT_TIME, 100,
                MatchPolicy.ALL_CANDIDATES, 0L, LatePolicy.DROP);
        service = new MatchingService(cfg);
        server = new ApiServer(service, 0, "127.0.0.1");
        server.start();
        base = "http://127.0.0.1:" + server.getPort();
        client = HttpClient.newHttpClient();
        try {
            health();
            configRoundtrip();
            fullFlow();
            badRequestStatus();
            replayEndpoint();
            watermarkEndpoint();
        } finally {
            server.stop();
            service.close();
        }
    }

    private void health() throws Exception {
        HttpResponse<String> r = get("/health");
        eq(r.statusCode(), 200, "HTTP: /health 200");
        Map<String, Object> body = (Map<String, Object>) Json.parse(r.body());
        eq(body.get("status"), "UP", "HTTP: 健康响应内容");
    }

    private void configRoundtrip() throws Exception {
        HttpResponse<String> r = get("/config");
        eq(r.statusCode(), 200, "HTTP: GET /config 200");
        check(r.body().contains("EVENT_TIME"), "HTTP: 配置含模式");

        HttpResponse<String> upd = post("/config",
                "{\"windowMillis\": 250, \"matchPolicy\": \"SKIP_PAST_LAST\"}");
        eq(upd.statusCode(), 200, "HTTP: POST /config 200");
        check(upd.body().contains("250"), "HTTP: 窗口已更新");
        check(upd.body().contains("SKIP_PAST_LAST"), "HTTP: 策略已更新");

        HttpResponse<String> bad = post("/config", "{\"mode\": \"PROCESSING_TIME\"}");
        eq(bad.statusCode(), 422, "HTTP: 改模式 -> 422");
    }

    private void fullFlow() throws Exception {
        post("/reset", "{}");
        post("/config", "{\"windowMillis\": 100, \"matchPolicy\": \"ALL_CANDIDATES\"}");

        HttpResponse<String> r1 = post("/events", """
                {"events":[
                  {"id":"A1","key":"k","type":"A","timestamp":0},
                  {"id":"A2","key":"k","type":"A","timestamp":10},
                  {"id":"B3","key":"k","type":"B","timestamp":60}
                ]}""");
        eq(r1.statusCode(), 200, "HTTP: 提交事件 200");
        List<Map<String, Object>> ms1 = matches(r1);
        eq(ms1.size(), 2, "HTTP: 一个 B 产生两个匹配");
        eq(ms1.get(0).get("aId"), "A1", "HTTP: 含 A1 匹配");
        eq(ms1.get(1).get("aId"), "A2", "HTTP: 含 A2 匹配");

        HttpResponse<String> cInterrupt = post("/events", """
                {"events":[
                  {"id":"A4","key":"k","type":"A","timestamp":200},
                  {"id":"C5","key":"k","type":"C","timestamp":230},
                  {"id":"B6","key":"k","type":"B","timestamp":280}
                ]}""");
        eq(cInterrupt.statusCode(), 200, "HTTP: C 打断序列 200");
        eq(matches(cInterrupt).size(), 0, "HTTP: A4 被 C 打断，不匹配");
        List<Map<String, Object>> removed =
                (List<Map<String, Object>>) body(cInterrupt).get("removed");
        check(removed.stream().anyMatch(x -> "INTERRUPTED_BY_C".equals(x.get("reason"))),
                "HTTP: 返回打断原因");

        HttpResponse<String> matchesResp = get("/matches");
        List<?> allMatches = (List<?>) Json.parse(matchesResp.body());
        eq(allMatches.size(), 2, "HTTP: /matches 保留历史 A1/A2 两个匹配");
    }

    private void badRequestStatus() throws Exception {
        HttpResponse<String> malformed = post("/events", "{not json");
        eq(malformed.statusCode(), 400, "HTTP: 畸形 JSON -> 400");

        HttpResponse<String> wrongType = post("/events",
                "{\"events\":[{\"id\":\"x\",\"key\":\"k\",\"type\":\"Z\",\"timestamp\":1}]}");
        eq(wrongType.statusCode(), 400, "HTTP: 非法事件类型 -> 400");

        HttpResponse<String> methodNotAllowed = client.send(
                HttpRequest.newBuilder(URI.create(base + "/state")).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        // /state 只允许 GET（这里就是 GET），再验证 POST 被拒
        eq(methodNotAllowed.statusCode(), 200, "HTTP: GET /state 200");
        HttpResponse<String> postState = post("/state", "{}");
        eq(postState.statusCode(), 400, "HTTP: POST /state -> 400");
    }

    private void replayEndpoint() throws Exception {
        post("/reset", "{\"windowMillis\":100,\"matchPolicy\":\"ALL_CANDIDATES\"}");
        // 故意乱序：先到 B，再到 A（迟到）
        post("/events", """
                {"events":[
                  {"id":"A1","key":"k","type":"A","timestamp":0},
                  {"id":"B2","key":"k","type":"B","timestamp":80}
                ]}""");
        HttpResponse<String> late = post("/events",
                "{\"events\":[{\"id\":\"A3\",\"key\":\"k\",\"type\":\"A\",\"timestamp\":20}]}");
        check(((List<?>) body(late).get("lateDropped")).contains("A3"),
                "HTTP: 迟到事件出现在 lateDropped");

        HttpResponse<String> replay = post("/replay", """
                {"events":[
                  {"id":"A1","key":"k","type":"A","timestamp":0},
                  {"id":"B2","key":"k","type":"B","timestamp":80},
                  {"id":"A3","key":"k","type":"A","timestamp":20}
                ]}""");
        eq(replay.statusCode(), 200, "HTTP: /replay 200");
        Map<String, Object> rb = body(replay);
        eq(rb.get("consistent"), Boolean.TRUE, "HTTP: 重放与参考一致");
        List<?> refMatches = (List<?>) rb.get("referenceMatches");
        eq(refMatches.size(), 2, "HTTP: 重放补算迟到 A3，共 2 个匹配");
    }

    private void watermarkEndpoint() throws Exception {
        post("/reset", "{}");
        post("/events", "{\"events\":[{\"id\":\"A1\",\"key\":\"k\",\"type\":\"A\",\"timestamp\":0}]}");
        HttpResponse<String> r = post("/watermark", "{\"watermarkMillis\":101}");
        eq(r.statusCode(), 200, "HTTP: 推进 watermark 200");
        List<Map<String, Object>> removed =
                (List<Map<String, Object>>) body(r).get("removed");
        check(removed.stream().anyMatch(x -> "TIMEOUT".equals(x.get("reason"))),
                "HTTP: watermark 推进触发超时");
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> body(HttpResponse<String> r) {
        return (Map<String, Object>) Json.parse(r.body());
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> matches(HttpResponse<String> r) {
        return (List<Map<String, Object>>) body(r).get("matches");
    }

    private HttpResponse<String> get(String path) throws Exception {
        return client.send(HttpRequest.newBuilder(URI.create(base + path)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> post(String path, String body) throws Exception {
        return client.send(HttpRequest.newBuilder(URI.create(base + path))
                        .header("Content-Type", "application/json")
                        .POST(HttpRequest.BodyPublishers.ofString(body))
                        .build(),
                HttpResponse.BodyHandlers.ofString());
    }
}
