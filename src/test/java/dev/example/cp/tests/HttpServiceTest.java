package dev.example.cp.tests;

import dev.example.cp.engine.Engine;
import dev.example.cp.svc.HttpService;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Path;
import java.time.Duration;
import java.util.Map;

import dev.example.cp.json.Json;

public class HttpServiceTest extends TestCase {

    private HttpService service;
    private int port;
    private final HttpClient client = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(2)).build();

    public HttpServiceTest() {
        super("http/json service: append, drain, checkpoint, status, fault+recover");
    }

    @Override
    protected void run() throws Exception {
        Path dir = newDataDir();
        // 端口 0：由操作系统分配空闲端口，避免与残留进程冲突导致测试挂起
        service = new HttpService(dir, 0);
        service.start();
        port = service.boundPort();
        try {
            // 追加 6 条事件
            for (int i = 0; i < 6; i++) {
                String key = new String[]{"a", "b", "c"}[i % 3];
                post("/events", Map.of("key", key, "value", i + 1), 200);
            }
            // 排空（会自动补一个检查点）
            post("/drain", Map.of(), 200);
            Map<String, Object> status1 = getStatus();
            assertEquals(5L, ((Number) status1.get("committedOffset")).longValue(), "6 events committed");
            assertEquals(9L, ((Number) ((Map<?, ?>) status1.get("sums")).get("c")).longValue(), "c=3+6=9");

            // 在 epoch 2 的状态写入点注入故障（先追加事件，再造障，再 drain）
            post("/events", Map.of("key", "a", "value", 100), 200);
            Map<String, Object> armed = post("/faults",
                    Map.of("at", "STATE_WRITE", "epoch", 2, "halt", false), 200);
            assertEquals(true, armed.get("armed"), "fault armed");

            // drain 时崩溃：服务返回 500 crashed
            int code = rawPost("/drain", "{}");
            assertEquals(500, code, "request reports injected crash");

            // 下一次请求 = 进程重启后的第一个请求：引擎重新 open 并恢复，epoch 2 作废，重放后成功
            post("/drain", Map.of(), 200);
            Map<String, Object> status2 = getStatus();
            assertEquals(6L, ((Number) status2.get("committedOffset")).longValue(),
                    "recovered: final offset 6");
            Map<?, ?> sums = (Map<?, ?>) status2.get("sums");
            assertEquals(105L, ((Number) sums.get("a")).longValue(), "a=1+4+100=105 after recovery");
            assertEquals(2L, ((Number) status2.get("appliedEpoch")).longValue(), "applied epoch 2");

            // 显式检查点（没有未提交数据时是 no-op 仍推进 epoch？不应推进）
            long before = ((Number) status2.get("latestEpoch")).longValue();
            Map<String, Object> cp = post("/checkpoints", Map.of(), 200);
            assertEquals(before, ((Number) cp.get("epoch")).longValue(),
                    "empty checkpoint does not advance epoch");
        } finally {
            service.stop();
        }
    }

    private Map<String, Object> post(String path, Map<String, Object> body, int expectCode) throws Exception {
        int code = rawPostCode(path, Json.write(body));
        assertEquals(expectCode, code, "HTTP code for POST " + path);
        return lastJson;
    }

    private int rawPost(String path, String body) throws Exception {
        return rawPostCode(path, body);
    }

    private Map<String, Object> lastJson;

    private int rawPostCode(String path, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .timeout(Duration.ofSeconds(5))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        lastJson = Json.parseObject(resp.body());
        return resp.statusCode();
    }

    private Map<String, Object> getStatus() throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/status"))
                .timeout(Duration.ofSeconds(5)).GET().build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        assertEquals(200, resp.statusCode(), "GET /status code");
        return Json.parseObject(resp.body());
    }
}
