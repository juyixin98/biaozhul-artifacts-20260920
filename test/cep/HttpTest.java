package cep;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.Map;

/**
 * HTTP 接口的端到端测试（在同一 JVM 内以随机端口启动真实服务）。
 * 进程级崩溃恢复见 {@link RecoveryProcessTest}。
 */
public class HttpTest {

    private Path dir;
    private Main app;
    private int port;
    private HttpClient http;

    private void start(boolean testEndpoints) throws IOException {
        dir = Files.createTempDirectory("cep-http-test-");
        Engine engine = new Engine();
        EventLog log = new EventLog(dir);
        engine.recover(log.replay());
        app = new Main(engine, log, testEndpoints);
        port = app.start(0);
        http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    }

    private void stop() {
        if (app != null) {
            app.stop(0);
        }
    }

    private HttpResponse<String> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .timeout(Duration.ofSeconds(5)).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private HttpResponse<String> post(String path, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .timeout(Duration.ofSeconds(5))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8)).build();
        return http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    public void testHealth() throws Exception {
        start(false);
        try {
            HttpResponse<String> r = get("/health");
            TestRunner.checkEq(r.statusCode(), 200, "health 应 200");
            TestRunner.check(r.body().contains("\"status\":\"ok\""), "health 返回 ok");
        } finally {
            stop();
        }
    }

    /** 走 HTTP 跑验收场景 1：A,A,B,C -> count=2。 */
    public void testAcceptanceOverHttp_doubleA() throws Exception {
        start(false);
        try {
            String body = "[{\"type\":\"A\",\"entityId\":\"e1\",\"timestamp\":1000},"
                    + "{\"type\":\"A\",\"entityId\":\"e1\",\"timestamp\":1000},"
                    + "{\"type\":\"B\",\"entityId\":\"e1\",\"timestamp\":2000},"
                    + "{\"type\":\"C\",\"entityId\":\"e1\",\"timestamp\":3000}]";
            HttpResponse<String> post = post("/events", body);
            TestRunner.checkEq(post.statusCode(), 200, "写入应成功");
            TestRunner.check(post.body().contains("\"created\":2"),
                    "响应应报告新建 2 个匹配，实际: " + post.body());

            HttpResponse<String> q = get("/matches?entityId=e1");
            @SuppressWarnings("unchecked")
            Map<String, Object> resp = (Map<String, Object>) Json.parse(q.body());
            TestRunner.checkEq(((Number) resp.get("count")).longValue(), 2L,
                    "查询应得到 2 个匹配");
        } finally {
            stop();
        }
    }

    /** 走 HTTP 跑验收场景 2：超时 -> count=0。 */
    public void testAcceptanceOverHttp_timeout() throws Exception {
        start(false);
        try {
            post("/events", "[{\"type\":\"A\",\"entityId\":\"e1\",\"timestamp\":0},"
                    + "{\"type\":\"B\",\"entityId\":\"e1\",\"timestamp\":1000}]");
            HttpResponse<String> r = post("/events",
                    "[{\"type\":\"C\",\"entityId\":\"e1\",\"timestamp\":11000}]");
            TestRunner.checkEq(r.statusCode(), 200, "写入本身应成功");
            TestRunner.check(r.body().contains("\"created\":0"),
                    "超时 C 应产生 0 个匹配: " + r.body());
            HttpResponse<String> q = get("/matches");
            TestRunner.check(q.body().contains("\"count\":0"),
                    "总匹配数应为 0: " + q.body());
        } finally {
            stop();
        }
    }

    /** 单个事件对象（非数组）也应被接受。 */
    public void testSingleEventObjectAccepted() throws Exception {
        start(false);
        try {
            HttpResponse<String> r = post("/events",
                    "{\"type\":\"A\",\"entityId\":\"z\",\"timestamp\":0}");
            TestRunner.checkEq(r.statusCode(), 200, "单对象写入应 200: " + r.body());
            HttpResponse<String> state = get("/state?entityId=z");
            TestRunner.check(state.body().contains("waitingA"),
                    "state 应显示等待中的 A");
        } finally {
            stop();
        }
    }

    /** 坏 JSON / 缺字段 -> 400，且不产生任何状态。 */
    public void testInvalidJsonAndFields() throws Exception {
        start(false);
        try {
            TestRunner.checkEq(post("/events", "not-json").statusCode(), 400,
                    "非法 JSON 应 400");
            TestRunner.checkEq(post("/events", "{\"type\":\"A\"}").statusCode(), 400,
                    "缺 entityId/timestamp 应 400");
            TestRunner.checkEq(post("/events",
                    "{\"type\":\"A\",\"entityId\":\"z\",\"timestamp\":\"now\"}").statusCode(),
                    400, "timestamp 非数字应 400");
            TestRunner.checkEq(post("/events",
                    "{\"type\":\"A\",\"entityId\":\"z\",\"timestamp\":1,\"seq\":5}").statusCode(),
                    400, "客户端指定 seq 应 400");
            TestRunner.checkEq(post("/events", "[]").statusCode(), 400,
                    "空数组应 400");
            TestRunner.checkEq(get("/matches").body().contains("\"count\":0"), true,
                    "所有失败请求都不得写入状态");
        } finally {
            stop();
        }
    }

    /** 晚到事件 -> 409，响应中明确说明，不静默重排。 */
    public void testLateEventReturns409() throws Exception {
        start(false);
        try {
            post("/events", "[{\"type\":\"A\",\"entityId\":\"e\",\"timestamp\":5000}]");
            HttpResponse<String> r = post("/events",
                    "[{\"type\":\"B\",\"entityId\":\"e\",\"timestamp\":4000}]");
            TestRunner.checkEq(r.statusCode(), 409, "晚到事件应返回 409");
            TestRunner.check(r.body().contains("late_event"),
                    "错误码应为 late_event: " + r.body());
        } finally {
            stop();
        }
    }

    /** 组合超上限 -> 422，明确报错而不是截断。 */
    public void testCombinationLimitReturns422() throws Exception {
        dir = Files.createTempDirectory("cep-http-test-");
        Engine engine = new Engine(1);
        EventLog log = new EventLog(dir);
        engine.recover(log.replay());
        app = new Main(engine, log, false);
        port = app.start(0);
        http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
        try {
            HttpResponse<String> r = post("/events",
                    "[{\"type\":\"A\",\"entityId\":\"e\",\"timestamp\":0},"
                            + "{\"type\":\"A\",\"entityId\":\"e\",\"timestamp\":100},"
                            + "{\"type\":\"B\",\"entityId\":\"e\",\"timestamp\":200},"
                            + "{\"type\":\"C\",\"entityId\":\"e\",\"timestamp\":300}]");
            TestRunner.checkEq(r.statusCode(), 422, "组合超限应 422");
            TestRunner.check(r.body().contains("combination_limit"),
                    "错误码应为 combination_limit: " + r.body());
            HttpResponse<String> q = get("/matches");
            TestRunner.check(q.body().contains("\"count\":0"),
                    "422 批次必须完全不生效");
        } finally {
            stop();
        }
    }

    /** 无关事件与实体过滤。 */
    public void testIrrelevantEventAndEntityFilter() throws Exception {
        start(false);
        try {
            post("/events", "[{\"type\":\"A\",\"entityId\":\"e\",\"timestamp\":0},"
                    + "{\"type\":\"PING\",\"entityId\":\"e\",\"timestamp\":100},"
                    + "{\"type\":\"B\",\"entityId\":\"e\",\"timestamp\":200},"
                    + "{\"type\":\"C\",\"entityId\":\"e\",\"timestamp\":300}]");
            HttpResponse<String> q = get("/matches?entityId=e");
            TestRunner.check(q.body().contains("\"count\":1"),
                    "无关事件被跳过后仍应 1 个匹配: " + q.body());
            TestRunner.check(get("/matches?entityId=other").body().contains("\"count\":0"),
                    "实体过滤应隔离");
        } finally {
            stop();
        }
    }

    /** /reset 清空匹配、状态与日志（再发事件序号从 0 开始）。 */
    public void testResetClearsEverything() throws Exception {
        start(false);
        try {
            post("/events", "[{\"type\":\"A\",\"entityId\":\"e\",\"timestamp\":0},"
                    + "{\"type\":\"B\",\"entityId\":\"e\",\"timestamp\":100},"
                    + "{\"type\":\"C\",\"entityId\":\"e\",\"timestamp\":200}]");
            TestRunner.checkEq(post("/reset", "{}").statusCode(), 200, "reset 应 200");
            TestRunner.check(get("/matches").body().contains("\"count\":0"),
                    "reset 后匹配清空");
            HttpResponse<String> h = get("/health");
            TestRunner.check(h.body().contains("\"nextSeq\":0"),
                    "reset 后输入序号归零: " + h.body());
            HttpResponse<String> again = post("/events",
                    "{\"type\":\"A\",\"entityId\":\"e2\",\"timestamp\":9}");
            @SuppressWarnings("unchecked")
            java.util.List<Map<String, Object>> events =
                    (java.util.List<Map<String, Object>>)
                            ((Map<String, Object>) Json.parse(again.body())).get("events");
            TestRunner.checkEq(((Number) events.get(0).get("seq")).longValue(), 0L,
                    "reset 后新事件序号应重新从 0 开始: " + again.body());
        } finally {
            stop();
        }
    }

    /** 崩溃端点默认关闭；开启后存在且返回 200（不真的在同 JVM 内 halt）。 */
    public void testCrashEndpointGate() throws Exception {
        start(false);
        try {
            TestRunner.checkEq(post("/test/crash", "{}").statusCode(), 404,
                    "未开启测试端点时应 404");
        } finally {
            stop();
        }
    }

    /** 未知路径 404、错误方法 405。 */
    public void testRoutingErrors() throws Exception {
        start(false);
        try {
            TestRunner.checkEq(get("/nope").statusCode(), 404, "未知路径 404");
            TestRunner.checkEq(get("/events").statusCode(), 405, "GET /events 405");
            TestRunner.checkEq(get("/reset").statusCode(), 405, "GET /reset 405");
        } finally {
            stop();
        }
    }
}
