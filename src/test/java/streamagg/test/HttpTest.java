package streamagg.test;

import streamagg.json.Json;
import streamagg.server.AggregateHttpServer;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.List;

/** End-to-end tests over real HTTP using the JDK HttpClient. */
public final class HttpTest {

    private static HttpClient client() {
        return HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    }

    private static HttpResponse<String> post(HttpClient c, int port, String path, String body)
            throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body))
                .build();
        return c.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static HttpResponse<String> get(HttpClient c, int port, String path)
            throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .GET().build();
        return c.send(req, HttpResponse.BodyHandlers.ofString());
    }

    private static Json.JsonValue j(String s) {
        return Json.parse(s);
    }

    public static void register(TestRunner r) {
        r.test("http: full retract-before-add acceptance flow over HTTP", () -> {
            try (AggregateHttpServer server = new AggregateHttpServer(0)) {
                server.start();
                int port = server.getPort();
                HttpClient c = client();

                Assert.assertEquals(200, get(c, port, "/health").statusCode(), "health up");

                // Reset to be safe
                Assert.assertEquals(200, post(c, port, "/v1/admin/reset", "{}").statusCode(), "reset");

                // v2 retract arrives before v1 add
                var buffered = j(post(c, port, "/v1/events/ingest",
                        "{\"eventId\":\"e1\",\"op\":\"RETRACT\",\"version\":2,\"opId\":\"r2\"}").body());
                Assert.assertEquals("BUFFERED", buffered.get("status").asString(), "buffered over http");

                var added = j(post(c, port, "/v1/events/ingest",
                        "{\"eventId\":\"e1\",\"op\":\"ADD\",\"key\":\"k1\",\"value\":42,\"version\":1,\"opId\":\"a1\"}").body());
                Assert.assertEquals("APPLIED", added.get("status").asString(), "add triggers drain");
                Assert.assertEquals(2, added.get("drained").asArray().size(), "both drained");

                // Aggregates net zero
                var aggs = j(get(c, port, "/v1/aggregates?verify=1").body());
                Assert.assertEquals(0L, aggs.get("aggregates").asArray().size(), "no net aggregates");
                Assert.assertTrue(aggs.get("verification").get("matchesLedgerRecomputation").asBoolean(),
                        "verification flag");
            }
        });

        r.test("http: batch corrections then replay reference check", () -> {
            try (AggregateHttpServer server = new AggregateHttpServer(0)) {
                server.start();
                int port = server.getPort();
                HttpClient c = client();

                String batch = """
                        {
                          "operations": [
                            {"eventId":"e1","op":"ADD","key":"k1","value":100,"version":1,"opId":"1"},
                            {"eventId":"e2","op":"ADD","key":"k1","value":50,"version":1,"opId":"2"},
                            {"eventId":"e3","op":"CORRECT","key":"k2","value":70,"version":3,"opId":"3"},
                            {"eventId":"e1","op":"CORRECT","key":"k1","value":110,"version":2,"opId":"4"},
                            {"eventId":"e3","op":"ADD","key":"k1","value":60,"version":1,"opId":"5"},
                            {"eventId":"e3","op":"CORRECT","key":"k1","value":70,"version":2,"opId":"6"},
                            {"eventId":"e2","op":"RETRACT","opId":"7"}
                          ]
                        }
                        """;
                var br = j(post(c, port, "/v1/events/batch", batch).body());
                Assert.assertEquals(7L, br.get("count").asLong(), "batch count");
                Assert.assertTrue(br.get("buffered").asLong() >= 1, "e3v3/e2v2 buffered at some point");

                // Final state: e1 k1=110; e2 added(50) then retracted; e3 k2=70
                var aggs = j(get(c, port, "/v1/aggregates?verify=1").body());
                var map = new java.util.LinkedHashMap<String, Json.JsonValue>();
                for (Json.JsonValue a : aggs.get("aggregates").asArray()) {
                    map.put(a.get("key").asString(), a);
                }
                Assert.assertEquals(new java.math.BigDecimal("110"), map.get("k1").get("sum").asBigDecimal(),
                        "k1 sum");
                Assert.assertEquals(1L, map.get("k1").get("count").asLong(), "k1 count");
                Assert.assertEquals(new java.math.BigDecimal("70"), map.get("k2").get("sum").asBigDecimal(),
                        "k2 sum");
                Assert.assertTrue(aggs.get("verification").get("matchesLedgerRecomputation").asBoolean(),
                        "streaming == ledger replay");

                // Replay the same raw operations through a fresh engine via /v1/replay
                var replay = j(post(c, port, "/v1/replay", batch).body());
                Assert.assertTrue(replay.get("matchesLedgerRecomputation").asBoolean(),
                        "fresh replay matches its ledger");
                var replayAggs = new java.util.LinkedHashMap<String, Json.JsonValue>();
                for (Json.JsonValue a : replay.get("aggregatesAfterReplay").asArray()) {
                    replayAggs.put(a.get("key").asString(), a);
                }
                Assert.assertEquals(new java.math.BigDecimal("110"),
                        replayAggs.get("k1").get("sum").asBigDecimal(), "replay k1");
                Assert.assertEquals(new java.math.BigDecimal("70"),
                        replayAggs.get("k2").get("sum").asBigDecimal(), "replay k2");
            }
        });

        r.test("http: duplicate opId in batch is idempotent", () -> {
            try (AggregateHttpServer server = new AggregateHttpServer(0)) {
                server.start();
                int port = server.getPort();
                HttpClient c = client();
                String body = "{\"operations\":["
                        + "{\"eventId\":\"e1\",\"op\":\"ADD\",\"key\":\"k\",\"value\":10,\"opId\":\"t\"},"
                        + "{\"eventId\":\"e1\",\"op\":\"ADD\",\"key\":\"k\",\"value\":10,\"opId\":\"t\"}"
                        + "]}";
                var br = j(post(c, port, "/v1/events/batch", body).body());
                Assert.assertEquals(1L, br.get("applied").asLong(), "one applied");
                Assert.assertEquals(1L, br.get("duplicate").asLong(), "one duplicate");
                var aggs = j(get(c, port, "/v1/aggregates").body());
                Json.JsonValue agg = aggs.get("aggregates").asArray().get(0);
                Assert.assertEquals(1L, agg.get("count").asLong(), "count not doubled");
            }
        });

        r.test("http: invalid and malformed requests return 400", () -> {
            try (AggregateHttpServer server = new AggregateHttpServer(0)) {
                server.start();
                int port = server.getPort();
                HttpClient c = client();
                Assert.assertEquals(400, post(c, port, "/v1/events/ingest", "not-json").statusCode(),
                        "malformed json");
                Assert.assertEquals(400, post(c, port, "/v1/events/ingest",
                        "{\"eventId\":\"e1\",\"op\":\"ADD\",\"key\":\"k\"}").statusCode(),
                        "missing value");
                Assert.assertEquals(405, get(c, port, "/v1/events/ingest").statusCode(),
                        "wrong method");
            }
        });

        r.test("http: ledger and events endpoints show resolved and pending state", () -> {
            try (AggregateHttpServer server = new AggregateHttpServer(0)) {
                server.start();
                int port = server.getPort();
                HttpClient c = client();
                post(c, port, "/v1/events/ingest",
                        "{\"eventId\":\"e1\",\"op\":\"RETRACT\",\"version\":2,\"opId\":\"r\"}");
                var events = j(get(c, port, "/v1/events").body());
                Assert.assertTrue(events.get("pending").asArray().size() >= 1, "pending visible");

                post(c, port, "/v1/events/ingest",
                        "{\"eventId\":\"e1\",\"op\":\"ADD\",\"key\":\"k\",\"value\":5,\"version\":1,\"opId\":\"a\"}");
                var ledger = j(get(c, port, "/v1/ledger").body());
                Assert.assertEquals(2L, ledger.get("count").asLong(), "two ledger entries");
            }
        });

        r.test("http: reset clears all state", () -> {
            try (AggregateHttpServer server = new AggregateHttpServer(0)) {
                server.start();
                int port = server.getPort();
                HttpClient c = client();
                post(c, port, "/v1/events/ingest",
                        "{\"eventId\":\"e1\",\"op\":\"ADD\",\"key\":\"k\",\"value\":1}");
                post(c, port, "/v1/admin/reset", "{}");
                var aggs = j(get(c, port, "/v1/aggregates").body());
                Assert.assertEquals(0L, aggs.get("aggregates").asArray().size(), "aggregates cleared");
                var ledger = j(get(c, port, "/v1/ledger").body());
                Assert.assertEquals(0L, ledger.get("count").asLong(), "ledger cleared");
            }
        });
    }
}
