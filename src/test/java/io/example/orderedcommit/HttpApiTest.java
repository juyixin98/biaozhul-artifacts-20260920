package io.example.orderedcommit;

import java.util.List;
import java.util.Map;

/**
 * End-to-end tests over real HTTP (JDK HttpServer + java.net.http.HttpClient),
 * including the three headline acceptance scenarios:
 * <ol>
 *   <li>first event times out while later ones finish first, output still in
 *       order;</li>
 *   <li>buffer cap returns 429;</li>
 *   <li>retry exhaustion commits a FAILURE placeholder in sequence order;</li>
 *   <li>cancellation never commits a result.</li>
 * </ol>
 */
final class HttpApiTest implements AutoCloseable {

    private final HttpFixture http;

    HttpApiTest() throws Exception {
        OrderedEventService.Config config =
                new OrderedEventService.Config(8, 16, 50, 10_000, 3, 20);
        this.http = HttpFixture.start(config);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> asMap(Object o) {
        return (Map<String, Object>) o;
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> asList(Object o) {
        return (List<Map<String, Object>>) o;
    }

    @Test
    void healthAndStatsAndPartitionLifecycle() throws Exception {
        Asserts.assertEquals(200, http.get("/health").status(), "health status");
        Asserts.assertEquals("ok", http.get("/health").body().get("status"), "health body");

        Asserts.assertEquals(200,
                http.put("/partitions/p-life", "{\"bufferCap\":5}").status(),
                "create partition");
        var stats = http.get("/partitions/p-life");
        Asserts.assertEquals(200, stats.status(), "partition stats");
        Asserts.assertEquals(5L, ((Number) stats.body().get("bufferCap")).longValue(),
                "buffer cap echoed");

        var list = http.get("/partitions");
        Asserts.assertTrue(
                asList(list.body().get("partitions")).stream()
                        .anyMatch(m -> "p-life".equals(m.get("name"))),
                "partition listed");
    }

    @Test
    void acceptanceTimeoutFirstLaterFinishesFirstOutputOrdered() throws Exception {
        Asserts.assertEquals(200,
                http.put("/partitions/acc", "{\"bufferCap\":10}").status(),
                "create");

        // seq0: work takes 2000ms but the attempt times out at 800ms, one attempt only.
        // seq1/seq2 finish in tens of ms, so they sit SUCCEEDED behind the head
        // for roughly 700ms: a wide, deterministic head-of-line buffering window.
        var r0 = http.post("/partitions/acc/events",
                "{\"payload\":\"slow\",\"delayMillis\":2000,\"timeoutMillis\":800,\"maxAttempts\":1}");
        Asserts.assertEquals(202, r0.status(), "submit seq0");
        // seq1: quick success
        var r1 = http.post("/partitions/acc/events",
                "{\"payload\":\"fast\",\"delayMillis\":40,\"timeoutMillis\":5000}");
        var r2 = http.post("/partitions/acc/events",
                "{\"payload\":\"fast2\",\"delayMillis\":60,\"timeoutMillis\":5000}");
        String id0 = (String) r0.body().get("id");
        String id1 = (String) r1.body().get("id");
        String id2 = (String) r2.body().get("id");

        // seq1 and seq2 finish long before seq0's timeout placeholder releases seq0.
        Asserts.waitFor(1_500, "tails SUCCEEDED first", () -> {
            Asserts.assertEquals("SUCCEEDED",
                    (String) http.get("/partitions/acc/events/" + id1).body().get("status"),
                    "id1 finished");
            Asserts.assertEquals("SUCCEEDED",
                    (String) http.get("/partitions/acc/events/" + id2).body().get("status"),
                    "id2 finished");
            // While seq0's slot is unresolved, nothing has been committed.
            Asserts.assertEquals(0,
                    asList(http.get("/partitions/acc/results").body().get("results")).size(),
                    "head-of-line buffered");
        });

        // The head is still RUNNING when its tails are already done.
        Asserts.assertEquals("RUNNING",
                (String) http.get("/partitions/acc/events/" + id0).body().get("status"),
                "head still running after tails finished");

        // Long-poll the ordered output.
        var out = http.get("/partitions/acc/results?waitMillis=3000");
        List<Map<String, Object>> results = asList(out.body().get("results"));
        Asserts.assertEquals(3, results.size(), "three output entries");
        Asserts.assertEquals(0L, ((Number) results.get(0).get("seq")).longValue(), "seq 0 first");
        Asserts.assertEquals("FAILURE", results.get(0).get("outcome"),
                "timeout -> failure placeholder");
        Asserts.assertTrue(String.valueOf(results.get(0).get("error")).toLowerCase().contains("timed out"),
                "placeholder mentions timeout: " + results.get(0).get("error"));
        Asserts.assertEquals(1L, ((Number) results.get(1).get("seq")).longValue(), "seq 1");
        Asserts.assertEquals("SUCCESS", results.get(1).get("outcome"), "seq1 success");
        Asserts.assertEquals(2L, ((Number) results.get(2).get("seq")).longValue(), "seq 2");
        Asserts.assertEquals("SUCCESS", results.get(2).get("outcome"), "seq2 success");
        Asserts.assertEquals(id0, results.get(0).get("eventId"), "id0 identity");
        Asserts.assertEquals(id2, results.get(2).get("eventId"), "id2 identity");
    }

    @Test
    void acceptanceBufferCapReturns429() throws Exception {
        Asserts.assertEquals(200,
                http.put("/partitions/cap", "{\"bufferCap\":2}").status(),
                "create tiny partition");
        String longJob =
                "{\"delayMillis\":1500,\"timeoutMillis\":5000,\"maxAttempts\":1}";
        Asserts.assertEquals(202, http.post("/partitions/cap/events", longJob).status(), "first");
        Asserts.assertEquals(202, http.post("/partitions/cap/events", longJob).status(), "second");
        var third = http.post("/partitions/cap/events", longJob);
        Asserts.assertEquals(429, third.status(), "third rejected");
        Asserts.assertTrue(
                String.valueOf(asMap(third.body().get("error")).get("message"))
                        .contains("buffer full"),
                "error message names the buffer");

        var stats = http.get("/partitions/cap");
        Asserts.assertEquals(2L, ((Number) stats.body().get("outstanding")).longValue(),
                "only two outstanding");
    }

    @Test
    void acceptanceRetriesExhaustedThenFailureInOrder() throws Exception {
        Asserts.assertEquals(200,
                http.put("/partitions/ret", "{\"bufferCap\":10}").status(),
                "create");
        var fail = http.post("/partitions/ret/events",
                "{\"fail\":true,\"delayMillis\":15,\"timeoutMillis\":5000,"
                        + "\"maxAttempts\":3,\"retryDelayMillis\":25}");
        var ok = http.post("/partitions/ret/events",
                "{\"delayMillis\":15,\"timeoutMillis\":5000,\"maxAttempts\":1}");
        Asserts.assertEquals(202, fail.status(), "submit fail");
        String failId = (String) fail.body().get("id");

        Asserts.waitFor(2_000, "failed event retried 3 times", () -> {
            var event = http.get("/partitions/ret/events/" + failId);
            Asserts.assertEquals(3L, ((Number) event.body().get("attempts")).longValue(),
                    "3 attempts");
        });

        var out = http.get("/partitions/ret/results?waitMillis=3000");
        List<Map<String, Object>> results = asList(out.body().get("results"));
        Asserts.assertEquals(2, results.size(), "failure + success");
        Asserts.assertEquals(0L, ((Number) results.get(0).get("seq")).longValue(), "failure first");
        Asserts.assertEquals("FAILURE", results.get(0).get("outcome"),
                "placeholder in failure's slot");
        Asserts.assertEquals(3L, ((Number) results.get(0).get("attempts")).longValue(),
                "attempts copied to output");
        Asserts.assertEquals(1L, ((Number) results.get(1).get("seq")).longValue(),
                "success stays after failure");
        Asserts.assertEquals("SUCCESS", results.get(1).get("outcome"), "success outcome");
        Asserts.assertEquals(ok.body().get("id"), results.get(1).get("eventId"),
                "success identity");
        Asserts.assertTrue(
                String.valueOf(results.get(0).get("error")).contains("injected processing failure"),
                "error text preserved");
    }

    @Test
    void acceptanceCancelProducesNoResult() throws Exception {
        Asserts.assertEquals(200,
                http.put("/partitions/cxl", "{\"bufferCap\":10}").status(),
                "create");
        var victim = http.post("/partitions/cxl/events",
                "{\"delayMillis\":2000,\"timeoutMillis\":5000,\"maxAttempts\":1}");
        var tail = http.post("/partitions/cxl/events",
                "{\"delayMillis\":30,\"timeoutMillis\":5000,\"maxAttempts\":1}");
        String victimId = (String) victim.body().get("id");

        Thread.sleep(100);
        var cancel = http.post("/partitions/cxl/events/" + victimId + "/cancel",
                "{\"reason\":\"acceptance test\"}");
        Asserts.assertEquals(200, cancel.status(), "cancel accepted");
        Asserts.assertEquals("CANCELLED", cancel.body().get("status"), "status cancelled");
        Asserts.assertEquals("acceptance test", cancel.body().get("cancelReason"),
                "reason stored");

        var out = http.get("/partitions/cxl/results?waitMillis=3000");
        List<Map<String, Object>> results = asList(out.body().get("results"));
        Asserts.assertEquals(1, results.size(), "only tail committed");
        Asserts.assertEquals(1L, ((Number) results.get(0).get("seq")).longValue(),
                "cancelled slot skipped");
        Asserts.assertEquals(tail.body().get("id"), results.get(0).get("eventId"),
                "tail identity");
        for (Map<String, Object> r : results) {
            Asserts.assertFalse(victimId.equals(r.get("eventId")),
                    "victim must never appear in committed output");
        }

        // Cancelling again is idempotent; cancelling finished tail is 409.
        Asserts.assertEquals(200,
                http.post("/partitions/cxl/events/" + victimId + "/cancel", "{}").status(),
                "idempotent cancel");
        var doubleCancel = http.post(
                "/partitions/cxl/events/" + tail.body().get("id") + "/cancel", "{}");
        Asserts.assertEquals(409, doubleCancel.status(), "finished -> conflict");
    }

    @Test
    void errorsAreReportedAsJson() throws Exception {
        Asserts.assertEquals(404, http.get("/partitions/nope").status(), "missing partition");
        Asserts.assertEquals(400,
                http.put("/partitions/bad%20name", "{}").status(),
                "bad partition name (decoded to 'bad name')");
        Asserts.assertEquals(200,
                http.put("/partitions/err", "{}").status(),
                "empty body partition uses defaults");
        Asserts.assertEquals(400,
                http.post("/partitions/err/events", "not-json").status(),
                "malformed json");
        Asserts.assertEquals(400,
                http.post("/partitions/err/events", "{\"maxAttempts\":0}").status(),
                "validation error");
        Asserts.assertEquals(404,
                http.post("/partitions/err/events/missing-id/cancel", "{}").status(),
                "missing event");
        var bad = http.post("/partitions/err/events", "{\"delayMillis\":-5}");
        Asserts.assertEquals(400, bad.status(), "negative delay rejected");
        Asserts.assertInstanceOf(String.class,
                asMap(bad.body().get("error")).get("message"),
                "error message present");
    }

    @Test
    void globalStatsTrackInflightAndCommitted() throws Exception {
        Asserts.assertEquals(200,
                http.put("/partitions/g", "{\"bufferCap\":20}").status(),
                "create");
        for (int i = 0; i < 4; i++) {
            Asserts.assertEquals(202,
                    http.post("/partitions/g/events",
                            "{\"delayMillis\":400,\"timeoutMillis\":5000,\"maxAttempts\":1}")
                            .status(),
                    "submit " + i);
        }
        Asserts.waitFor(2_000, "4 in flight", () -> {
            var stats = http.get("/stats");
            Asserts.assertEquals(4L, ((Number) stats.body().get("inFlight")).longValue(),
                    "inflight rises");
            Asserts.assertTrue(((Number) stats.body().get("outstanding")).longValue() >= 4,
                    "outstanding counter");
        });
        Asserts.waitFor(3_000, "all committed", () -> {
            var stats = http.get("/stats");
            Asserts.assertEquals(4L, ((Number) stats.body().get("committed")).longValue(),
                    "committed counter");
            Asserts.assertEquals(0L, ((Number) stats.body().get("outstanding")).longValue(),
                    "drained");
        });
    }

    @Override
    public void close() {
        http.close();
    }
}
