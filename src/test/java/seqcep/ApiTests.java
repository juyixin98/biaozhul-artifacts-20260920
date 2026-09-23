package seqcep;

import seqcep.engine.MatchEngine;
import seqcep.http.ApiServer;
import seqcep.json.Json;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import static seqcep.TestRunner.*;

/** End-to-end tests over the real HTTP API (in-memory engine, ephemeral port). */
public final class ApiTests {

    private static final HttpClient CLIENT = HttpClient.newHttpClient();

    public static void register() {

        test("http: full ingest/match/state/reset flow", () -> {
            MatchEngine engine = MatchEngine.inMemory("api-test");
            ApiServer api = new ApiServer(engine, 0);
            api.start();
            try {
                String base = "http://localhost:" + api.port();

                // health
                Resp health = get(base + "/healthz");
                assertEquals(200, health.status, "health status");

                // single event
                Resp one = post(base + "/v1/events",
                        "{\"type\":\"A\",\"entity\":\"e1\",\"ts\":1000}");
                assertEquals(200, one.status, "single ingest status");
                assertEquals(1L, asLong(one.json.get("accepted")), "accepted count");

                // batch: A (dup), B, noise, C  -> with the first A: A,A,B,C = 2 matches
                Resp batch = post(base + "/v1/events",
                        "{\"events\":["
                                + "{\"type\":\"A\",\"entity\":\"e1\",\"ts\":1500},"
                                + "{\"type\":\"B\",\"entity\":\"e1\",\"ts\":2000},"
                                + "{\"type\":\"X\",\"entity\":\"e1\",\"ts\":2500},"
                                + "{\"type\":\"C\",\"entity\":\"e1\",\"ts\":3000}]}");
                assertEquals(200, batch.status, "batch ingest status");
                assertEquals(4L, asLong(batch.json.get("accepted")), "batch accepted");

                // matches
                Resp matches = get(base + "/v1/matches?entity=e1");
                assertEquals(200, matches.status, "matches status");
                assertEquals(2L, asLong(matches.json.get("total")), "A,A,B,C -> 2 matches");
                assertEquals(false, matches.json.get("hasMore"), "no truncation without limit");
                @SuppressWarnings("unchecked")
                List<Object> items = (List<Object>) matches.json.get("matches");
                assertEquals(2, items.size(), "returned matches");

                // state snapshot exposes partials
                Resp state = get(base + "/v1/state");
                assertEquals(200, state.status, "state status");
                assertEquals(5L, asLong(state.json.get("lastSeq")), "lastSeq after 5 events");

                // reset
                Resp reset = delete(base + "/v1/state");
                assertEquals(200, reset.status, "reset status");
                Resp after = get(base + "/v1/matches");
                assertEquals(0L, asLong(after.json.get("total")), "matches cleared after reset");
            } finally {
                api.stop();
            }
        });

        test("http: acceptance scenario A,B,timeout,C via API", () -> {
            MatchEngine engine = MatchEngine.inMemory("api-timeout");
            ApiServer api = new ApiServer(engine, 0);
            api.start();
            try {
                String base = "http://localhost:" + api.port();
                post(base + "/v1/events", "{\"events\":["
                        + "{\"type\":\"A\",\"entity\":\"dev-7\",\"ts\":1000},"
                        + "{\"type\":\"B\",\"entity\":\"dev-7\",\"ts\":2000},"
                        + "{\"type\":\"C\",\"entity\":\"dev-7\",\"ts\":13000}]}");
                Resp m = get(base + "/v1/matches?entity=dev-7");
                assertEquals(0L, asLong(m.json.get("total")), "timed-out chain must not match");
            } finally {
                api.stop();
            }
        });

        test("http: pagination is explicit, never silent", () -> {
            MatchEngine engine = MatchEngine.inMemory("api-page");
            ApiServer api = new ApiServer(engine, 0);
            api.start();
            try {
                String base = "http://localhost:" + api.port();
                // A,A,B,B,C -> 4 matches
                post(base + "/v1/events", "{\"events\":["
                        + "{\"type\":\"A\",\"entity\":\"e1\",\"ts\":1000},"
                        + "{\"type\":\"A\",\"entity\":\"e1\",\"ts\":1100},"
                        + "{\"type\":\"B\",\"entity\":\"e1\",\"ts\":2000},"
                        + "{\"type\":\"B\",\"entity\":\"e1\",\"ts\":2100},"
                        + "{\"type\":\"C\",\"entity\":\"e1\",\"ts\":3000}]}");

                Resp all = get(base + "/v1/matches?entity=e1");
                assertEquals(4L, asLong(all.json.get("total")), "all matches without limit");
                @SuppressWarnings("unchecked")
                List<Object> allItems = (List<Object>) all.json.get("matches");
                assertEquals(4, allItems.size(), "no silent truncation");

                Resp page = get(base + "/v1/matches?entity=e1&limit=2&offset=0");
                assertEquals(4L, asLong(page.json.get("total")), "total still reported");
                assertEquals(true, page.json.get("hasMore"), "hasMore flags truncation");
                @SuppressWarnings("unchecked")
                List<Object> pageItems = (List<Object>) page.json.get("matches");
                assertEquals(2, pageItems.size(), "page size");

                Resp page2 = get(base + "/v1/matches?entity=e1&limit=2&offset=2");
                assertEquals(false, page2.json.get("hasMore"), "last page has no more");
            } finally {
                api.stop();
            }
        });

        test("http: malformed requests get 400, unknown routes 404", () -> {
            MatchEngine engine = MatchEngine.inMemory("api-errors");
            ApiServer api = new ApiServer(engine, 0);
            api.start();
            try {
                String base = "http://localhost:" + api.port();

                Resp badJson = post(base + "/v1/events", "{not json");
                assertEquals(400, badJson.status, "malformed JSON");

                Resp missingField = post(base + "/v1/events", "{\"type\":\"A\",\"ts\":1}");
                assertEquals(400, missingField.status, "missing entity field");

                Resp badTs = post(base + "/v1/events",
                        "{\"type\":\"A\",\"entity\":\"e1\",\"ts\":\"soon\"}");
                assertEquals(400, badTs.status, "non-numeric ts");

                Resp unknown = get(base + "/v1/nope");
                assertEquals(404, unknown.status, "unknown route");

                Resp wrongMethod = delete(base + "/v1/events");
                assertEquals(405, wrongMethod.status, "DELETE on /v1/events");

                // failed validation must not have ingested anything
                Resp state = get(base + "/v1/state");
                assertEquals(0L, asLong(state.json.get("acceptedEvents")), "no partial ingest on error");
            } finally {
                api.stop();
            }
        });

        test("http: recovery through a real server restart", () -> {
            Path dir = Path.of("build", "test-data", "api-restart");
            deleteRecursively(dir);
            Files.createDirectories(dir);
            Path wal = dir.resolve("events.log");

            MatchEngine e1 = MatchEngine.open("api-restart", wal);
            ApiServer api1 = new ApiServer(e1, 0);
            api1.start();
            String base1 = "http://localhost:" + api1.port();
            post(base1 + "/v1/events", "{\"events\":["
                    + "{\"type\":\"A\",\"entity\":\"e1\",\"ts\":1000},"
                    + "{\"type\":\"B\",\"entity\":\"e1\",\"ts\":2000}]}");
            Resp stateBefore = get(base1 + "/v1/state");
            api1.stop();
            e1.close();

            // "restart": new engine over the same WAL
            MatchEngine e2 = MatchEngine.open("api-restart", wal);
            ApiServer api2 = new ApiServer(e2, 0);
            api2.start();
            try {
                String base2 = "http://localhost:" + api2.port();
                Resp stateAfter = get(base2 + "/v1/state");
                assertEquals(Json.write(stateBefore.json.get("entities")),
                        Json.write(stateAfter.json.get("entities")),
                        "partial state identical across server restart");

                // the recovered pending pair completes
                post(base2 + "/v1/events", "{\"type\":\"C\",\"entity\":\"e1\",\"ts\":3000}");
                Resp m = get(base2 + "/v1/matches?entity=e1");
                assertEquals(1L, asLong(m.json.get("total")), "match completes after restart");
            } finally {
                api2.stop();
                e2.close();
            }
        });
    }

    // ------------------------------------------------------------- helpers

    private record Resp(int status, Map<String, Object> json) {}

    private static Resp get(String url) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url)).GET().build();
        return send(req);
    }

    private static Resp delete(String url) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url)).DELETE().build();
        return send(req);
    }

    private static Resp post(String url, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .header("Content-Type", "application/json")
                .build();
        return send(req);
    }

    private static Resp send(HttpRequest req) throws Exception {
        HttpResponse<String> resp = CLIENT.send(req, HttpResponse.BodyHandlers.ofString());
        Map<String, Object> json;
        try {
            json = Json.parseObject(resp.body());
        } catch (RuntimeException e) {
            throw new AssertionError("Response was not a JSON object: " + resp.body());
        }
        return new Resp(resp.statusCode(), json);
    }

    private static long asLong(Object v) {
        if (v instanceof Number n) return n.longValue();
        throw new AssertionError("expected number, got " + v);
    }

    private static void deleteRecursively(Path dir) throws Exception {
        if (!Files.exists(dir)) return;
        try (var walk = Files.walk(dir)) {
            walk.sorted(java.util.Comparator.reverseOrder())
                    .forEach(p -> {
                        try { Files.delete(p); } catch (Exception e) { throw new RuntimeException(e); }
                    });
        }
    }

    private ApiTests() {}
}
