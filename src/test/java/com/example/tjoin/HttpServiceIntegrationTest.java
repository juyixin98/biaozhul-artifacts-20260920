package com.example.tjoin;

import com.example.tjoin.server.Main;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * End-to-end JSON/HTTP tests of the service. Runs the real JDK HTTP server
 * on an ephemeral port with a manually driven clock. Each acceptance point
 * gets its own job so buffer/watermark state stays simple to reason about.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class HttpServiceIntegrationTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private Main service;
    private String base;
    private HttpClient client;

    @BeforeAll
    void start() throws IOException {
        service = new Main(0);
        service.start();
        base = "http://127.0.0.1:" + service.port();
        // Explicit NO_PROXY regardless of *_proxy environment variables.
        client = HttpClient.newBuilder()
                .proxy(java.net.ProxySelector.of(null))
                .build();
    }

    @AfterAll
    void stop() {
        service.stop();
    }

    // ------------------------------------------------------------------

    private JsonNode post(String path, Object body) throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
                .build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        return expect(resp, 200, 201);
    }

    private JsonNode get(String path) throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).GET().build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        return expect(resp, 200);
    }

    private JsonNode delete(String path) throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path)).DELETE().build();
        HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
        return expect(resp, 200);
    }

    private JsonNode expect(HttpResponse<String> resp, int... allowed) throws IOException {
        JsonNode node = MAPPER.readTree(resp.body());
        for (int code : allowed) {
            if (resp.statusCode() == code) {
                return node;
            }
        }
        throw new AssertionError("HTTP " + resp.statusCode() + ": " + resp.body()
                + " uri=" + resp.request().method());
    }

    private JsonNode status(String jobId) throws IOException, InterruptedException {
        return get("/api/v1/jobs/" + jobId + "/status").path("data");
    }

    private Map<String, Object> ev(String id, String key, long eventTime) {
        return Map.of("id", id, "key", key, "eventTime", eventTime);
    }

    private JsonNode createJob(String id, long lower, long upper, long idle, int cap,
                               String policy) throws Exception {
        return post("/api/v1/jobs", Map.of(
                "jobId", id,
                "lowerBound", lower,
                "upperBound", upper,
                "maxOutOfOrderness", 0,
                "idleTimeoutMillis", idle,
                "maxBufferSize", cap,
                "overflowPolicy", policy));
    }

    // ------------------------------------------------------------------

    @Test
    @DisplayName("health endpoint responds")
    void health() throws Exception {
        HttpResponse<String> health = client.send(
                HttpRequest.newBuilder(URI.create(base + "/health")).GET().build(),
                HttpResponse.BodyHandlers.ofString());
        assertEquals(200, health.statusCode());
        assertTrue(health.body().contains("\"ok\":true"));
    }

    @Test
    @DisplayName("matches and inclusive upper boundary over HTTP")
    void matchesAndBoundary() throws Exception {
        createJob("j-match", 0, 10_000, 0, 0, "REJECT");
        post("/api/v1/jobs/j-match/events/left", Map.of(
                "processingTime", 1000,
                "events", new Object[]{ev("L1", "k", 10_000), ev("L2", "k", 12_000)}));

        JsonNode r = post("/api/v1/jobs/j-match/events/right", Map.of(
                "processingTime", 2000,
                "events", new Object[]{
                        ev("R1", "k", 15_000),    // (L1,R1) d=5000, (L2,R1) d=3000
                        ev("R2", "k", 22_000),    // (L2,R2) d=10000 boundary
                        ev("R3", "k", 22_001)})); // d=10001 vs L2 -> no match
        assertEquals(3, r.path("data").path("emittedCount").asInt());
        assertEquals(3, status("j-match").path("metrics").path("pairsEmitted").asInt());
    }

    @Test
    @DisplayName("duplicate event id is ignored; distinct ids with same value all match")
    void duplicateIdsAndValues() throws Exception {
        createJob("j-dup", 0, 10_000, 0, 0, "REJECT");
        JsonNode r = post("/api/v1/jobs/j-dup/events/left", Map.of(
                "processingTime", 1,
                "events", new Object[]{
                        Map.of("id", "L1", "key", "k", "eventTime", 10_000, "value", "same"),
                        Map.of("id", "L1", "key", "k", "eventTime", 10_000, "value", "same"),
                        Map.of("id", "L2", "key", "k", "eventTime", 10_000, "value", "same")}));
        assertEquals("DUPLICATE", r.path("data").path("items").get(1).path("status").asText());

        // Two duplicate-VALUE right events, distinct ids, each matches L1 and L2.
        JsonNode out = post("/api/v1/jobs/j-dup/events/right", Map.of(
                "processingTime", 2,
                "events", new Object[]{
                        Map.of("id", "R1", "key", "k", "eventTime", 10_000, "value", "same"),
                        Map.of("id", "R2", "key", "k", "eventTime", 10_000, "value", "same")}));
        assertEquals(4, out.path("data").path("emittedCount").asInt());
    }

    @Test
    @DisplayName("buffer REJECT caps memory without discarding buffered matchable records")
    void bufferCapReject() throws Exception {
        createJob("j-cap", 0, 100_000, 0, 3, "REJECT");
        // Wide interval means NO watermark cleanup removes anything during
        // this test (right watermark never exceeds 100_000).
        post("/api/v1/jobs/j-cap/events/left", Map.of(
                "processingTime", 1,
                "events", new Object[]{ev("L1", "k", 1000), ev("L2", "k", 2000),
                        ev("L3", "k", 3000)}));

        JsonNode full = post("/api/v1/jobs/j-cap/events/left", Map.of(
                "processingTime", 2,
                "events", new Object[]{ev("L4", "k", 4000)}));
        assertEquals("BUFFER_FULL", full.path("data").path("items").get(0).path("status").asText());
        assertEquals(3, status("j-cap").path("leftBuffered").asInt());

        // Retrying the same id is still a normal attempt (not DUPLICATE).
        JsonNode retry = post("/api/v1/jobs/j-cap/events/left", Map.of(
                "processingTime", 3,
                "events", new Object[]{ev("L4", "k", 4000)}));
        assertEquals("BUFFER_FULL", retry.path("data").path("items").get(0).path("status").asText());

        // All buffered records remain matchable: R at 2000 pairs with both
        // L1(1000, d=1000) and L2(2000, d=0).
        JsonNode matches = post("/api/v1/jobs/j-cap/events/right", Map.of(
                "processingTime", 4,
                "events", new Object[]{ev("R1", "k", 2000)}));
        assertEquals(2, matches.path("data").path("emittedCount").asInt());
    }

    @Test
    @DisplayName("stalled LEFT: right records are not prematurely discarded and match on recovery")
    void leftStallAndRecovery() throws Exception {
        // interval [0, 10000], idle timeout 5000 processing ms, wide cap.
        createJob("j-stall", 0, 10_000, 5_000, 100, "REJECT");

        post("/api/v1/jobs/j-stall/events/left", Map.of(
                "processingTime", 1000,
                "events", new Object[]{ev("L1", "k", 10_000)}));          // left wm 10000
        post("/api/v1/jobs/j-stall/events/right", Map.of(
                "processingTime", 2000,
                "events", new Object[]{ev("R1", "k", 12_000), ev("R2", "k", 18_000)}));
        // (L1,R1) matches. R1,R2 buffered: R1(12000) would be cleaned by a
        // left wm of 10000? threshold = 10000+0 = 10000; both >= 10000, kept.

        // Left stalls; right keeps advancing its own watermark.
        post("/api/v1/jobs/j-stall/events/right", Map.of(
                "processingTime", 3000,
                "events", new Object[]{ev("R3", "k", 50_000)}));          // right wm 50000

        JsonNode idle = post("/api/v1/jobs/j-stall/time", Map.of("advanceTo", 8000));
        assertTrue(idle.path("data").path("status").path("metrics").path("leftIdle").asBoolean(),
                "left side must be detected idle after the timeout");

        // Even though left is idle, R1(12000),R2(18000) are retained: left
        // withholding its watermark is conservative; independently the
        // records would be unmatchable, but no record is discarded due to
        // staleness of processing alone — verify they are still buffered.
        JsonNode st = status("j-stall");
        assertEquals(3, st.path("rightBuffered").asInt(),
                "right records retained while left is stalled");

        // Left recovers at t=10000 (its frozen watermark) -> R1(12000)
        // matches (d=2000); R2 d=8000 also matches! choose R at 11000 for
        // exactly one new pair instead.
        JsonNode recovered = post("/api/v1/jobs/j-stall/events/left", Map.of(
                "processingTime", 9000,
                "events", new Object[]{ev("L2", "k", 10_000)}));
        assertFalse(status("j-stall").path("metrics").path("leftIdle").asBoolean());
        // L2 at 10000 matches R1(12000,d2000) and R2(18000,d8000): 2 pairs.
        assertEquals(2, recovered.path("data").path("emittedCount").asInt());
    }

    @Test
    @DisplayName("stalled RIGHT: left records retained; resuming right matches them")
    void rightStallAndRecovery() throws Exception {
        createJob("j-stall2", 0, 10_000, 5_000, 100, "REJECT");

        post("/api/v1/jobs/j-stall2/events/right", Map.of(
                "processingTime", 1000,
                "events", new Object[]{ev("R1", "k", 10_000)}));          // right wm 10000
        // Left events: L at 5000 would be pruned by right wm threshold
        // 10000-10000=0 -> kept. Send left up to 20000.
        post("/api/v1/jobs/j-stall2/events/left", Map.of(
                "processingTime", 2000,
                "events", new Object[]{ev("L1", "k", 5_000), ev("L2", "k", 15_000)}));
        // (L1,R1) matches (d=5000). Left: L1 t=5000, L2 t=15000 buffered.

        post("/api/v1/jobs/j-stall2/events/left", Map.of(
                "processingTime", 3000,
                "events", new Object[]{ev("L3", "k", 60_000)}));          // left wm 60000
        JsonNode idle = post("/api/v1/jobs/j-stall2/time", Map.of("advanceTo", 8000));
        assertTrue(idle.path("data").path("status").path("metrics").path("rightIdle").asBoolean());

        // Left state is pruned by the (idle) right watermark 10000:
        // threshold = 10000-10000 = 0, so L1(5000),L2(15000),L3(60000) all
        // retained — the critical non-premature-discard guarantee.
        assertEquals(3, status("j-stall2").path("leftBuffered").asInt());

        // Right resumes at 10000 (frozen wm); L2(15000) distance -5000 is
        // out of [0,10000]; L1(5000) distance 5000 matches (already paired
        // with R1 — different id though: R2 vs L1 is a NEW pair).
        JsonNode recovered = post("/api/v1/jobs/j-stall2/events/right", Map.of(
                "processingTime", 9000,
                "events", new Object[]{ev("R2", "k", 10_000)}));
        assertEquals(1, recovered.path("data").path("emittedCount").asInt());
        assertEquals("L1", recovered.path("data").path("emitted").get(0).path("leftId").asText());
    }

    @Test
    @DisplayName("explicit watermark injection: boundary-exact cleanup, late event dropped")
    void watermarkCleanupAndLate() throws Exception {
        createJob("j-wm", 0, 10_000, 0, 100, "REJECT");
        post("/api/v1/jobs/j-wm/events/right", Map.of(
                "processingTime", 1,
                "events", new Object[]{
                        ev("RA", "k", 10_000), ev("RB", "k", 15_000),
                        ev("RC", "k", 20_000), ev("RD", "k", 35_000)}));

        // Inject left watermark 20000: right threshold 20000 + lower(0).
        // tR < 20000 removed strictly -> RA(10000),RB(15000); RC kept (boundary).
        JsonNode wm = post("/api/v1/jobs/j-wm/watermark/left",
                Map.of("watermark", 20_000, "processingTime", 2));
        assertEquals(2, wm.path("data").path("cleaned").asInt());
        assertEquals(2, status("j-wm").path("rightBuffered").asInt());

        // RC at exactly 20000 matches a left at 20000 (d=0); RD d=15000 out.
        JsonNode edge = post("/api/v1/jobs/j-wm/events/left", Map.of(
                "processingTime", 3,
                "events", new Object[]{ev("LE", "k", 20_000)}));
        assertEquals(1, edge.path("data").path("emittedCount").asInt());

        // A left event behind the left watermark 20000 is late.
        JsonNode late = post("/api/v1/jobs/j-wm/events/left", Map.of(
                "processingTime", 4,
                "events", new Object[]{ev("LL", "k", 19_999)}));
        assertEquals("LATE", late.path("data").path("items").get(0).path("status").asText());
    }

    @Test
    @DisplayName("DROP_OLDEST evicts the globally oldest event")
    void dropOldestOverHttp() throws Exception {
        createJob("j-drop", 0, 1_000_000, 0, 2, "DROP_OLDEST");
        post("/api/v1/jobs/j-drop/events/left", Map.of(
                "processingTime", 1,
                "events", new Object[]{ev("L1", "a", 1000), ev("L2", "b", 2000)}));
        JsonNode r = post("/api/v1/jobs/j-drop/events/left", Map.of(
                "processingTime", 2,
                "events", new Object[]{ev("L3", "a", 3000)}));
        assertEquals("L1", r.path("data").path("items").get(0).path("evictedId").asText());
        assertEquals(2, status("j-drop").path("leftBuffered").asInt());
        assertEquals(1, status("j-drop").path("metrics").path("oldestEvicted").asInt());
    }

    @Test
    @DisplayName("job lifecycle: list, duplicate create conflicts, delete")
    void lifecycle() throws Exception {
        createJob("j-life", 0, 1, 0, 0, "REJECT");
        JsonNode listed = get("/api/v1/jobs");
        assertTrue(listed.path("data").isArray());
        assertTrue(listed.path("data").size() >= 1);

        HttpResponse<String> dup = rawPost("/api/v1/jobs", Map.of(
                "jobId", "j-life", "lowerBound", 0, "upperBound", 1));
        assertEquals(409, dup.statusCode());

        JsonNode deleted = delete("/api/v1/jobs/j-life");
        assertTrue(deleted.path("ok").asBoolean());
    }

    @Test
    @DisplayName("bad input returns structured 400 errors")
    void badInput() throws Exception {
        HttpResponse<String> bad = rawPost("/api/v1/jobs", Map.of(
                "jobId", "j-bad", "lowerBound", 100, "upperBound", 0));
        assertEquals(400, bad.statusCode());
        assertTrue(bad.body().contains("\"ok\":false"));
    }

    private HttpResponse<String> rawPost(String path, Object body)
            throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }
}
