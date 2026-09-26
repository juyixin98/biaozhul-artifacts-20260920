package hlc.server;

import hlc.Json;
import hlc.Test;
import hlc.TestRunner;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

/** Boots the real {@link HLCHttpServer} on an ephemeral port and exercises it over HTTP. */
public class HLCHttpServerIT {

    private static HLCHttpServer server;
    private static HttpClient http;
    private static String base;

    private static synchronized void ensureStarted() throws Exception {
        if (server != null) {
            return;
        }
        Path state = Files.createTempDirectory("hlc-it-").resolve("state.properties");
        server = HLCHttpServer.start(0, state);
        base = "http://localhost:" + server.port();
        http = HttpClient.newHttpClient();
    }

    @hlc.AfterAll
    public static void shutdown() {
        if (server != null) {
            server.stop();
            server = null;
        }
    }

    @Test("health and info report the tzdb version")
    void healthAndInfo() throws Exception {
        ensureStarted();
        Map<String, Object> health = get("/health");
        TestRunner.assertEquals("ok", health.get("status"), "health ok");
        TestRunner.assertTrue(((String) health.get("tzdb")).length() >= 4, "tzdb present");
        Map<String, Object> info = get("/info");
        TestRunner.assertEquals("hybrid-logical-clock", info.get("service"), "service name");
    }

    @Test("full create/tick/send/receive/persist flow over HTTP")
    void clockFlowOverHttp() throws Exception {
        ensureStarted();
        post("/nodes", Map.of("node", "alice", "initialPhysicalMicros", 1000L));
        post("/nodes", Map.of("node", "bob"));

        Map<String, Object> sent = post("/send",
                Map.of("node", "alice", "physicalMicros", 5000L));
        TestRunner.assertEquals("5000:0", sent.get("hlc"), "alice sends at 5000, c resets to 0");

        Map<String, Object> recv = post("/receive", Map.of(
                "node", "bob", "physicalMicros", 4000L, "message", sent.get("message")));
        TestRunner.assertEquals("5000:1", recv.get("hlc"),
                "bob physically behind but merges, counter continues from remote (cm+1)");

        Map<String, Object> saved = post("/persist/save", Map.of());
        TestRunner.assertEquals(Boolean.TRUE, saved.get("saved"), "persisted");

        Map<String, Object> nodes = get("/nodes");
        TestRunner.assertEquals(2, ((List<?>) nodes.get("nodes")).size(), "two nodes listed");
    }

    @Test("simulate endpoint validates causal soundness over HTTP")
    void simulateOverHttp() throws Exception {
        ensureStarted();
        Map<String, Object> resp = post("/simulate", Map.of(
                "name", "http causal scenario",
                "events", List.of(
                        Map.of("label", "s1", "node", "a", "type", "SEND", "physicalMicros", 100L),
                        Map.of("label", "r1", "node", "b", "type", "RECV", "physicalMicros", 50L,
                                "from", "s1"))));
        TestRunner.assertEquals(Boolean.TRUE, resp.get("soundnessHolds"), "soundness holds");
        TestRunner.assertEquals(0, ((List<?>) resp.get("violations")).size(), "no violations");
    }

    @Test("malformed JSON and overflow return structured error statuses")
    void errorHandling() throws Exception {
        ensureStarted();
        HttpResponse<String> badJson = rawPost("/tick", "{not valid json");
        TestRunner.assertEquals(400, badJson.statusCode(), "bad JSON -> 400");
        TestRunner.assertTrue(Json.parseObject(badJson.body()).containsKey("error"),
                "error envelope present");

        post("/nodes", Map.of("node", "z"));
        String overflowState = "{\"node\":\"z\",\"state\":{\"l\":1,\"c\":"
                + hlc.LogicalCounterOverflowException.MAX_COUNTER + "}}";
        post("/restore", Json.parseObject(overflowState));
        HttpResponse<String> overflow = rawPost("/tick",
                "{\"node\":\"z\",\"physicalMicros\":1}");
        TestRunner.assertEquals(507, overflow.statusCode(), "overflow -> 507");
        TestRunner.assertEquals("LOGICAL_COUNTER_OVERFLOW",
                Json.parseObject(overflow.body()).get("error"), "explicit overflow code");
    }

    // ------------------------------------------------------------ harness

    private Map<String, Object> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
        TestRunner.assertEquals(200, resp.statusCode(), "GET " + path + " -> " + resp.body());
        return Json.parseObject(resp.body());
    }

    private Map<String, Object> post(String path, Map<String, Object> body) throws Exception {
        HttpResponse<String> resp = rawPost(path, Json.write(body));
        TestRunner.assertEquals(200, resp.statusCode(), "POST " + path + " -> " + resp.body());
        return Json.parseObject(resp.body());
    }

    private HttpResponse<String> rawPost(String path, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
