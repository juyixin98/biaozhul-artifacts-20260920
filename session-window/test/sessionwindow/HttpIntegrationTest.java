package sessionwindow;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.Map;

/**
 * Black-box tests that boot the real {@link ServerMain} on an ephemeral port
 * and talk to it over HTTP using the JDK {@link HttpClient}. Includes a
 * restart-recovery test against a persistent data directory.
 */
final class HttpIntegrationTest {

    private HttpIntegrationTest() {
    }

    private static Path tempDir(String prefix) throws IOException {
        return Files.createTempDirectory("sw-test-" + prefix + "-");
    }

    private static ServerMain startFresh(Path dir) throws IOException {
        return ServerMain.start(0, 10, 10, dir); // gap=10, lateness=10, port 0 = ephemeral
    }

    private static String base(ServerMain app) {
        return "http://127.0.0.1:" + app.port();
    }

    private static HttpResponse<String> post(HttpClient client, String url, String json)
            throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofSeconds(5))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static HttpResponse<String> get(HttpClient client, String url)
            throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofSeconds(5))
                .GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> obj(HttpResponse<String> r) {
        return Json.parseObject(r.body());
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> listOfObjects(Map<String, Object> m, String key) {
        return (List<Map<String, Object>>) m.get(key);
    }

    private static Map<String, Object> firstObject(Map<String, Object> m, String key) {
        return listOfObjects(m, key).get(0);
    }

    static TestRunner build() {
        return new TestRunner("HTTP integration tests")
                .test("health responds ok", () -> {
                    Path dir = tempDir("health");
                    ServerMain app = startFresh(dir);
                    HttpClient client = HttpClient.newHttpClient();
                    try {
                        HttpResponse<String> r = get(client, base(app) + "/health");
                        TestRunner.eq(r.statusCode(), 200, "200");
                        TestRunner.eq(obj(r).get("status"), "ok", "status field");
                    } finally {
                        app.stop();
                    }
                })
                .test("unknown route -> 404 json", () -> {
                    Path dir = tempDir("notfound");
                    ServerMain app = startFresh(dir);
                    HttpClient client = HttpClient.newHttpClient();
                    try {
                        HttpResponse<String> r = get(client, base(app) + "/nope");
                        TestRunner.eq(r.statusCode(), 404, "404");
                        TestRunner.eq(obj(r).get("error"), Boolean.TRUE, "error body");
                    } finally {
                        app.stop();
                    }
                })
                .test("acceptance over HTTP: bridge emits retract+upsert, final view stable", () -> {
                    Path dir = tempDir("bridge");
                    ServerMain app = startFresh(dir);
                    HttpClient client = HttpClient.newHttpClient();
                    try {
                        String url = base(app) + "/events";

                        HttpResponse<String> r1 = post(client, url,
                                "{\"eventId\":\"e0\",\"key\":\"k\",\"timestamp\":0}");
                        HttpResponse<String> r2 = post(client, url,
                                "{\"eventId\":\"e20\",\"key\":\"k\",\"timestamp\":20}");
                        TestRunner.eq(r1.statusCode(), 200, "e0 accepted");
                        TestRunner.eq(r2.statusCode(), 200, "e20 accepted");

                        HttpResponse<String> sessionsBefore =
                                get(client, base(app) + "/sessions");
                        Map<String, Object> before = obj(sessionsBefore);
                        TestRunner.eq(before.get("count"), 2.0, "two sessions before bridge");

                        HttpResponse<String> r3 = post(client, url,
                                "{\"eventId\":\"e10\",\"key\":\"k\",\"timestamp\":10}");
                        TestRunner.eq(r3.statusCode(), 200, "late e10 accepted");
                        Map<String, Object> bridgeBody = obj(r3);
                        List<Map<String, Object>> changes = listOfObjects(bridgeBody, "changes");
                        TestRunner.eq(changes.size(), 3, "3 changelog entries from bridge");
                        TestRunner.eq(changes.get(0).get("type"), "RETRACT", "first is retract");
                        TestRunner.eq(changes.get(0).get("sessionId"), "k@20",
                                "retracted the later session");
                        TestRunner.eq(changes.get(2).get("type"), "UPSERT", "last is upsert");
                        TestRunner.eq(changes.get(2).get("sessionId"), "k@0",
                                "surviving session keeps id k@0");

                        HttpResponse<String> sessionsAfter =
                                get(client, base(app) + "/sessions");
                        Map<String, Object> after = obj(sessionsAfter);
                        TestRunner.eq(after.get("count"), 1.0, "one session after bridge");
                        Map<String, Object> only = firstObject(after, "sessions");
                        TestRunner.eq(only.get("sessionId"), "k@0", "stable id");
                        TestRunner.eq(only.get("version"), 2.0, "version bumped to 2");
                        TestRunner.eq(only.get("eventIds"), List.of("e0", "e10", "e20"),
                                "events ordered");

                        HttpResponse<String> acc =
                                get(client, base(app) + "/accounting");
                        Map<String, Object> accounting = obj(acc);
                        TestRunner.eq(accounting.get("noDoubleCount"), Boolean.TRUE,
                                "no double count over HTTP");
                        TestRunner.eq(accounting.get("materializedEventRows"), 3.0,
                                "three materialized rows");
                    } finally {
                        app.stop();
                    }
                })
                .test("watermark rejection returns 422 and is listed under /rejected", () -> {
                    Path dir = tempDir("wm");
                    ServerMain app = startFresh(dir);
                    HttpClient client = HttpClient.newHttpClient();
                    try {
                        String url = base(app) + "/events";
                        post(client, url, "{\"eventId\":\"hi\",\"key\":\"k\",\"timestamp\":100}");
                        // gap=10, lateness=10 -> watermark 90.
                        HttpResponse<String> rejected =
                                post(client, url, "{\"eventId\":\"lo\",\"key\":\"k\",\"timestamp\":5}");
                        TestRunner.eq(rejected.statusCode(), 422, "422 unprocessable (late)");
                        TestRunner.eq(obj(rejected).get("rejected"), Boolean.TRUE, "rejected flag");

                        HttpResponse<String> boundary =
                                post(client, url, "{\"eventId\":\"edge\",\"key\":\"k\",\"timestamp\":90}");
                        TestRunner.eq(boundary.statusCode(), 200, "t=90 at watermark accepted");

                        HttpResponse<String> rejList =
                                get(client, base(app) + "/rejected");
                        Map<String, Object> body = obj(rejList);
                        TestRunner.eq(body.get("count"), 1.0, "exactly one rejected event");

                        HttpResponse<String> wm = get(client, base(app) + "/watermark");
                        TestRunner.eq(obj(wm).get("watermark"), 90.0, "watermark exposed");
                    } finally {
                        app.stop();
                    }
                })
                .test("duplicate event is idempotent (200, duplicate:true, no new changes)", () -> {
                    Path dir = tempDir("dup");
                    ServerMain app = startFresh(dir);
                    HttpClient client = HttpClient.newHttpClient();
                    try {
                        String url = base(app) + "/events";
                        String payload = "{\"eventId\":\"d\",\"key\":\"k\",\"timestamp\":0}";
                        post(client, url, payload);
                        HttpResponse<String> again = post(client, url, payload);
                        TestRunner.eq(again.statusCode(), 200, "duplicate is 200");
                        Map<String, Object> b = obj(again);
                        TestRunner.eq(b.get("duplicate"), Boolean.TRUE, "duplicate marked");
                        TestRunner.eq(((List<?>) b.get("changes")).size(), 0, "no changes");
                    } finally {
                        app.stop();
                    }
                })
                .test("eventId reused with different timestamp -> 409", () -> {
                    Path dir = tempDir("conflict");
                    ServerMain app = startFresh(dir);
                    HttpClient client = HttpClient.newHttpClient();
                    try {
                        String url = base(app) + "/events";
                        post(client, url, "{\"eventId\":\"d\",\"key\":\"k\",\"timestamp\":0}");
                        HttpResponse<String> conflict =
                                post(client, url, "{\"eventId\":\"d\",\"key\":\"k\",\"timestamp\":9}");
                        TestRunner.eq(conflict.statusCode(), 409, "conflict -> 409");
                    } finally {
                        app.stop();
                    }
                })
                .test("batch ingest accepts an array and reports per-item results", () -> {
                    Path dir = tempDir("batch");
                    ServerMain app = startFresh(dir);
                    HttpClient client = HttpClient.newHttpClient();
                    try {
                        HttpResponse<String> r = post(client, base(app) + "/events",
                                "[{\"eventId\":\"a\",\"key\":\"k\",\"timestamp\":0},"
                                + "{\"eventId\":\"b\",\"key\":\"k\",\"timestamp\":5}]");
                        TestRunner.eq(r.statusCode(), 200, "batch 200");
                        Map<String, Object> body = obj(r);
                        TestRunner.eq(body.get("count"), 2.0, "two results");
                        HttpResponse<String> sessions =
                                get(client, base(app) + "/sessions");
                        TestRunner.eq(obj(sessions).get("count"), 1.0, "batch merged into one session");
                    } finally {
                        app.stop();
                    }
                })
                .test("malformed json -> 400", () -> {
                    Path dir = tempDir("badjson");
                    ServerMain app = startFresh(dir);
                    HttpClient client = HttpClient.newHttpClient();
                    try {
                        HttpResponse<String> r =
                                post(client, base(app) + "/events", "{not json");
                        TestRunner.eq(r.statusCode(), 400, "400");
                    } finally {
                        app.stop();
                    }
                })
                .test("changelog ?since pagination", () -> {
                    Path dir = tempDir("since");
                    ServerMain app = startFresh(dir);
                    HttpClient client = HttpClient.newHttpClient();
                    try {
                        String url = base(app) + "/events";
                        // gap=10, lateness=10: 0 and 20 are two sessions,
                        // then late 10 bridges them (all within lateness).
                        post(client, url, "{\"eventId\":\"a\",\"key\":\"k\",\"timestamp\":0}");
                        post(client, url, "{\"eventId\":\"b\",\"key\":\"k\",\"timestamp\":20}");
                        post(client, url, "{\"eventId\":\"c\",\"key\":\"k\",\"timestamp\":10}");
                        HttpResponse<String> all =
                                get(client, base(app) + "/changelog");
                        List<?> allChanges = (List<?>) obj(all).get("changes");
                        // upsert k@0, upsert k@20, then bridge:
                        // retract k@20, retract k@0 v1, upsert k@0 v2 = 5.
                        TestRunner.eq(allChanges.size(), 5, "five changelog entries total");
                        HttpResponse<String> since =
                                get(client, base(app) + "/changelog?since=2");
                        List<?> rest = (List<?>) obj(since).get("changes");
                        TestRunner.eq(rest.size(), 3, "three entries after seq 2");
                        TestRunner.eq(((Map<?, ?>) rest.get(0)).get("seq"), 3.0,
                                "starts at seq 3");
                    } finally {
                        app.stop();
                    }
                })
                .test("recovery after restart: same sessions, ids and versions", () -> {
                    Path dir = tempDir("recovery");
                    ServerMain first = startFresh(dir);
                    HttpClient client = HttpClient.newHttpClient();
                    try {
                        String url = base(first) + "/events";
                        post(client, url, "{\"eventId\":\"e0\",\"key\":\"k\",\"timestamp\":0}");
                        post(client, url, "{\"eventId\":\"e20\",\"key\":\"k\",\"timestamp\":20}");
                        post(client, url, "{\"eventId\":\"e10\",\"key\":\"k\",\"timestamp\":10}");
                        post(client, url, "{\"eventId\":\"late\",\"key\":\"k\",\"timestamp\":80}");
                        // watermark after t=80 is 70; earlier bridge events are persisted.
                    } finally {
                        first.stop();
                    }

                    // Restart on the same data directory: the append log is replayed.
                    ServerMain second = startFresh(dir);
                    try {
                        HttpResponse<String> r =
                                get(client, base(second) + "/sessions");
                        Map<String, Object> body = obj(r);
                        List<Map<String, Object>> sessions = listOfObjects(body, "sessions");
                        TestRunner.eq(sessions.size(), 2, "k@0..20 and k@80 survive restart");
                        TestRunner.eq(sessions.get(0).get("sessionId"), "k@0", "id stable 1");
                        TestRunner.eq(sessions.get(0).get("version"), 2.0, "k@0 version 2 stable");
                        TestRunner.eq(sessions.get(0).get("eventIds"),
                                List.of("e0", "e10", "e20"), "bridged events stable");
                        TestRunner.eq(sessions.get(1).get("sessionId"), "k@80", "id stable 2");

                        HttpResponse<String> acc =
                                get(client, base(second) + "/accounting");
                        TestRunner.eq(obj(acc).get("noDoubleCount"), Boolean.TRUE,
                                "no double count after recovery");
                        TestRunner.eq(obj(acc).get("materializedEventRows"), 4.0,
                                "all four events counted once");
                    } finally {
                        second.stop();
                    }
                });
    }
}
