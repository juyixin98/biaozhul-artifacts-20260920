package com.example.sessionwindow;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Zero-dependency test harness. Run with:
 *   java -cp out/classes:out/test-classes com.example.sessionwindow.Tests
 *
 * Includes the acceptance scenario: events at 0, 20, then late 10 with gap=10
 * bridge the two sessions; retract must not double count; after recovery the
 * session id and final aggregation must be stable.
 */
public final class Tests {

    private static int passed = 0;
    private static int failed = 0;
    private static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) throws Exception {
        Tests t = new Tests();
        t.testNewSession();
        t.testBoundaryGapChainsInOrder();
        t.testGapSplitsSessions();
        t.testLateEventBridgesSessions();
        t.testChangelogFoldNeverDoubleCounts();
        t.testAutoWatermarkAllowsBridgeAtBoundary();
        t.testManualWatermarkRejects();
        t.testIdempotency();
        t.testRecoveryFromLog();
        t.testRecoveryRejectsAfterLoggedWatermark();
        t.testDeterminismAcrossReplay();
        t.testBatchAndHttpEndpoints();
        t.testUnknownClient();

        System.out.println();
        System.out.println("============================================");
        System.out.println("Tests: " + (passed + failed) + ", passed: " + passed + ", failed: " + failed);
        if (failed > 0) {
            System.out.println("--------------------------------------------");
            for (String f : failures) {
                System.out.println("FAIL " + f);
            }
            System.exit(1);
        }
    }

    // ---------------- tests ----------------

    void testNewSession() {
        check("new session starts at version 1", () -> {
            SessionEngine e = new SessionEngine(10, 10, null);
            SessionEngine.Result r = e.submit(new Event("k", 0, null, null));
            assertStatus(r, SessionEngine.Status.ACCEPTED);
            Map<String, Object> s = soleSession(e, "k");
            eq(1, s.get("version"), "version");
            eq(1, s.get("count"), "count");
            eq(0L, s.get("startTs"), "startTs");
            eq(0L, s.get("endTs"), "endTs");
        });
    }

    void testBoundaryGapChainsInOrder() {
        check("distance exactly == gap connects, transitively (0,10,20)", () -> {
            SessionEngine e = new SessionEngine(10, 10, null);
            for (long ts : new long[]{0, 10, 20}) {
                e.submit(new Event("k", ts, null, null));
            }
            Map<String, Object> s = soleSession(e, "k");
            eq("k#1", s.get("sessionId"), "sessionId");
            eq(3, s.get("count"), "count");
            eq(0L, s.get("startTs"), "startTs");
            eq(20L, s.get("endTs"), "endTs");
        });
    }

    void testGapSplitsSessions() {
        check("distance gap+1 splits into two sessions", () -> {
            SessionEngine e = new SessionEngine(10, 10, null);
            e.submit(new Event("k", 0, null, null));
            e.submit(new Event("k", 11, null, null));
            List<Map<String, Object>> list = sessionsOf(e, "k");
            eq(2, list.size(), "session count");
            eq("k#1", list.get(0).get("sessionId"), "first id");
            eq("k#2", list.get(1).get("sessionId"), "second id");
        });
    }

    void testLateEventBridgesSessions() {
        check("ACCEPTANCE: late event ts=10 bridges sessions from ts=0 and ts=20", () -> {
            SessionEngine e = new SessionEngine(10, 10, null);
            e.submit(new Event("k", 0, null, null));
            e.submit(new Event("k", 20, null, null));
            eq(2, sessionsOf(e, "k").size(), "two sessions before bridge");

            SessionEngine.Result r = e.submit(new Event("k", 10, null, null));
            assertStatus(r, SessionEngine.Status.ACCEPTED);

            List<Change> changes = r.changes();
            eq(2, changes.size(), "one RETRACT + one UPSERT");
            eq(Change.Kind.RETRACT, changes.get(0).kind(), "first change retracts k#2");
            eq("k#2", changes.get(0).sessionId(), "retracted id");
            eq(Change.Kind.UPSERT, changes.get(1).kind(), "second change upserts survivor");
            eq("k#1", changes.get(1).sessionId(), "survivor keeps smallest id");
            eq(3, changes.get(1).count(), "merged session has all 3 events");
            eq(0L, changes.get(1).startTs(), "merged startTs");
            eq(20L, changes.get(1).endTs(), "merged endTs");

            Map<String, Object> s = soleSession(e, "k");
            eq("k#1", s.get("sessionId"), "final stable session id");
            eq(3, s.get("count"), "final count");
        });
    }

    void testChangelogFoldNeverDoubleCounts() {
        check("folding the changelog never double counts (acceptance)", () -> {
            SessionEngine e = new SessionEngine(10, 10, null);
            e.submit(new Event("k", 0, null, null));
            e.submit(new Event("k", 20, null, null));
            e.submit(new Event("k", 10, null, null)); // bridge

            int acceptedEvents = 3;

            // Naive sequential fold (records in changelog order). Retract-before-upsert
            // inside the bridge revision can transiently under-count (0) but must
            // NEVER over-count the number of accepted events.
            Map<String, Integer> view = new LinkedHashMap<>();
            int maxObserved = -1;
            for (Change c : e.changelog()) {
                if (c.kind() == Change.Kind.RETRACT) {
                    view.remove(c.sessionId());
                } else {
                    view.put(c.sessionId(), c.count());
                }
                int total = view.values().stream().mapToInt(Integer::intValue).sum();
                if (total > maxObserved) {
                    maxObserved = total;
                }
                if (total > acceptedEvents) {
                    throw new AssertionError("double count detected: view total " + total
                            + " > accepted events " + acceptedEvents + " at seq " + c.seq());
                }
            }
            eq(acceptedEvents, maxObserved, "peak count equals accepted events, no more");
            eq(1, view.size(), "one live session");
            eq(3, view.get("k#1"), "final count 3");

            // Revision-atomic fold: records sharing a revision are applied together,
            // so no transient under/over-count is ever visible.
            Map<String, Integer> atomic = foldAtomic(e.changelog());
            int atomicTotal = atomic.values().stream().mapToInt(Integer::intValue).sum();
            eq(acceptedEvents, atomicTotal, "atomic fold count always equals accepted events");
            eq(3, atomic.get("k#1"), "atomic fold count 3");
            eq(1, atomic.size(), "atomic fold one session");

            // Stats agree with the materialized view: no double counting anywhere.
            eq(3L, e.stats().get("accepted"), "accepted counter");
        });
    }

    void testAutoWatermarkAllowsBridgeAtBoundary() {
        check("auto watermark = maxTs - lateness; event AT the watermark still accepted", () -> {
            SessionEngine e = new SessionEngine(10, 10, null);
            e.submit(new Event("k", 0, null, null));
            e.submit(new Event("k", 20, null, null));
            eq(10L, e.watermark(), "watermark after ts=20");
            SessionEngine.Result r = e.submit(new Event("k", 10, null, null));
            assertStatus(r, SessionEngine.Status.ACCEPTED);
        });
    }

    void testManualWatermarkRejects() {
        check("events strictly behind a defined watermark are rejected, not counted", () -> {
            SessionEngine e = new SessionEngine(10, 10, null);
            e.submit(new Event("k", 0, null, null));
            e.advanceWatermark(20);
            SessionEngine.Result r = e.submit(new Event("k", 19, null, null));
            assertStatus(r, SessionEngine.Status.REJECTED);
            eq(0, r.changes().size(), "rejected event emits no changes");
            Map<String, Object> s = soleSession(e, "k");
            eq(1, s.get("count"), "count unchanged after rejection");
            eq(1L, e.stats().get("rejected"), "rejected counter");
            eq(1L, e.stats().get("accepted"), "accepted counter");

            // on-time events still work
            SessionEngine.Result ok = e.submit(new Event("k", 25, null, null));
            assertStatus(ok, SessionEngine.Status.ACCEPTED);
        });
    }

    void testIdempotency() {
        check("same clientId is processed once (DUPLICATE, never counted twice)", () -> {
            SessionEngine e = new SessionEngine(10, 10, null);
            SessionEngine.Result a = e.submit(new Event("k", 0, "evt-1", null));
            assertStatus(a, SessionEngine.Status.ACCEPTED);
            SessionEngine.Result b = e.submit(new Event("k", 0, "evt-1", null));
            assertStatus(b, SessionEngine.Status.DUPLICATE);
            eq(0, b.changes().size(), "duplicate emits no changes");
            eq(1, soleSession(e, "k").get("count"), "count still 1");
            eq(1L, e.stats().get("duplicates"), "duplicate counter");
            eq(1L, e.stats().get("accepted"), "accepted counter");
        });
    }

    void testRecoveryFromLog() {
        check("RECOVERY: new engine on same log restores sessions, changelog, ids, watermark", () -> {
            Path dir = Files.createTempDirectory("sw-recovery-");
            EventLog log1 = new EventLog(dir.resolve("event-log.jsonl"));
            SessionEngine e1 = new SessionEngine(10, 10, log1);
            e1.submit(new Event("k", 0, "a", null));
            e1.submit(new Event("k", 20, "b", null));
            e1.advanceWatermark(5);
            e1.submit(new Event("k", 10, "c", null)); // bridges
            log1.close();

            EventLog log2 = new EventLog(dir.resolve("event-log.jsonl"));
            SessionEngine e2 = new SessionEngine(10, 10, log2);

            eq(Json.write(e1.sessions()), Json.write(e2.sessions()), "sessions equal after recovery");
            Map<String, Object> s = soleSession(e2, "k");
            eq("k#1", s.get("sessionId"), "stable session id after recovery");
            eq(3, s.get("count"), "aggregation stable after recovery");

            eq(e1.changelog().size(), e2.changelog().size(), "changelog length");
            for (int i = 0; i < e1.changelog().size(); i++) {
                eq(Json.write(e1.changelog().get(i).toMap()),
                        Json.write(e2.changelog().get(i).toMap()),
                        "changelog record " + i);
            }
            eq(e1.watermark(), e2.watermark(), "watermark restored");
            eq(3L, e2.stats().get("accepted"), "accepted counter restored");
            log2.close();
        });
    }

    void testRecoveryRejectsAfterLoggedWatermark() {
        check("RECOVERY: logged watermark still rejects late events after restart", () -> {
            Path dir = Files.createTempDirectory("sw-wm-");
            EventLog log1 = new EventLog(dir.resolve("event-log.jsonl"));
            SessionEngine e1 = new SessionEngine(10, 10, log1);
            e1.submit(new Event("k", 100, null, null));
            e1.advanceWatermark(100);
            log1.close();

            EventLog log2 = new EventLog(dir.resolve("event-log.jsonl"));
            SessionEngine e2 = new SessionEngine(10, 10, log2);
            eq(100L, e2.watermark(), "watermark replayed");
            SessionEngine.Result r = e2.submit(new Event("k", 99, null, null));
            assertStatus(r, SessionEngine.Status.REJECTED);
            log2.close();
        });
    }

    void testDeterminismAcrossReplay() {
        check("id stability under interleaved keys and multi-session bridges", () -> {
            Path dir = Files.createTempDirectory("sw-determ-");
            EventLog log1 = new EventLog(dir.resolve("event-log.jsonl"));
            SessionEngine e1 = new SessionEngine(10, 10, log1);
            long[] tss = {0, 20, 40, 5, 35, 10};
            int n = 0;
            for (long ts : tss) {
                e1.submit(new Event("alpha", ts, "a" + (n++), null));
            }
            e1.submit(new Event("beta", 7, null, null));
            e1.submit(new Event("beta", 27, null, null));
            e1.submit(new Event("beta", 17, null, null));
            log1.close();

            EventLog log2 = new EventLog(dir.resolve("event-log.jsonl"));
            SessionEngine e2 = new SessionEngine(10, 10, log2);
            eq(Json.write(e1.sessions()), Json.write(e2.sessions()), "identical sessions");
            eq(Json.write(e1.changelog()), Json.write(e2.changelog()), "identical changelog");
            log2.close();
        });
    }

    void testBatchAndHttpEndpoints() {
        check("HTTP: events, batch, output, sessions, watermark rejection, reset", () -> {
            Path dir = Files.createTempDirectory("sw-http-");
            EventLog log = new EventLog(dir.resolve("event-log.jsonl"));
            SessionEngine engine = new SessionEngine(10, 10, log);
            ApiServer api = new ApiServer(0, engine);
            api.start();
            int port = api.port();
            try {
                HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

                // health
                eq(200, get(http, port, "/health").statusCode(), "health 200");

                // event at 0
                HttpResponse<String> r1 = post(http, port, "/events",
                        "{\"key\":\"userA\",\"ts\":0,\"clientId\":\"e1\"}");
                eq(200, r1.statusCode(), "POST /events 200");
                eq("ACCEPTED", Json.parseObject(r1.body()).get("status"), "event 0 accepted");

                // event at 20 via batch
                HttpResponse<String> rb = post(http, port, "/events/batch",
                        "{\"events\":[{\"key\":\"userA\",\"ts\":20,\"clientId\":\"e2\"}]}");
                eq(200, rb.statusCode(), "batch 200");

                // two sessions
                HttpResponse<String> rs = get(http, port, "/sessions?key=userA");
                Map<String, Object> sessions = Json.parseObject(rs.body());
                @SuppressWarnings("unchecked")
                List<Object> list = (List<Object>) sessions.get("sessions");
                eq(2, list.size(), "two sessions over HTTP");

                // bridge with the late event at 10
                HttpResponse<String> rBridge = post(http, port, "/events",
                        "{\"key\":\"userA\",\"ts\":10,\"clientId\":\"e3\"}");
                Map<String, Object> bridge = Json.parseObject(rBridge.body());
                eq("ACCEPTED", bridge.get("status"), "bridge accepted");
                @SuppressWarnings("unchecked")
                List<Map<String, Object>> bridgeChanges = (List<Map<String, Object>>) bridge.get("changes");
                eq("RETRACT", bridgeChanges.get(0).get("kind"), "first change RETRACT");
                eq("UPSERT", bridgeChanges.get(1).get("kind"), "second change UPSERT");

                // duplicate clientId over HTTP
                HttpResponse<String> dup = post(http, port, "/events",
                        "{\"key\":\"userA\",\"ts\":10,\"clientId\":\"e3\"}");
                eq("DUPLICATE", Json.parseObject(dup.body()).get("status"), "duplicate over HTTP");

                // full changelog fold: sequential must never exceed 3,
                // revision-atomic must equal exactly 3
                HttpResponse<String> out = get(http, port, "/output");
                @SuppressWarnings("unchecked")
                List<Map<String, Object>> all = (List<Map<String, Object>>)
                        Json.parseObject(out.body()).get("changes");
                Map<String, Integer> view = new LinkedHashMap<>();
                int peak = -1;
                for (Map<String, Object> c : all) {
                    String id = (String) c.get("sessionId");
                    if ("RETRACT".equals(c.get("kind"))) {
                        view.remove(id);
                    } else {
                        view.put(id, ((Number) c.get("count")).intValue());
                    }
                    int total = view.values().stream().mapToInt(Integer::intValue).sum();
                    peak = Math.max(peak, total);
                    if (total > 3) {
                        throw new AssertionError("double count over HTTP: total " + total);
                    }
                }
                eq(3, peak, "HTTP changelog peak count = accepted events");
                eq(1, view.size(), "one final session");
                eq(3, view.get("userA#1"), "final session count 3");

                // manual watermark then rejection
                post(http, port, "/watermark", "{\"watermark\":20}");
                HttpResponse<String> late = post(http, port, "/events",
                        "{\"key\":\"userA\",\"ts\":19,\"clientId\":\"late\"}");
                eq("REJECTED", Json.parseObject(late.body()).get("status"), "late event rejected over HTTP");

                // stats
                HttpResponse<String> stats = get(http, port, "/stats");
                Map<String, Object> sm = Json.parseObject(stats.body());
                eq(3L, ((Number) sm.get("accepted")).longValue(), "stats accepted=3");
                eq(1L, ((Number) sm.get("rejected")).longValue(), "stats rejected=1");
                eq(1L, ((Number) sm.get("duplicates")).longValue(), "stats duplicates=1");

                // bad request handling
                HttpResponse<String> bad = post(http, port, "/events", "{not json");
                eq(400, bad.statusCode(), "malformed JSON -> 400");

                // reset
                HttpResponse<String> reset = post(http, port, "/reset", "{}");
                eq(200, reset.statusCode(), "reset 200");
                HttpResponse<String> after = get(http, port, "/output");
                @SuppressWarnings("unchecked")
                List<Object> afterList = (List<Object>)
                        Json.parseObject(after.body()).get("changes");
                eq(0, afterList.size(), "changelog empty after reset");
            } finally {
                api.stop();
                log.close();
            }
        });
    }

    void testUnknownClient() {
        check("unrelated keys aggregate independently", () -> {
            SessionEngine e = new SessionEngine(10, 10, null);
            e.submit(new Event("a", 0, null, null));
            e.submit(new Event("b", 0, null, null));
            eq("a#1", soleSession(e, "a").get("sessionId"), "a id");
            eq("b#1", soleSession(e, "b").get("sessionId"), "b id");
            eq(2, e.sessions().size(), "two keys");
        });
    }

    // ---------------- helpers ----------------

    private static Map<String, Integer> foldAtomic(List<Change> changes) {
        Map<String, Integer> view = new LinkedHashMap<>();
        int i = 0;
        while (i < changes.size()) {
            long rev = changes.get(i).revision();
            int j = i;
            while (j < changes.size() && changes.get(j).revision() == rev) {
                j++;
            }
            Map<String, Integer> staged = new LinkedHashMap<>(view);
            for (int k = i; k < j; k++) {
                Change c = changes.get(k);
                if (c.kind() == Change.Kind.RETRACT) {
                    staged.remove(c.sessionId());
                } else {
                    staged.put(c.sessionId(), c.count());
                }
            }
            view = staged;
            i = j;
        }
        return view;
    }

    private static List<Map<String, Object>> sessionsOf(SessionEngine e, String key) {
        return e.sessions().getOrDefault(key, List.of());
    }

    private static Map<String, Object> soleSession(SessionEngine e, String key) {
        List<Map<String, Object>> list = sessionsOf(e, key);
        if (list.size() != 1) {
            throw new AssertionError("expected exactly 1 session for " + key + ", got " + list.size());
        }
        return list.get(0);
    }

    private static void assertStatus(SessionEngine.Result r, SessionEngine.Status expected) {
        if (r.status() != expected) {
            throw new AssertionError("expected status " + expected + " but got " + r.status()
                    + (r.reason() != null ? " (" + r.reason() + ")" : ""));
        }
    }

    private static void eq(Object expected, Object actual, String what) {
        if (!expected.equals(actual)) {
            throw new AssertionError(what + ": expected <" + expected + "> but was <" + actual + ">");
        }
    }

    private void check(String name, ThrowingRunnable body) {
        try {
            body.run();
            passed++;
            System.out.println("PASS " + name);
        } catch (Throwable t) {
            failed++;
            failures.add(name + " -> " + t);
            System.out.println("FAIL " + name + " -> " + t);
        }
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }

    // ---------------- tiny HTTP client for the end-to-end test ----------------

    private static HttpResponse<String> get(HttpClient http, int port, String path) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .timeout(Duration.ofSeconds(5))
                .GET()
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }

    private static HttpResponse<String> post(HttpClient http, int port, String path, String body)
            throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path))
                .timeout(Duration.ofSeconds(5))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                .build();
        return http.send(req, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
    }
}
