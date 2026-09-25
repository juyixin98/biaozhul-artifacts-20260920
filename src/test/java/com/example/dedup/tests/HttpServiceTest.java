package com.example.dedup.tests;

import com.example.dedup.json.Json;
import com.example.dedup.pipeline.PipelineConfig;
import com.example.dedup.service.HttpService;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;

/** Black-box tests of the JSON HTTP service, including restart recovery. */
public final class HttpServiceTest {

    private static PipelineConfig cfg() {
        return new PipelineConfig(1000, 2000, 1000, 2000, 1000, 100, 0, 0);
    }

    public static void register(TestRunner r) {
        r.run("HTTP: health + ingest duplicate + watermark + drain + metrics", () -> {
            Path tmp = Files.createTempDirectory("dedup-test-");
            Path snap = tmp.resolve("state.json");
            HttpService svc = HttpService.manual(0, cfg(), snap);
            svc.start();
            int port = svc.port();
            try {
                HttpClient c = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();

                Json.JsonObject health = get(c, port, "/health");
                TestRunner.assertTrue(health.has("clockMillis"), "health shape");

                // two events, second a duplicate with a different payload
                String body = "{\"events\":["
                        + "{\"id\":\"e1\",\"eventTime\":100,\"type\":\"UPSERT\",\"key\":\"k\",\"payload\":{\"v\":1}},"
                        + "{\"id\":\"e1\",\"eventTime\":100,\"type\":\"UPSERT\",\"key\":\"k\",\"payload\":{\"v\":2}}"
                        + "]}";
                Json.JsonObject resp = post(c, port, "/events", body);
                Json.JsonArray results = (Json.JsonArray) resp.get("results");
                TestRunner.assertEquals(2, results.elements().size(), "two results");
                Json.JsonObject r1 = (Json.JsonObject) results.elements().get(0);
                Json.JsonObject r2 = (Json.JsonObject) results.elements().get(1);
                TestRunner.assertTrue(asBool(r1, "emitted"), "first emitted over HTTP");
                TestRunner.assertTrue(asBool(r2, "duplicate"), "second duplicate over HTTP");
                TestRunner.assertTrue(asBool(r2, "payloadMismatch"), "mismatch visible over HTTP");

                // bad request surfaces a 400 envelope
                HttpResponse<String> bad = rawPost(c, port, "/events", "{\"events\":\"nope\"}");
                TestRunner.assertEquals(400, bad.statusCode(), "validation 400");

                // advance watermark to fire window [0,1000)+lateness 100 -> needs 1100
                Json.JsonObject wm = post(c, port, "/watermark", "{\"watermark\":1100}");
                TestRunner.assertTrue(asBool(wm, "advanced"), "watermark advanced");

                Json.JsonObject drain = post(c, port, "/drain", "{}");
                Json.JsonArray wins = (Json.JsonArray) drain.get("windowResults");
                TestRunner.assertEquals(1, wins.elements().size(), "one window fired");
                Json.JsonObject win = (Json.JsonObject) wins.elements().get(0);
                TestRunner.assertEquals("k", ((Json.JsonString) win.get("key")).value(), "key");

                // duplicate drain is empty (queue drained)
                Json.JsonObject drain2 = post(c, port, "/drain", "{}");
                TestRunner.assertEquals(0, ((Json.JsonArray) drain2.get("windowResults")).elements().size(),
                        "output queue drained");

                // watermark regression is reported
                Json.JsonObject reg = post(c, port, "/watermark", "{\"watermark\":500}");
                TestRunner.assertFalse(asBool(reg, "advanced"), "regression rejected");
                Json.JsonObject metrics = get(c, port, "/metrics");
                TestRunner.assertEquals(1L, ((Json.JsonNumber) metrics.get("watermarkRegressions")).longValue(),
                        "regression metric");

                // persist snapshot file
                post(c, port, "/snapshot", "{}");
                TestRunner.assertTrue(Files.exists(snap), "snapshot file written");
            } finally {
                svc.close();
            }
        });

        r.run("HTTP: restart from snapshot file keeps dedup + pending window", () -> {
            Path tmp = Files.createTempDirectory("dedup-test-");
            Path snap = tmp.resolve("state.json");
            PipelineConfig config = cfg();

            HttpService s1 = HttpService.manual(0, config, snap);
            s1.start();
            int p1;
            try {
                p1 = s1.port();
                HttpClient c = HttpClient.newHttpClient();
                post(c, p1, "/events",
                        "{\"events\":[{\"id\":\"e1\",\"eventTime\":100,\"key\":\"k\",\"payload\":{\"v\":1}}]}");
                post(c, p1, "/watermark", "{\"watermark\":500}"); // window not fired yet
                post(c, p1, "/snapshot", "{}");
            } finally {
                s1.close();
            }

            // New service process on the same snapshot file restores state.
            HttpService s2 = HttpService.manual(0, config, snap);
            s2.start();
            try {
                int p2 = s2.port();
                HttpClient c = HttpClient.newHttpClient();
                Json.JsonObject resp = post(c, p2, "/events",
                        "{\"events\":[{\"id\":\"e1\",\"eventTime\":100,\"key\":\"k\",\"payload\":{\"v\":1}}]}");
                Json.JsonObject dup = (Json.JsonObject) ((Json.JsonArray) resp.get("results")).elements().get(0);
                TestRunner.assertTrue(asBool(dup, "duplicate"), "dedup survived process restart");

                // pending window accumulator survived: advance and fire -> exactly one window, one upsert
                post(c, p2, "/watermark", "{\"watermark\":1100}");
                Json.JsonObject drain = post(c, p2, "/drain", "{}");
                Json.JsonArray wins = (Json.JsonArray) drain.get("windowResults");
                TestRunner.assertEquals(1, wins.elements().size(), "one window after restart");
            } finally {
                s2.close();
            }
        });

        r.run("HTTP: clock rollback via /tick semantics and no state loss", () -> {
            Path tmp = Files.createTempDirectory("dedup-test-");
            HttpService svc = HttpService.manual(0, cfg(), tmp.resolve("s.json"));
            svc.start();
            try {
                int port = svc.port();
                HttpClient c = HttpClient.newHttpClient();
                post(c, port, "/events",
                        "{\"events\":[{\"id\":\"e1\",\"eventTime\":100,\"key\":\"k\",\"payload\":{\"v\":1}}]}");
                Json.JsonObject back = post(c, port, "/tick", "{\"advanceMillis\":-5000}");
                TestRunner.assertEquals(-5000L,
                        ((Json.JsonNumber) back.get("clockMillis")).longValue(), "clock rolled back on demand");
                // dedup unaffected by processing clock
                Json.JsonObject resp = post(c, port, "/events",
                        "{\"events\":[{\"id\":\"e1\",\"eventTime\":100,\"key\":\"k\",\"payload\":{\"v\":1}}]}");
                Json.JsonObject dup = (Json.JsonObject) ((Json.JsonArray) resp.get("results")).elements().get(0);
                TestRunner.assertTrue(asBool(dup, "duplicate"), "dedup intact after processing-clock rollback");
            } finally {
                svc.close();
            }
        });
    }

    // ---------------------------------------------------------------- helpers

    private static boolean asBool(Json.JsonObject o, String key) {
        return o.get(key) == Json.JsonBool.TRUE;
    }

    private static Json.JsonObject get(HttpClient c, int port, String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .timeout(Duration.ofSeconds(5)).GET().build();
        HttpResponse<String> res = c.send(req, HttpResponse.BodyHandlers.ofString());
        return Json.parseObject(res.body());
    }

    private static Json.JsonObject post(HttpClient c, int port, String path, String body) throws Exception {
        HttpResponse<String> res = rawPost(c, port, path, body);
        if (res.statusCode() / 100 != 2) {
            throw new AssertionError(path + " -> " + res.statusCode() + ": " + res.body());
        }
        return Json.parseObject(res.body());
    }

    private static HttpResponse<String> rawPost(HttpClient c, int port, String path, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path))
                .timeout(Duration.ofSeconds(5))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        return c.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
