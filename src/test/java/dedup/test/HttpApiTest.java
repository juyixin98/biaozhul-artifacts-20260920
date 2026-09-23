package dedup.test;

import dedup.DedupService;
import dedup.Json;
import dedup.http.DedupHttpServer;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dedup.test.Assert.assertEquals;
import static dedup.test.Assert.assertTrue;

/**
 * Full-stack tests over real HTTP on an ephemeral port.
 */
public class HttpApiTest {

    private DedupHttpServer server;
    private HttpClient client;
    private String base;

    @Test
    public void endToEndDedupWatermarkAndMigration() throws Exception {
        setUp();
        try {
            // health
            HttpResponse<String> health = get("/health");
            assertEquals(200, health.statusCode(), "health 200");
            assertEquals("ok", Json.parseObject(health.body()).get("status"), "health body");

            // create partition
            HttpResponse<String> created = put("/partitions/k",
                    Map.of("epoch", 7, "retentionMillis", 10_000));
            assertEquals(201, created.statusCode(), "created 201");
            assertEquals(7L, num(created, "epoch"), "created epoch 7");

            // duplicate create -> 409
            assertEquals(409, put("/partitions/k",
                    Map.of("retentionMillis", 10_000)).statusCode(), "dup create 409");

            // first event NEW, second DUPLICATE
            HttpResponse<String> e1 = post("/partitions/k/events",
                    Map.of("eventId", "A", "eventTime", 1_000, "epoch", 7));
            assertEquals(200, e1.statusCode(), "event 200");
            assertEquals(false, Json.parseObject(e1.body()).get("duplicate"), "A NEW");

            HttpResponse<String> e1again = post("/partitions/k/events",
                    Map.of("eventId", "A", "eventTime", 900, "epoch", 7));
            assertEquals(true, Json.parseObject(e1again.body()).get("duplicate"),
                    "A out-of-order redelivery DUPLICATE");

            // stale epoch via header
            HttpResponse<String> stale = post("/partitions/k/events",
                    Map.of("eventId", "B", "eventTime", 2_000),
                    Map.of("X-Routing-Epoch", "6"));
            assertEquals(409, stale.statusCode(), "stale header epoch 409");
            assertEquals("INVALID_ROUTING_VERSION",
                    Json.parseObject(stale.body()).get("error"), "fenced error code");

            // malformed JSON
            HttpResponse<String> bad = postRaw("/partitions/k/events", "{not json");
            assertEquals(400, bad.statusCode(), "malformed 400");

            // unknown partition
            assertEquals(404, post("/partitions/ghost/events",
                    Map.of("eventId", "x", "eventTime", 1)).statusCode(), "unknown 404");

            // watermark boundary: at 10999 A is still retained, at 11000 it is gone
            assertEquals(1L, num(post("/partitions/k/watermark",
                    Map.of("watermark", 10_999, "epoch", 7)), "retainedEventCount"),
                    "retained before boundary");
            assertEquals(0L, num(post("/partitions/k/watermark",
                    Map.of("watermark", 11_000, "epoch", 7)), "retainedEventCount"),
                    "released at boundary");

            HttpResponse<String> anew = post("/partitions/k/events",
                    Map.of("eventId", "A", "eventTime", 11_000, "epoch", 7));
            assertEquals(false, Json.parseObject(anew.body()).get("duplicate"),
                    "A is NEW again after expiry");

            // backwards watermark rejected
            HttpResponse<String> back = post("/partitions/k/watermark",
                    Map.of("watermark", 10_999, "epoch", 7));
            assertEquals(409, back.statusCode(), "backwards wm 409");

            // --- migration to new owner ---
            // seed a few ids on source key 'k' first (re-create after expiry scenario)
            post("/partitions/k/events", Map.of("eventId", "M1", "eventTime", 20_000, "epoch", 7));
            post("/partitions/k/events", Map.of("eventId", "M2", "eventTime", 21_000, "epoch", 7));

            HttpResponse<String> exported = post("/partitions/k/migration/export",
                    Map.of("epoch", 7));
            assertEquals(200, exported.statusCode(), "export 200");
            Map<String, Object> exportBody = Json.parseObject(exported.body());
            assertEquals("MIGRATING",
                    ((Map<?, ?>) exportBody.get("partition")).get("state"), "source frozen");

            // writes while migrating rejected
            HttpResponse<String> frozenWrite = post("/partitions/k/events",
                    Map.of("eventId", "M3", "eventTime", 22_000, "epoch", 7));
            assertEquals(409, frozenWrite.statusCode(), "frozen write 409");

            // import under the SAME epoch -> refused
            Map<String, Object> snapshot = (Map<String, Object>) exportBody.get("snapshot");
            HttpResponse<String> badImport = post("/partitions/k2/migration/import",
                    Map.of("snapshot", snapshot, "newEpoch", 7));
            assertEquals(409, badImport.statusCode(), "non-advancing epoch 409");
            assertEquals("INVALID_ROUTING_VERSION",
                    Json.parseObject(badImport.body()).get("error"), "import fencing");

            // correct import: newEpoch 8
            HttpResponse<String> imported = post("/partitions/k2/migration/import",
                    Map.of("snapshot", snapshot, "newEpoch", 8));
            assertEquals(200, imported.statusCode(), "import 200");
            assertEquals(8L, num(imported, "epoch"), "destination epoch 8");
            assertEquals(3L, num(imported, "retainedEventCount"),
                    "A (re-seen), M1, M2 traveled");

            // in-flight duplicates land on the new owner and are caught
            HttpResponse<String> dupAtDst = post("/partitions/k2/events",
                    Map.of("eventId", "M1", "eventTime", 20_000, "epoch", 8));
            assertEquals(true, Json.parseObject(dupAtDst.body()).get("duplicate"),
                    "M1 redelivery caught at destination");
            HttpResponse<String> newAtDst = post("/partitions/k2/events",
                    Map.of("eventId", "M9", "eventTime", 30_000, "epoch", 8));
            assertEquals(false, Json.parseObject(newAtDst.body()).get("duplicate"),
                    "M9 genuinely new");

            // old epoch still routed to the new owner -> rejected
            HttpResponse<String> staleToDst = post("/partitions/k2/events",
                    Map.of("eventId", "M2", "eventTime", 21_000, "epoch", 7));
            assertEquals(409, staleToDst.statusCode(), "old epoch to new owner 409");

            // complete source cutover
            HttpResponse<String> completed = post("/partitions/k/migration/complete",
                    Map.of("epoch", 7));
            assertEquals(200, completed.statusCode(), "complete 200");
            assertEquals("ACTIVE",
                    Json.parseObject(get("/partitions/k").body()).get("state"),
                    "source active again with empty state");
            assertEquals(0L, num(get("/partitions/k"), "retainedEventCount"),
                    "source state dropped");

            // listing shows both partitions
            List<?> keys = (List<?>) Json.parseObject(get("/partitions").body())
                    .get("partitions");
            assertTrue(keys.contains("k") && keys.contains("k2"), "both keys listed");

            // delete
            assertEquals(200, delete("/partitions/k2").statusCode(), "delete 200");
            assertEquals(404, get("/partitions/k2").statusCode(), "gone after delete");

        } finally {
            tearDown();
        }
    }

    @Test
    public void rejectsBadMethodsAndRoutes() throws Exception {
        setUp();
        try {
            // POST to /health -> 405
            HttpRequest req = HttpRequest.newBuilder(URI.create(base + "/health"))
                    .POST(HttpRequest.BodyPublishers.noBody())
                    .build();
            HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
            assertEquals(405, resp.statusCode(), "POST /health 405");

            // unknown route
            HttpResponse<String> nf = get("/nope");
            assertEquals(404, nf.statusCode(), "unknown route 404");
        } finally {
            tearDown();
        }
    }

    // ------------------------------------------------------------------
    // plumbing
    // ------------------------------------------------------------------

    private void setUp() throws Exception {
        server = new DedupHttpServer(0, new DedupService());
        server.start();
        base = "http://127.0.0.1:" + server.port();
        client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    }

    private void tearDown() {
        if (server != null) {
            server.stop();
        }
    }

    private HttpResponse<String> get(String path) throws Exception {
        return send("GET", path, null, null);
    }

    private HttpResponse<String> delete(String path) throws Exception {
        return send("DELETE", path, null, null);
    }

    private HttpResponse<String> put(String path, Map<String, Object> body) throws Exception {
        return send("PUT", path, body, null);
    }

    private HttpResponse<String> post(String path, Map<String, Object> body) throws Exception {
        return send("POST", path, body, null);
    }

    private HttpResponse<String> post(String path, Map<String, Object> body,
                                      Map<String, String> headers) throws Exception {
        return send("POST", path, body, headers);
    }

    private HttpResponse<String> postRaw(String path, String rawJson) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(rawJson))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private HttpResponse<String> send(String method, String path,
                                      Map<String, Object> body,
                                      Map<String, String> headers) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create(base + path));
        if (headers != null) {
            headers.forEach(b::header);
        }
        String payload = body == null ? "" : Json.write(body);
        HttpRequest.BodyPublisher pub = body == null
                ? HttpRequest.BodyPublishers.noBody()
                : HttpRequest.BodyPublishers.ofString(payload);
        b.header("Content-Type", "application/json").method(method, pub);
        return client.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    private static long num(HttpResponse<String> resp, String field) {
        return ((Number) Json.parseObject(resp.body()).get(field)).longValue();
    }
}
