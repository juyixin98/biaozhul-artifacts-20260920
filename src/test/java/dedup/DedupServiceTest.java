package dedup;

import dedup.DedupService.PartitionSnapshot;
import dedup.DedupService.Status;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.ArrayList;
import java.util.List;

/**
 * Self-contained test runner (no JUnit — zero external dependencies).
 * Run: java -cp out/test:out/main dedup.DedupServiceTest
 * Exits non-zero if any test fails.
 */
public class DedupServiceTest {

    private static int passed = 0;
    private static int failed = 0;
    private static final List<String> failures = new ArrayList<>();

    public static void main(String[] args) {
        run("out-of-order duplicates", DedupServiceTest::testOutOfOrderDuplicate);
        run("watermark boundary", DedupServiceTest::testWatermarkBoundary);
        run("expired id treated as new", DedupServiceTest::testExpiredIdTreatedAsNew);
        run("memory released with watermark", DedupServiceTest::testMemoryReleasedAsWatermarkAdvances);
        run("stale routing version rejected", DedupServiceTest::testStaleRoutingVersionRejected);
        run("migration handoff", DedupServiceTest::testMigrationHandoff);
        run("stale snapshot rejected", DedupServiceTest::testMigrationImportRejectsStaleSnapshot);
        run("HTTP end-to-end", DedupServiceTest::testHttpEndToEnd);

        System.out.println();
        System.out.printf("RESULT: %d passed, %d failed%n", passed, failed);
        for (String f : failures) System.out.println("  FAILED: " + f);
        if (failed > 0) System.exit(1);
    }

    // ---------- 1. out-of-order duplicates ----------

    /** Duplicates arriving out of order (late) within the retention window are still duplicates. */
    static void testOutOfOrderDuplicate() {
        DedupService svc = new DedupService(4, 1_000, 10_000);
        long v = svc.routingVersion();

        // Establish a high watermark with a "newer" event first.
        expect(svc.submit("e-new", 50_000, v).status() == Status.NEW, "e-new first seen");
        // The same id arriving again is a duplicate.
        expect(svc.submit("e-new", 50_000, v).status() == Status.DUPLICATE, "e-new replayed -> duplicate");
        // A late event whose id was not seen yet.
        expect(svc.submit("e-old", 40_000, v).status() == Status.NEW, "e-old first seen (late)");
        expect(svc.submit("e-old", 40_000, v).status() == Status.DUPLICATE, "e-old replayed late -> duplicate");
        // Same id with an even older timestamp (out-of-order redelivery) still duplicates.
        expect(svc.submit("e-old", 39_000, v).status() == Status.DUPLICATE, "e-old older redelivery -> duplicate");
    }

    // ---------- 2. watermark boundary ----------

    /** Entry expires exactly when watermark reaches eventTime + retentionMs. */
    static void testWatermarkBoundary() {
        long retention = 500;
        DedupService svc = new DedupService(4, 0, retention);
        long v = svc.routingVersion();

        expect(svc.submit("b1", 1_000, v).status() == Status.NEW, "b1 first seen");
        int p = svc.partitionFor("b1");

        // watermark = 1499 < 1000 + 500 -> still retained.
        svc.advanceWatermark(p, 1_499, v);
        expect(svc.submit("b1", 1_000, v).status() == Status.DUPLICATE,
                "watermark 1499 (< expiry 1500): still duplicate");

        // watermark = 1500 == eventTime + retention -> expired, same id is new again.
        svc.advanceWatermark(p, 1_500, v);
        expect(svc.submit("b1", 1_000, v).status() == Status.NEW,
                "watermark 1500 (== expiry): treated as new");
    }

    // ---------- 3. expired id is a new event ----------

    static void testExpiredIdTreatedAsNew() {
        DedupService svc = new DedupService(4, 100, 1_000);
        long v = svc.routingVersion();

        expect(svc.submit("x", 10_000, v).status() == Status.NEW, "x first seen");
        // Push x's partition watermark past x's expiry (10_000 + 1_000 = 11_000).
        svc.advanceWatermark(svc.partitionFor("x"), 11_000, v);
        // Same id, same old timestamp: retention expired -> new.
        expect(svc.submit("x", 10_000, v).status() == Status.NEW, "x after expiry -> new");
    }

    // ---------- 4. memory released as watermark advances ----------

    static void testMemoryReleasedAsWatermarkAdvances() {
        DedupService svc = new DedupService(4, 0, 1_000);
        long v = svc.routingVersion();

        for (int i = 0; i < 1_000; i++) {
            svc.submit("m" + i, 5_000, v);
        }
        int before = svc.totalEntryCount();
        expect(before == 1_000, "1000 entries held, got " + before);

        // Advance every partition's watermark past 5_000 + 1_000 = 6_000.
        int evicted = 0;
        for (int i = 0; i < 4; i++) evicted += svc.advanceWatermark(i, 6_000, v);
        expect(evicted == 1_000, "all 1000 entries evicted, got " + evicted);
        expect(svc.totalEntryCount() == 0, "memory released, size=" + svc.totalEntryCount());

        // Partial advance: entries below the expiry line are NOT freed.
        for (int i = 0; i < 100; i++) svc.submit("n" + i, 10_000, v);
        int freed = 0;
        for (int i = 0; i < 4; i++) freed += svc.advanceWatermark(i, 10_500, v); // < 10_000+1_000
        expect(freed == 0, "nothing freed below expiry, freed=" + freed);
        expect(svc.totalEntryCount() == 100, "100 entries still held");
    }

    // ---------- 5. wrong routing version rejected ----------

    static void testStaleRoutingVersionRejected() {
        DedupService svc = new DedupService(4, 0, 1_000);
        long v = svc.routingVersion();
        svc.submit("k", 1_000, v);

        expectThrows(StaleRoutingVersionException.class,
                () -> svc.submit("k", 1_000, v + 1), "submit with future version rejected");
        svc.bumpRoutingVersion(); // ownership moved -> old clients are stale now
        expectThrows(StaleRoutingVersionException.class,
                () -> svc.submit("k", 1_000, v), "submit with old version rejected after bump");
        expect(svc.submit("k", 1_000, v + 1).status() == Status.DUPLICATE,
                "current version accepted, state preserved");
    }

    // ---------- 6. migration handoff: dedup state follows the keys ----------

    static void testMigrationHandoff() {
        DedupService nodeA = new DedupService(4, 0, 60_000);
        DedupService nodeB = new DedupService(4, 0, 60_000);
        long v = nodeA.routingVersion();

        // Traffic on node A.
        expect(nodeA.submit("order-1", 100_000, v).status() == Status.NEW, "order-1 on A");
        expect(nodeA.submit("order-2", 100_001, v).status() == Status.NEW, "order-2 on A");
        int p = nodeA.partitionFor("order-1");

        // Operator migrates partition p: bump version on A, export, hand to B.
        long newV = nodeA.bumpRoutingVersion();
        PartitionSnapshot snap = nodeA.exportPartition(p, newV);
        nodeB.importPartition(snap);

        // A duplicate delivered to the NEW owner during/after handoff is still a duplicate.
        expect(nodeB.submit("order-1", 100_000, newV).status() == Status.DUPLICATE,
                "duplicate delivered to new owner after handoff");
        // A genuinely new id on B is new.
        expect(nodeB.submit("order-9", 100_002, newV).status() == Status.NEW,
                "new id on new owner is new");

        // Both nodes now agree on the routing version; stale senders are rejected on both.
        expect(nodeB.routingVersion() == newV, "B adopted snapshot version");
        expectThrows(StaleRoutingVersionException.class,
                () -> nodeB.submit("order-1", 100_000, v), "old version rejected on B");
        expectThrows(StaleRoutingVersionException.class,
                () -> nodeA.submit("order-1", 100_000, v), "old version rejected on A");

        // Watermark carried over: state expires on B at the same point it would have on A.
        nodeB.advanceWatermark(p, 100_000 + 60_000, newV);
        expect(nodeB.submit("order-1", 100_000, newV).status() == Status.NEW,
                "retention still bounded by migrated watermark on B");
    }

    /** A snapshot exported under an old routing version must not be imported. */
    static void testMigrationImportRejectsStaleSnapshot() {
        DedupService nodeA = new DedupService(4, 0, 60_000);
        DedupService nodeB = new DedupService(4, 0, 60_000);
        long v = nodeA.routingVersion();
        nodeA.submit("s1", 1_000, v);
        PartitionSnapshot stale = nodeA.exportPartition(nodeA.partitionFor("s1"), v);

        // B has already moved to a newer version (e.g. an earlier migration round).
        nodeB.bumpRoutingVersion();
        expectThrows(StaleRoutingVersionException.class,
                () -> nodeB.importPartition(stale), "stale snapshot rejected on import");
    }

    // ---------- 7. HTTP end-to-end ----------

    static void testHttpEndToEnd() throws Exception {
        DedupService svc = new DedupService(4, 0, 500);
        DedupServer server = new DedupServer(0, svc); // ephemeral port
        server.start();
        try {
            HttpClient client = HttpClient.newHttpClient();
            String base = "http://127.0.0.1:" + server.port();

            HttpResponse<String> health = get(client, base + "/health");
            expect(health.statusCode() == 200 && health.body().contains("ok"), "GET /health ok");

            HttpResponse<String> r1 = post(client, base + "/events",
                    "{\"eventId\":\"h1\",\"eventTime\":1000,\"routingVersion\":1}");
            expect(r1.statusCode() == 200 && r1.body().contains("\"new\""), "POST /events new");

            HttpResponse<String> r2 = post(client, base + "/events",
                    "{\"eventId\":\"h1\",\"eventTime\":1000,\"routingVersion\":1}");
            expect(r2.statusCode() == 200 && r2.body().contains("\"duplicate\""), "POST /events duplicate");

            HttpResponse<String> r3 = post(client, base + "/events",
                    "{\"eventId\":\"h1\",\"eventTime\":1000,\"routingVersion\":99}");
            expect(r3.statusCode() == 409 && r3.body().contains("stale_routing_version"),
                    "POST /events stale version -> 409, got " + r3.statusCode());

            HttpResponse<String> r4 = post(client, base + "/events", "{\"eventTime\":1000}");
            expect(r4.statusCode() == 400, "POST /events missing field -> 400");

            // export -> import round-trip over HTTP
            int p = svc.partitionFor("h1");
            HttpResponse<String> exp = post(client, base + "/migration/export",
                    "{\"partition\":" + p + ",\"routingVersion\":1}");
            expect(exp.statusCode() == 200 && exp.body().contains("\"h1\":1000"),
                    "export contains entry, body=" + exp.body());

            DedupService svc2 = new DedupService(4, 0, 500);
            DedupServer server2 = new DedupServer(0, svc2);
            server2.start();
            try {
                HttpResponse<String> imp = post(client,
                        "http://127.0.0.1:" + server2.port() + "/migration/import", exp.body());
                expect(imp.statusCode() == 200, "import ok");
                HttpResponse<String> dup = post(client,
                        "http://127.0.0.1:" + server2.port() + "/events",
                        "{\"eventId\":\"h1\",\"eventTime\":1000,\"routingVersion\":1}");
                expect(dup.body().contains("\"duplicate\""), "duplicate survives HTTP migration");
            } finally {
                server2.stop();
            }

            HttpResponse<String> state = get(client, base + "/state");
            expect(state.statusCode() == 200 && state.body().contains("\"routingVersion\":1"),
                    "GET /state ok");
        } finally {
            server.stop();
        }
    }

    // ---------- helpers ----------

    private static HttpResponse<String> get(HttpClient c, String url) throws IOException, InterruptedException {
        return c.send(HttpRequest.newBuilder(URI.create(url)).GET().build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private static HttpResponse<String> post(HttpClient c, String url, String json)
            throws IOException, InterruptedException {
        return c.send(HttpRequest.newBuilder(URI.create(url))
                        .POST(HttpRequest.BodyPublishers.ofString(json))
                        .header("Content-Type", "application/json").build(),
                HttpResponse.BodyHandlers.ofString());
    }

    private static void expect(boolean cond, String name) {
        if (!cond) throw new AssertionError(name);
    }

    private static <T extends Throwable> void expectThrows(Class<T> type, ThrowingRunnable r, String name) {
        try {
            r.run();
        } catch (Throwable t) {
            if (type.isInstance(t)) return;
            throw new AssertionError(name + ": expected " + type.getSimpleName() + " but got " + t);
        }
        throw new AssertionError(name + ": expected " + type.getSimpleName() + " but nothing was thrown");
    }

    @FunctionalInterface
    private interface ThrowingRunnable { void run() throws Throwable; }

    private static void run(String name, ThrowingRunnable t) {
        try {
            t.run();
            passed++;
            System.out.println("PASS  " + name);
        } catch (Throwable e) {
            failed++;
            failures.add(name + " -> " + e);
            System.out.println("FAIL  " + name + " -> " + e);
        }
    }
}
