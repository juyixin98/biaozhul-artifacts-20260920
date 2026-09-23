package com.example.watermark.test;

import static com.example.watermark.test.Assert.assertEquals;
import static com.example.watermark.test.Assert.assertTrue;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

import com.example.watermark.json.Json;
import com.example.watermark.service.WatermarkHttpServer;
import com.example.watermark.test.TestRunner.Test;

/** Exercises the real HTTP surface on an ephemeral port. */
public class HttpServiceTest {

    private final HttpClient client = HttpClient.newHttpClient();

    private HttpResponse<String> post(String url, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @Test
    public void healthRunAndSessionLifecycle() throws Exception {
        try (WatermarkHttpServer server = new WatermarkHttpServer(0)) {
            server.start();
            int port = server.getPort();
            String base = "http://127.0.0.1:" + port;

            HttpResponse<String> health = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/health")).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            assertEquals(200, health.statusCode());
            assertTrue(health.body().contains("UP"));

            // Stateless run
            String script = """
                    {
                      "config": {"maxOutOfOrdernessMillis": 0,
                                 "autoWatermarkIntervalMillis": 100,
                                 "idleTimeoutMillis": 300,
                                 "partitions": ["p1","p2"]},
                      "steps": [
                        {"op":"event","key":"p1","timestamp":1000},
                        {"op":"event","key":"p2","timestamp":1000},
                        {"op":"advance","time":100}
                      ]
                    }
                    """;
            HttpResponse<String> run = post(base + "/api/run", script);
            assertEquals(200, run.statusCode(), run.body());
            Map<String, Object> runBody = Json.parseObject(run.body());
            Map<String, Object> snapshot = cast(runBody.get("snapshot"));
            assertEquals(1000L, ((Number) snapshot.get("globalWatermark")).longValue());

            // Stateful session lifecycle
            HttpResponse<String> created = post(base + "/api/sessions",
                    "{\"config\":{\"idleTimeoutMillis\":300}}");
            assertEquals(201, created.statusCode());
            String id = (String) Json.parseObject(created.body()).get("sessionId");

            HttpResponse<String> scripted = post(base + "/api/sessions/" + id + "/script",
                    "{\"steps\":[{\"op\":\"event\",\"key\":\"p\",\"timestamp\":77},"
                            + "{\"op\":\"advance\",\"time\":100}]}");
            assertEquals(200, scripted.statusCode());
            Map<String, Object> scriptedBody = Json.parseObject(scripted.body());
            Map<String, Object> snap2 = cast(scriptedBody.get("snapshot"));
            assertEquals(77L, ((Number) snap2.get("globalWatermark")).longValue());

            HttpResponse<String> fetched = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/api/sessions/" + id)).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            assertEquals(200, fetched.statusCode());

            HttpResponse<String> deleted = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/api/sessions/" + id)).DELETE().build(),
                    HttpResponse.BodyHandlers.ofString());
            assertEquals(200, deleted.statusCode());

            HttpResponse<String> missing = client.send(
                    HttpRequest.newBuilder(URI.create(base + "/api/sessions/" + id)).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            assertEquals(404, missing.statusCode());
        }
    }

    @Test
    public void badRequestReturns400() throws Exception {
        try (WatermarkHttpServer server = new WatermarkHttpServer(0)) {
            server.start();
            HttpResponse<String> resp = post(
                    "http://127.0.0.1:" + server.getPort() + "/api/run",
                    "{\"steps\": \"not-an-array\"}");
            assertEquals(400, resp.statusCode());
            assertTrue(resp.body().contains("error"));
        }
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> cast(Object o) {
        return (Map<String, Object>) o;
    }
}
