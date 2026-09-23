package com.example.sessionwindow.tests;

import com.example.sessionwindow.json.Json;
import com.example.sessionwindow.service.HttpServerRunner;
import com.example.sessionwindow.service.SessionWindowService;
import com.example.sessionwindow.time.ManualTimerService;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

import static com.example.sessionwindow.tests.Assert.assertEquals;
import static com.example.sessionwindow.tests.Assert.assertTrue;

/** End-to-end tests against the real (JDK) HTTP server bound to a random port. */
public class HttpServerTest {

    private HttpServerRunner runner;
    private HttpClient client;
    private String base;

    private void start() throws Exception {
        SessionWindowService service = new SessionWindowService(new ManualTimerService());
        runner = new HttpServerRunner(0, service);
        runner.start();
        client = HttpClient.newHttpClient();
        base = "http://127.0.0.1:" + runner.port();
    }

    private void stop() {
        if (runner != null) {
            runner.stop();
        }
    }

    private HttpResponse<String> post(String path, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void healthRespondsUp() throws Exception {
        start();
        try {
            HttpResponse<String> resp = get("/health");
            assertEquals(200, resp.statusCode(), "health 200");
            assertTrue(resp.body().contains("UP"), "body UP");
        } finally {
            stop();
        }
    }

    @Test
    void runEndpointBridgingScenarioMatchesReference() throws Exception {
        start();
        try {
            String request = """
                    {
                      "gap": 10,
                      "allowedLateness": 20,
                      "flushAtEnd": true,
                      "items": [
                        {"key":"u1","timestamp":0},
                        {"key":"u1","timestamp":15},
                        {"watermark":25},
                        {"key":"u1","timestamp":10},
                        {"watermark":200}
                      ]
                    }
                    """;
            HttpResponse<String> resp = post("/session-windows/run", request);
            assertEquals(200, resp.statusCode(), "run 200: " + resp.body());
            Map<String, Object> body = Json.parseObject(resp.body());
            assertEquals(Boolean.TRUE, body.get("matchesOfflineReference"),
                    "reference matches");
            Map<?, ?> state = (Map<?, ?>) body.get("stateAfterFlush");
            assertEquals(Boolean.TRUE, state.get("allStateCleaned"), "state cleaned");

            @SuppressWarnings("unchecked")
            List<Map<String, Object>> records = (List<Map<String, Object>>) body.get("records");
            long retracts = records.stream().filter(r -> "RETRACT".equals(r.get("type"))).count();
            assertTrue(retracts >= 2, "retractions present: " + retracts);

            @SuppressWarnings("unchecked")
            Map<String, Object> results = (Map<String, Object>) body.get("results");
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> u1 = (List<Map<String, Object>>) results.get("u1");
            assertEquals(1, u1.size(), "u1 single bridged session");
            assertEquals(3L, u1.get(0).get("count"), "u1 count 3");
        } finally {
            stop();
        }
    }

    @Test
    void runEndpointReportsDroppedLateEvent() throws Exception {
        start();
        try {
            String request = """
                    {
                      "gap": 5,
                      "allowedLateness": 0,
                      "items": [
                        {"key":"u1","timestamp":0},
                        {"watermark":100},
                        {"key":"u1","timestamp":50}
                      ]
                    }
                    """;
            HttpResponse<String> resp = post("/session-windows/run", request);
            assertEquals(200, resp.statusCode(), "200");
            Map<String, Object> body = Json.parseObject(resp.body());
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> dropped =
                    (List<Map<String, Object>>) body.get("droppedEvents");
            assertEquals(1, dropped.size(), "one dropped event");
            assertEquals(50L, dropped.get(0).get("timestamp"), "dropped ts");
        } finally {
            stop();
        }
    }

    @Test
    void statefulPipelineCrudAndIngest() throws Exception {
        start();
        try {
            HttpResponse<String> created = post("/session-windows/pipelines/p1",
                    "{\"gap\":10,\"allowedLateness\":5,\"autoWatermark\":false}");
            assertEquals(201, created.statusCode(), "created: " + created.body());

            HttpResponse<String> ingest = post("/session-windows/pipelines/p1/events",
                    "{\"items\":[{\"key\":\"u1\",\"timestamp\":1},{\"watermark\":20}]}");
            assertEquals(200, ingest.statusCode(), "ingest 200: " + ingest.body());
            Map<String, Object> body = Json.parseObject(ingest.body());
            @SuppressWarnings("unchecked")
            List<Map<String, Object>> records = (List<Map<String, Object>>) body.get("records");
            assertTrue(records.stream().anyMatch(r -> "SEALED".equals(r.get("type"))), "sealed");
            assertTrue(records.stream().anyMatch(r -> "PURGED".equals(r.get("type"))), "purged");

            HttpResponse<String> status = get("/session-windows/pipelines/p1");
            assertEquals(200, status.statusCode(), "status 200");
            Map<String, Object> statusBody = Json.parseObject(status.body());
            assertEquals(0L, statusBody.get("retainedSessions"), "state empty in status");

            HttpResponse<String> deleted =
                    client.send(HttpRequest.newBuilder(URI.create(base + "/session-windows/pipelines/p1"))
                                    .DELETE().build(),
                            HttpResponse.BodyHandlers.ofString());
            assertEquals(200, deleted.statusCode(), "deleted");
        } finally {
            stop();
        }
    }

    @Test
    void badRequestReturns400() throws Exception {
        start();
        try {
            HttpResponse<String> resp = post("/session-windows/run",
                    "{\"gap\":-1,\"items\":[]}");
            assertEquals(400, resp.statusCode(), "invalid gap -> 400");
            assertTrue(resp.body().contains("error"), "error field");
        } finally {
            stop();
        }
    }
}
