package orderedevents;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import orderedevents.json.Json;
import orderedevents.json.JsonWriter;
import orderedevents.service.EventService;
import orderedevents.service.ServerConfig;
import orderedevents.web.ApiServer;

/**
 * End-to-end tests over real HTTP (JDK {@code com.sun.net.httpserver.HttpServer}
 * + {@code java.net.http.HttpClient}). No JUnit: a tiny assert harness keeps
 * the project dependency-free.
 *
 * <p>Runs a fresh server on an ephemeral port with tight, deterministic limits:
 * maxInFlight=3, maxConcurrent=2, timeout 300ms, backoff 20ms.
 */
public final class OrderedEventsHttpTest {

    private static final long TIMEOUT_MS = 300;
    private static final long BACKOFF_MS = 20;

    private static int failures;
    private static int checks;

    private static String base;
    private static HttpClient http;

    public static void main(String[] args) throws Exception {
        ServerConfig cfg = new ServerConfig(0, 3, 2, 3, 10, TIMEOUT_MS, 60_000, BACKOFF_MS, 30_000);
        EventService service = new EventService(cfg);
        ApiServer server = new ApiServer(cfg, service);
        server.start();
        base = "http://localhost:" + server.boundPort();
        http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();

        try {
            testHealth();
            testOrderedCommitWithSlowHead();
            testTimeoutHeadUnblocksLaterEvents();
            testRetryExhaustionPreservesOrder();
            testBufferCapRejects();
            testCancelBuffered();
            testCancelRunning();
            testCancelCommittedRejectedAndNoFurtherSubmit();
            testPartitionsIndependent();
            testRetryThenSuccess();
            testValidationErrors();
        } catch (Throwable t) {
            failures++;
            System.out.println("FATAL: unexpected exception in test suite:");
            t.printStackTrace(System.out);
        } finally {
            server.stop();
            service.shutdown();
        }

        System.out.println();
        System.out.println("checks=" + checks + " failures=" + failures);
        if (failures != 0) {
            System.exit(1);
        }
    }

    // ------------------------------------------------------------- tests

    static void testHealth() throws Exception {
        section("health + partitions listing");
        HttpResponse<String> r = get("/health");
        check(r.statusCode() == 200, "health 200");
        check("ok".equals(asMap(Json.parse(r.body())).get("status")), "health status ok");

        HttpResponse<String> rp = get("/partitions");
        check(rp.statusCode() == 200, "list partitions 200");
    }

    static void testOrderedCommitWithSlowHead() throws Exception {
        section("ordered commit: settled later events wait behind a slow head");
        String p = "p-order";
        long t0 = System.currentTimeMillis();

        String e1 = submit(p, Map.of(
                "payload", "first",
                "attempts", List.of(Map.of("delayMillis", 500, "behavior", "succeed")),
                // Slower than the server's 300ms default timeout, so give it room;
                // this test is about ordering, not timeout.
                "timeoutMillis", 1000));
        String e2 = submit(p, Map.of(
                "payload", "second",
                "attempts", List.of(Map.of("delayMillis", 50, "behavior", "succeed"))));
        String e3 = submit(p, Map.of(
                "payload", "third",
                "attempts", List.of(Map.of("delayMillis", 100, "behavior", "succeed"))));
        long submittedAt = System.currentTimeMillis();

        // Start the long-poll immediately on a background thread so its blocking
        // duration is measured from well before the 500ms head settles.
        java.util.concurrent.Future<List<Map<String, Object>>> pollFuture =
                java.util.concurrent.Executors.newSingleThreadExecutor()
                        .submit(() -> results(p, -1, 2L, 3000));

        // e2/e3 finish before e1, but nothing may be committed until e1 settles.
        Thread.sleep(300);
        List<Map<String, Object>> early = results(p, -1, 0);
        check(early.isEmpty(), "no results while head unsettled (got " + early.size() + ")");

        List<Map<String, Object>> all = pollFuture.get(5, java.util.concurrent.TimeUnit.SECONDS);
        long blockedFor = System.currentTimeMillis() - submittedAt;
        // The head's 500ms delay starts when its attempt starts (shortly after
        // submit), so unblock happens a bit under 500ms from submit return —
        // but nowhere near the ~50-100ms at which e2/e3 settled.
        check(blockedFor >= 400,
                "long-poll stayed blocked until the slow head settled (unblocked "
                        + blockedFor + "ms after submit, e2/e3 settled ~50-100ms)");

        checkSeqs(all, List.of(0L, 1L, 2L), "commit order = input order");
        check(all.get(0).get("id").equals(e1), "entry[0] is the slow first event");
        check(all.get(1).get("id").equals(e2), "entry[1] is second");
        check(all.get(2).get("id").equals(e3), "entry[2] is third");
        check("SUCCEEDED".equals(all.get(0).get("status")), "head committed SUCCEEDED");
        check("first".equals(all.get(0).get("value")), "head value echoed");

        long headCommitAt = num(all.get(0).get("committedAtMillis"));
        long secondCommitAt = num(all.get(1).get("committedAtMillis"));
        check(headCommitAt >= t0 + 480, "head committed only after its 500ms work");
        check(secondCommitAt >= headCommitAt, "second committed no earlier than head");
        System.out.println("    (e2/e3 settled early but waited behind e1; barrier opened at +"
                + (headCommitAt - t0) + "ms)");
    }

    static void testTimeoutHeadUnblocksLaterEvents() throws Exception {
        section("acceptance: first event TIMES OUT while later ones finish first");
        String p = "p-timeout";
        // e0 hangs past the 300ms deadline -> TIMED_OUT placeholder after all 2 attempts.
        String slow = submit(p, Map.of(
                "payload", "slow",
                "attempts", List.of(Map.of("delayMillis", 5000, "behavior", "timeout")),
                "maxAttempts", 2));
        String fast1 = submit(p, Map.of(
                "payload", "fast-1",
                "attempts", List.of(Map.of("delayMillis", 30, "behavior", "succeed"))));
        String fast2 = submit(p, Map.of(
                "payload", "fast-2",
                "attempts", List.of(Map.of("delayMillis", 60, "behavior", "succeed"))));

        long t0 = System.currentTimeMillis();
        List<Map<String, Object>> all = pollResults(p, -1, 3, 5000);
        long elapsed = System.currentTimeMillis() - t0;
        checkSeqs(all, List.of(0L, 1L, 2L), "timeout head still commits in seq order");
        check("TIMED_OUT".equals(all.get(0).get("status")),
                "exhausted head committed as TIMED_OUT placeholder");
        check(all.get(0).get("error") != null, "placeholder carries an error detail");
        check(num(all.get(0).get("attempts")) == 2, "placeholder records 2 attempts");
        check("SUCCEEDED".equals(all.get(1).get("status")), "fast-1 success behind placeholder");
        check("SUCCEEDED".equals(all.get(2).get("status")), "fast-2 success behind placeholder");
        // 2 attempts x 300ms deadline + 1 backoff ≈ 620ms. Allow scheduling slack:
        // what matters is two distinct deadline waits happened, not one 300ms cut.
        check(elapsed >= TIMEOUT_MS * 2 + BACKOFF_MS - 120,
                "timeout retried across the full budget (elapsed=" + elapsed + "ms)");
        check(elapsed < 1500, "hung attempt was cut off by the deadline, not run to 5000ms");
        System.out.println("    (head placeholder committed after " + elapsed
                + "ms; later events had settled long before)");
    }

    static void testRetryExhaustionPreservesOrder() throws Exception {
        section("acceptance: retry-exhausted failure placeholder keeps position");
        String p = "p-retry";
        submit(p, Map.of(
                "payload", "fail-twice",
                "attempts", List.of(Map.of("delayMillis", 20, "behavior", "fail", "error", "boom")),
                "maxAttempts", 2));
        submit(p, Map.of(
                "payload", "fail-fast",
                "attempts", List.of(Map.of("delayMillis", 5, "behavior", "fail", "error", "nope")),
                "maxAttempts", 1));
        submit(p, Map.of(
                "payload", "ok",
                "attempts", List.of(Map.of("delayMillis", 10, "behavior", "succeed"))));

        List<Map<String, Object>> all = pollResults(p, -1, 3, 4000);
        checkSeqs(all, List.of(0L, 1L, 2L), "placeholders and success in input order");
        check("FAILED".equals(all.get(0).get("status")), "seq0 FAILED placeholder");
        check("boom".equals(all.get(0).get("error")), "placeholder error preserved");
        check(num(all.get(0).get("attempts")) == 2, "seq0 used both attempts");
        check("FAILED".equals(all.get(1).get("status")), "seq1 FAILED placeholder");
        check(num(all.get(1).get("attempts")) == 1, "seq1 had no retries");
        check("SUCCEEDED".equals(all.get(2).get("status")), "seq2 SUCCEEDED despite earlier failures");
    }

    static void testBufferCapRejects() throws Exception {
        section("acceptance: in-flight buffer cap rejects overflow with 503");
        String p = "p-cap";
        List<String> admitted = new ArrayList<>();
        int rejected = 0;
        for (int i = 0; i < 5; i++) {
            Resp rr = postRaw("/partitions/" + p + "/events", JsonWriter.write(Map.of(
                    "payload", "ev-" + i,
                    "attempts", List.of(Map.of("delayMillis", 400, "behavior", "succeed")))));
            if (rr.status == 202) {
                admitted.add(asMap(Json.parse(rr.body)).get("id").toString());
            } else if (rr.status == 503) {
                rejected++;
                String err = asMap(Json.parse(rr.body)).get("error").toString();
                check(err.contains("buffer is full"), "503 explains the cap");
            } else {
                check(false, "unexpected status " + rr.status + ": " + rr.body);
            }
        }
        check(admitted.size() == 3, "exactly maxInFlight=3 admitted (got " + admitted.size() + ")");
        check(rejected == 2, "overflowing submits rejected (got " + rejected + ")");

        Map<String, Object> st = asMap(Json.parse(get("/partitions/" + p + "/status").body()));
        check(num(st.get("inFlightLimit")) == 3, "status exposes inFlightLimit");
        check(num(st.get("concurrencyLimit")) == 2, "status exposes concurrencyLimit");
        check(num(st.get("admitted")) == 3, "status admitted=3 while busy");

        // After the batch drains, capacity returns and new submits succeed.
        pollResults(p, -1, 3, 4000);
        Resp again = postRaw("/partitions/" + p + "/events", JsonWriter.write(Map.of(
                "payload", "after-drain",
                "attempts", List.of(Map.of("delayMillis", 0, "behavior", "succeed")))));
        check(again.status == 202, "capacity freed after commit");
        pollResults(p, 2, 1, 3000);
    }

    static void testCancelBuffered() throws Exception {
        section("acceptance: buffered cancel -> event never submits a result");
        String p = "p-cancel-buf";
        // Fill both concurrency slots with long events; the 3rd admitted event is buffered.
        submit(p, Map.of("payload", "occupy-1",
                "attempts", List.of(Map.of("delayMillis", 800, "behavior", "succeed"))));
        submit(p, Map.of("payload", "occupy-2",
                "attempts", List.of(Map.of("delayMillis", 800, "behavior", "succeed"))));
        String buffered = submit(p, Map.of("payload", "buffered",
                "attempts", List.of(Map.of("delayMillis", 0, "behavior", "succeed"))));

        Thread.sleep(100);
        Map<String, Object> snap = asMap(Json.parse(get(evPath(p, buffered)).body()));
        check("PENDING".equals(snap.get("state")), "buffered event seen as PENDING pre-cancel");

        HttpResponse<String> c = post("/partitions/" + p + "/events/" + buffered + "/cancel", "");
        check(c.statusCode() == 200, "cancel buffered returns 200");
        check("CANCELLED".equals(asMap(Json.parse(c.body())).get("state")), "cancel snapshot CANCELLED");

        // Idempotent repeat cancel.
        HttpResponse<String> c2 = post("/partitions/" + p + "/events/" + buffered + "/cancel", "");
        check(c2.statusCode() == 200, "repeat cancel is idempotent");

        List<Map<String, Object>> done = pollResults(p, -1, 2, 4000);
        checkSeqs(done, List.of(0L, 1L), "only the two occupants commit; seq=2 skipped");
        check(done.stream().noneMatch(e -> buffered.equals(e.get("id"))),
                "cancelled event produced no result entry");

        // Freed capacity: a fresh submit is admitted (buffer slot was returned).
        Resp extra = postRaw("/partitions/" + p + "/events", JsonWriter.write(Map.of(
                "payload", "extra",
                "attempts", List.of(Map.of("delayMillis", 0, "behavior", "succeed")))));
        check(extra.status == 202, "capacity freed by cancellation admits new work");
        // afterSeq=1 returns only entries newer than seq 1: the new event at seq 3
        // (seq 2 was cancelled and produces no entry).
        List<Map<String, Object>> tail = pollResults(p, 1, 1, 3000);
        check("extra".equals(tail.get(0).get("value")),
                "new work commits after the cancelled gap; lastSeq tracks the gap");
        check(num(tail.get(0).get("seq")) == 3, "new event keeps its own input seq=3");
    }

    static void testCancelRunning() throws Exception {
        section("acceptance: cancel a running attempt -> abandoned, no result submitted");
        String p = "p-cancel-run";
        String running = submit(p, Map.of("payload", "running",
                "attempts", List.of(Map.of("delayMillis", 5000, "behavior", "succeed"))));
        Thread.sleep(150);
        HttpResponse<String> c = post("/partitions/" + p + "/events/" + running + "/cancel", "");
        check(c.statusCode() == 200, "cancel running returns 200");

        // Behind it, another event must flow through and commit normally.
        String behind = submit(p, Map.of("payload", "behind",
                "attempts", List.of(Map.of("delayMillis", 30, "behavior", "succeed"))));
        List<Map<String, Object>> all = pollResults(p, -1, 1, 3000);
        checkSeqs(all, List.of(1L), "only seq=1 commits; cancelled seq=0 leaves a gap");
        check(all.get(0).get("id").equals(behind), "the later event is what committed");

        Map<String, Object> st = asMap(Json.parse(get("/partitions/" + p + "/status").body()));
        check(num(st.get("admitted")) == 0, "no admitted slots linger after cancel");
    }

    static void testCancelCommittedRejectedAndNoFurtherSubmit() throws Exception {
        section("acceptance: cancel after commit refused (results can't be un-submitted)");
        String p = "p-cancel-late";
        String eid = submit(p, Map.of("payload", "quick",
                "attempts", List.of(Map.of("delayMillis", 10, "behavior", "succeed"))));
        pollResults(p, -1, 1, 3000);

        HttpResponse<String> c = postRaw0("/partitions/" + p + "/events/" + eid + "/cancel");
        check(c.statusCode() == 409, "cancel of committed event -> 409");
        List<Map<String, Object>> after = results(p, -1, 0);
        check(after.size() == 1 && after.get(0).get("id").equals(eid),
                "the committed result stays exactly once");
    }

    static void testPartitionsIndependent() throws Exception {
        section("partitions order independently and enforce independent caps");
        String a = "p-indep-a";
        String b = "p-indep-b";
        submit(a, Map.of("payload", "a-slow",
                "attempts", List.of(Map.of("delayMillis", 400, "behavior", "succeed"))));
        submit(a, Map.of("payload", "a-fast",
                "attempts", List.of(Map.of("delayMillis", 20, "behavior", "succeed"))));
        submit(b, Map.of("payload", "b-1",
                "attempts", List.of(Map.of("delayMillis", 20, "behavior", "succeed"))));
        submit(b, Map.of("payload", "b-2",
                "attempts", List.of(Map.of("delayMillis", 40, "behavior", "succeed"))));

        List<Map<String, Object>> rb = pollResults(b, -1, 2, 3000);
        checkSeqs(rb, List.of(0L, 1L), "partition B commits without waiting on A's slow head");

        List<Map<String, Object>> ra = pollResults(a, -1, 2, 3000);
        checkSeqs(ra, List.of(0L, 1L), "partition A still ordered");

        // Independent buffer cap: A is empty here (2 committed), so it admits 3 fresh ones;
        // this also proves cap accounting is per partition.
        int admitted = 0;
        for (int i = 0; i < 3; i++) {
            Resp rr = postRaw("/partitions/" + a + "/events", JsonWriter.write(Map.of(
                    "payload", "fill-" + i,
                    "attempts", List.of(Map.of("delayMillis", 300, "behavior", "succeed")))));
            if (rr.status == 202) {
                admitted++;
            }
        }
        check(admitted == 3, "partition A accepts its own 3-event cap independently");
    }

    static void testRetryThenSuccess() throws Exception {
        section("retry: a failing first attempt succeeds on retry and commits SUCCEEDED");
        String p = "p-retry-ok";
        submit(p, Map.of(
                "payload", "retry-me",
                "attempts", List.of(
                        Map.of("delayMillis", 5, "behavior", "fail", "error", "transient"),
                        Map.of("delayMillis", 5, "behavior", "succeed")),
                "maxAttempts", 3));
        List<Map<String, Object>> all = pollResults(p, -1, 1, 3000);
        check("SUCCEEDED".equals(all.get(0).get("status")), "retry success -> SUCCEEDED");
        check(num(all.get(0).get("attempts")) == 2, "succeeded on attempt 2");
        check(all.get(0).get("error") == null, "no error on eventual success");
    }

    static void testValidationErrors() throws Exception {
        section("input validation: bad bodies rejected with 400, unknown routes 404");
        check(postRaw("/partitions/p-validation/events", "{not json}").status == 400, "malformed JSON -> 400");
        check(postRaw("/partitions/p-validation/events", JsonWriter.write(Map.of(
                "attempts", List.of(Map.of("behavior", "bogus"))))).status == 400, "bad behavior -> 400");
        check(postRaw("/partitions/p-validation/events", JsonWriter.write(Map.of(
                "maxAttempts", 0))).status == 400, "maxAttempts 0 -> 400");
        check(postRaw("/partitions/p-validation/events", JsonWriter.write(Map.of(
                "timeoutMillis", 999999))).status == 400, "timeout over cap -> 400");
        check(postRaw("/partitions/../events", JsonWriter.write(Map.of())).status == 400,
                "illegal partition name -> 400");
        check(get("/partitions/does-not-exist/status").statusCode() == 404, "missing partition -> 404");
        check(get("/nope").statusCode() == 404, "unknown route -> 404");
    }

    // ------------------------------------------------------------- helpers

    record Resp(int status, String body) {
    }

    static String submit(String partition, Map<String, Object> event) throws Exception {
        Resp r = postRaw("/partitions/" + partition + "/events", JsonWriter.write(event));
        if (r.status != 202) {
            throw new AssertionError("submit failed: " + r.status + " " + r.body);
        }
        return asMap(Json.parse(r.body)).get("id").toString();
    }

    static List<Map<String, Object>> results(String partition, long afterSeq, long waitMillis)
            throws Exception {
        return results(partition, afterSeq, null, waitMillis);
    }

    static List<Map<String, Object>> results(String partition, long afterSeq, Long waitForSeq,
                                             long waitMillis) throws Exception {
        String url = "/partitions/" + partition + "/results?afterSeq=" + afterSeq
                + "&waitMillis=" + waitMillis;
        if (waitForSeq != null) {
            url += "&waitForSeq=" + waitForSeq;
        }
        HttpResponse<String> r = get(url);
        if (r.statusCode() != 200) {
            throw new AssertionError("results failed: " + r.statusCode() + " " + r.body());
        }
        List<?> raw = (List<?>) asMap(Json.parse(r.body())).get("results");
        List<Map<String, Object>> out = new ArrayList<>();
        for (Object o : raw) {
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) o;
            out.add(m);
        }
        return out;
    }

    /**
     * Long-polls until the partition commits at least one entry with
     * seq >= {@code afterSeq + expected} (waiting on an exact watermark makes
     * multi-commit batches deterministic), then returns entries after afterSeq.
     */
    static List<Map<String, Object>> pollResults(String partition, long afterSeq, int expected,
                                                 long timeoutMs) throws Exception {
        long deadline = System.currentTimeMillis() + timeoutMs;
        long watermark = afterSeq + expected;
        while (true) {
            long left = deadline - System.currentTimeMillis();
            if (left <= 0) {
                break;
            }
            List<Map<String, Object>> got = results(partition, afterSeq, watermark,
                    Math.min(500, left));
            if (got.size() >= expected && lastSeq(got) >= watermark) {
                return got;
            }
        }
        List<Map<String, Object>> partial = results(partition, afterSeq, null, 0);
        check(false, "timed out waiting for watermark seq>=" + watermark + " in '" + partition
                + "', got " + partial.size() + " entries");
        return partial;
    }

    static long lastSeq(List<Map<String, Object>> entries) {
        long s = Long.MIN_VALUE;
        for (Map<String, Object> e : entries) {
            s = Math.max(s, num(e.get("seq")));
        }
        return s;
    }

    static String evPath(String partition, String id) {
        return "/partitions/" + partition + "/events/" + id;
    }

    static void checkSeqs(List<Map<String, Object>> entries, List<Long> expected, String what) {
        List<Long> actual = new ArrayList<>();
        for (Map<String, Object> e : entries) {
            actual.add(num(e.get("seq")));
        }
        check(actual.equals(expected), what + " (seqs expected " + expected + " got " + actual + ")");
    }

    static HttpResponse<String> get(String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .timeout(Duration.ofSeconds(5)).GET().build();
        return http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    static HttpResponse<String> post(String path, String body) throws Exception {
        return post0(path, body);
    }

    static HttpResponse<String> post0(String path, String body) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(base + path))
                .timeout(Duration.ofSeconds(5))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body == null ? "" : body, StandardCharsets.UTF_8))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    static Resp postRaw(String path, String body) throws Exception {
        HttpResponse<String> r = post0(path, body);
        return new Resp(r.statusCode(), r.body());
    }

    static HttpResponse<String> postRaw0(String path) throws Exception {
        return post0(path, "");
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> asMap(Object o) {
        return (Map<String, Object>) o;
    }

    static long num(Object o) {
        return ((Number) o).longValue();
    }

    static void check(boolean cond, String description) {
        checks++;
        if (cond) {
            System.out.println("  PASS " + description);
        } else {
            failures++;
            System.out.println("  FAIL " + description);
        }
    }

    static void section(String name) {
        System.out.println();
        System.out.println("[test] " + name);
    }
}
