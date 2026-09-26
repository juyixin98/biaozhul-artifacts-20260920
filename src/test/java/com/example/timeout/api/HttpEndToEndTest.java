package com.example.timeout.api;

import com.example.timeout.service.ServiceManager;
import com.example.timeout.store.JsonMappers;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Path;
import java.time.Duration;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 端到端验收测试：真实启动 HTTP 服务 + 真实 HTTP 调用。
 * 覆盖：向前/向后校时、单调计时到期、已过期截止、重启后按墙钟重新计算、输入校验、tzdb 上报。
 */
class HttpEndToEndTest {

    private static final String START_WALL = "2026-09-25T10:00:00Z";

    @TempDir
    Path dir;

    private TimeoutHttpServer server;
    private HttpClient http;
    private final ObjectMapper mapper = JsonMappers.mapper();
    private String base;

    @BeforeEach
    void setUp() throws Exception {
        ServiceManager manager = ServiceManager.virtual(
                java.time.Instant.parse(START_WALL), dir.resolve("e2e.json"));
        server = new TimeoutHttpServer(manager, 0);
        server.start();
        base = "http://localhost:" + server.port();
        http = HttpClient.newHttpClient();
    }

    @AfterEach
    void tearDown() {
        server.stop();
    }

    @Test
    void acceptanceScenarioForwardBackwardSteeringExpiryAndRestart() throws Exception {
        // 1) 安排一个 600 秒（单调计时）超时
        post("/timeouts", """
                {"id":"job","label":"e2e job","rule":{"type":"duration","seconds":600}}
                """);
        JsonNode job = get("/timeouts/job").get("data");
        assertEquals("SCHEDULED", job.get("status").asText());
        assertEquals(600_000_000_000L, job.get("remainingMonotonicNanos").asLong());

        // 2) 向前校时 3 小时：墙钟跳变，超时不受影响，仍 SCHEDULED、剩余仍 600s
        post("/clock/set-wall", "{\"wall\":\"2026-09-25T13:00:00Z\"}");
        job = get("/timeouts/job").get("data");
        assertEquals("SCHEDULED", job.get("status").asText());
        assertEquals(600_000_000_000L, job.get("remainingMonotonicNanos").asLong());

        // 3) 向后校时 5 小时：依旧不改变已安排超时
        post("/clock/advance-wall", "{\"seconds\":-18000}");
        job = get("/timeouts/job").get("data");
        assertEquals("SCHEDULED", job.get("status").asText());
        assertEquals(600_000_000_000L, job.get("remainingMonotonicNanos").asLong());

        // 4) 真实流逝 599 秒：未到期；再 1 秒：到期（只由单调钟决定）
        post("/clock/tick", "{\"seconds\":599}");
        assertEquals("SCHEDULED", get("/timeouts/job").get("data").get("status").asText());
        post("/clock/tick", "{\"seconds\":1}");
        assertEquals("EXPIRED", get("/timeouts/job").get("data").get("status").asText());
    }

    @Test
    void schedulingWithPastDeadlineIsImmediatelyExpired() throws Exception {
        post("/timeouts", """
                {"id":"past","rule":{"type":"absolute","deadline":"2026-09-25T08:00:00Z"}}
                """);
        JsonNode past = get("/timeouts/past").get("data");
        assertEquals("EXPIRED", past.get("status").asText());
        // 转换时算出的剩余是负 2 小时，保留迟到信息
        assertEquals(-Duration.ofHours(2).toNanos(),
                past.get("remainingAtConversionNanos").asLong());
    }

    @Test
    void restartRecomputesFromPersistedWallDeadlineAndResetsMonotonic() throws Exception {
        // 安排 1 小时超时，单调钟走 10 分钟后“进程重启”
        post("/timeouts", """
                {"id":"r","rule":{"type":"duration","seconds":3600}}
                """);
        post("/clock/tick", "{\"seconds\":600}");
        JsonNode before = get("/timeouts/r").get("data");
        assertEquals(3000_000_000_000L, before.get("remainingMonotonicNanos").asLong());

        // 重启瞬间墙钟被 NTP 向前拨到 12:00（截止 11:00 已过）：必须重新计算并判为过期
        JsonNode restart = post("/admin/restart",
                "{\"wallNow\":\"2026-09-25T12:00:00Z\"}").get("data");
        assertEquals(2, restart.get("generation").asInt());
        assertEquals(0L, restart.get("monoNowNanos").asLong(), "monotonic clock must reset");
        JsonNode job = restart.get("timeouts").get(0);
        assertEquals("r", job.get("id").asText());
        assertEquals("EXPIRED", job.get("status").asText());
    }

    @Test
    void restartBeforeDeadlineRecomputesFreshRemaining() throws Exception {
        post("/timeouts", """
                {"id":"f","rule":{"type":"duration","seconds":600}}
                """);
        post("/clock/tick", "{\"seconds\":100}");
        // 正常重启（墙钟也只前进 100 秒）：剩余按墙钟重算为 500 秒
        JsonNode restart = post("/admin/restart",
                "{\"wallNow\":\"2026-09-25T10:01:40Z\"}").get("data");
        JsonNode f = restart.get("timeouts").get(0);
        assertEquals("SCHEDULED", f.get("status").asText());
        assertEquals(500_000_000_000L, f.get("remainingMonotonicNanos").asLong());
    }

    @Test
    void versionTtlRuleIsDeterminedByVersionTimestamp() throws Exception {
        // 版本 10:00 发布、TTL 15 分钟；即使 10:10 才安排，截止仍是 10:15
        post("/timeouts", """
                {"id":"v","rule":{"type":"versionTtl","issuedAt":"2026-09-25T10:00:00Z","ttlSeconds":900}}
                """);
        JsonNode v = get("/timeouts/v").get("data");
        assertEquals("2026-09-25T10:15:00Z", v.get("deadlineWall").asText());
        assertEquals("SCHEDULED", v.get("status").asText());
    }

    @Test
    void invalidInputReturnsEnvelopeErrorAndInfoReportsTzdb() throws Exception {
        HttpResponse<String> bad = rawPost("/timeouts", "{\"id\":\"\"}");
        assertEquals(400, bad.statusCode());
        assertTrue(bad.body().contains("\"success\" : false"));
        assertTrue(bad.body().contains("rule is required"));

        JsonNode info = get("/info").get("data");
        assertTrue(info.get("tzdbVersion").asText().matches("20\\d{2}[a-z]"));
        assertNotNull(info.get("javaVersion").asText());
    }

    private JsonNode get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(java.net.URI.create(base + path)).GET().build();
        HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        assertEquals(200, resp.statusCode(), "GET " + path + " -> " + resp.body());
        return mapper.readTree(resp.body());
    }

    private JsonNode post(String path, String json) throws Exception {
        HttpResponse<String> resp = rawPost(path, json);
        assertTrue(resp.statusCode() >= 200 && resp.statusCode() < 300,
                "POST " + path + " -> " + resp.statusCode() + ": " + resp.body());
        return mapper.readTree(resp.body());
    }

    private HttpResponse<String> rawPost(String path, String json) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(java.net.URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
