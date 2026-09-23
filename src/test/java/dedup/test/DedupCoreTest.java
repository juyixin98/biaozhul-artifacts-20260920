package dedup.test;

import dedup.ApiException;
import dedup.DedupService;
import dedup.ErrorCode;
import dedup.Snapshot;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static dedup.test.Assert.assertEquals;
import static dedup.test.Assert.assertFalse;
import static dedup.test.Assert.assertThrows;
import static dedup.test.Assert.assertTrue;

/**
 * Direct unit tests of the domain service, no HTTP involved.
 *
 * Time axis convention: eventTime is epoch millis; retention is 10_000 ms in
 * most cases, so an event at t is expired once watermark >= t + 10_000.
 */
public class DedupCoreTest {

    private final DedupService svc = new DedupService();

    private static final long RETENTION = 10_000L;

    private Map<String, Object> create(String key, long epoch) {
        return svc.createPartition(key, epoch, RETENTION);
    }

    private boolean isDup(String key, long epoch, String id, long t) {
        return (Boolean) svc.checkEvent(key, epoch, id, t).get("duplicate");
    }

    private boolean isLate(String key, long epoch, String id, long t) {
        return (Boolean) svc.checkEvent(key, epoch, id, t).get("late");
    }

    private long count(String key) {
        return ((Number) svc.status(key).get("retainedEventCount")).longValue();
    }

    // ---------------------------------------------------------------
    // Out-of-order duplicates
    // ---------------------------------------------------------------

    @Test
    public void duplicatesAreDetectedRegardlessOfArrivalOrder() {
        create("p", 0);

        assertFalse(isDup("p", 0L, "e1", 1_000), "first sighting is NEW");
        // Replay/duplicate arriving with an EARLIER event time (out of order) is still a dup.
        assertTrue(isDup("p", 0L, "e1", 500), "earlier-timestamp redelivery is DUPLICATE");
        // Duplicate carrying a LATER event time lifts the anchor.
        assertTrue(isDup("p", 0L, "e1", 2_000), "later-timestamp redelivery is DUPLICATE");
        assertEquals(1L, count("p"), "only one retained entry for one id");

        // A second id with interleaved timestamps dedups independently.
        assertFalse(isDup("p", 0L, "e2", 1_500), "different id is NEW");
        assertTrue(isDup("p", 0L, "e2", 1_500), "same id again is DUPLICATE");
        assertEquals(2L, count("p"), "two ids retained");
    }

    // ---------------------------------------------------------------
    // Watermark boundary (inclusive)
    // ---------------------------------------------------------------

    @Test
    public void boundaryIsInclusiveAtAnchorPlusRetention() {
        create("p", 0);
        long anchor = 100_000L;
        assertFalse(isDup("p", 0L, "e1", anchor), "NEW at anchor");

        // wm = anchor + retention - 1  -> still inside the window: duplicate.
        svc.advanceWatermark("p", 0L, anchor + RETENTION - 1);
        assertEquals(1L, count("p"), "not purged just before boundary");
        assertTrue(isDup("p", 0L, "e1", anchor), "DUPLICATE one ms before expiry");

        // wm = anchor + retention      -> expired (boundary inclusive): id is new again.
        svc.advanceWatermark("p", 0L, anchor + RETENTION);
        assertEquals(0L, count("p"), "purged exactly at boundary");
        Map<String, Object> again = svc.checkEvent("p", 0L, "e1", anchor + RETENTION);
        assertFalse((Boolean) again.get("duplicate"), "same id after expiry is NEW again");
        assertEquals("NEW", again.get("status"), "status string NEW");
    }

    @Test
    public void laterDuplicateLiftsAnchorAndExtendsRetention() {
        create("p", 0);
        assertFalse(isDup("p", 0L, "e1", 0), "NEW at t=0");
        assertTrue(isDup("p", 0L, "e1", 5_000), "dup at t=5000 lifts anchor");

        // Watermark that WOULD have expired an anchor at t=0 must not expire the lifted anchor.
        svc.advanceWatermark("p", 0L, 9_999);
        assertEquals(1L, count("p"), "lifted anchor survives wm=9999");
        svc.advanceWatermark("p", 0L, 14_999);
        assertEquals(1L, count("p"), "lifted anchor survives wm=14999");
        svc.advanceWatermark("p", 0L, 15_000);
        assertEquals(0L, count("p"), "lifted anchor expires at 5000+retention");
    }

    @Test
    public void watermarkMustBeMonotonic() {
        create("p", 0);
        svc.advanceWatermark("p", 0L, 100);
        ApiException ex = assertThrows(ApiException.class,
                () -> svc.advanceWatermark("p", 0L, 99));
        assertEquals(ErrorCode.WATERMARK_MONOTONIC, ex.code(), "monotonic error code");
        // Equal watermark is allowed (idempotent re-drive).
        svc.advanceWatermark("p", 0L, 100);
    }

    @Test
    public void lateEventPastRetentionIsDeliveredButNeverStored() {
        create("p", 0);
        svc.advanceWatermark("p", 0L, 100_000); // far future wm
        Map<String, Object> r = svc.checkEvent("p", 0L, "late-1", 1_000);
        assertFalse((Boolean) r.get("duplicate"), "an unseen late event is NEW (delivered)");
        assertTrue((Boolean) r.get("late"), "flagged late");
        assertEquals(0L, count("p"), "late entry is not retained");

        // Repeated: it must not accidentally dedup against a non-stored entry.
        Map<String, Object> r2 = svc.checkEvent("p", 0L, "late-1", 1_000);
        assertFalse((Boolean) r2.get("duplicate"), "late id cannot dedup nothing");
    }

    // ---------------------------------------------------------------
    // Memory release as watermark advances
    // ---------------------------------------------------------------

    @Test
    public void retainedMemoryShrinksWithWatermark() {
        create("p", 0);
        int n = 1_000;
        for (int i = 0; i < n; i++) {
            isDup("p", 0L, "id-" + i, i * 10L); // t = 0 .. 9990
        }
        assertEquals(n, count("p"), "all retained before wm");
        long charsBefore = ((Number) svc.status("p").get("retainedIdChars")).longValue();
        assertTrue(charsBefore > 0, "tracked id chars before purge");

        svc.advanceWatermark("p", 0L, 5_000 + RETENTION); // expire anchors <= 5000
        long afterMid = count("p");
        assertTrue(afterMid < n && afterMid > 0, "partially purged mid-stream: " + afterMid);

        svc.advanceWatermark("p", 0L, 9_990 + RETENTION); // expire everything
        assertEquals(0L, count("p"), "everything purged at end");
        assertEquals(0L, ((Number) svc.status("p").get("retainedIdChars")).longValue(),
                "id-char footprint back to zero");
    }

    // ---------------------------------------------------------------
    // Epoch fencing / wrong routing version
    // ---------------------------------------------------------------

    @Test
    public void staleEpochIsRejectedOnEveryWrite() {
        create("p", 1);
        // Source hands the key off to a new owner at epoch 2.
        Snapshot snap = exportSnapshot("p", 1L);
        svc.importSnapshot("p-new", null, snap, 2L);
        svc.completeMigration("p", 1L);

        assertEquals(2L, ((Number) svc.status("p-new").get("epoch")).longValue(),
                "new owner at epoch 2");

        // A stale producer still using epoch 1 is fenced by the new owner...
        ApiException e1 = assertThrows(ApiException.class,
                () -> svc.checkEvent("p-new", 1L, "x", 0));
        assertEquals(ErrorCode.INVALID_ROUTING_VERSION, e1.code(), "stale event rejected");

        ApiException e2 = assertThrows(ApiException.class,
                () -> svc.advanceWatermark("p-new", 1L, 10));
        assertEquals(ErrorCode.INVALID_ROUTING_VERSION, e2.code(), "stale watermark rejected");

        // ...and by the drained old owner.
        ApiException e3 = assertThrows(ApiException.class,
                () -> svc.checkEvent("p", 1L, "x", 0));
        assertEquals(ErrorCode.INVALID_ROUTING_VERSION, e3.code(),
                "stale producer to old owner rejected");
    }

    @Test
    public void importRejectsSnapshotWithNonAdvancingEpoch() {
        create("p", 1);
        svc.checkEvent("p", 1L, "a", 100);
        Snapshot snap = exportSnapshot("p", 1L);
        svc.abortMigration("p", 1L);

        // Replaying the snapshot with an equal or lower epoch must fail, on any destination.
        ApiException e = assertThrows(ApiException.class,
                () -> svc.importSnapshot("dst", null, snap, snap.epoch()));
        assertEquals(ErrorCode.INVALID_ROUTING_VERSION, e.code(), "equal epoch refused");

        ApiException e2 = assertThrows(ApiException.class,
                () -> svc.importSnapshot("dst2", null, snap, 0L));
        assertEquals(ErrorCode.INVALID_ROUTING_VERSION, e2.code(), "lower epoch refused");

        // And the source, unfrozen, kept its data.
        assertTrue((Boolean) svc.checkEvent("p", 1L, "a", 100).get("duplicate"),
                "source data intact after abort");
    }

    // ---------------------------------------------------------------
    // Migration handoff: dedup state travels with the key
    // ---------------------------------------------------------------

    @Test
    public void migratedStateDedupsRedeliveredEvents() {
        // Source owns key "orders-7" at epoch 3 and has seen some ids.
        create("src", 3);
        svc.checkEvent("src", 3L, "o1", 10_000);
        svc.checkEvent("src", 3L, "o2", 20_000);
        svc.advanceWatermark("src", 3L, 15_000);

        // 1) freeze + snapshot
        Snapshot snap = exportSnapshot("src", 3L);
        assertEquals(3L, snap.epoch(), "snapshot carries source epoch");
        assertEquals(2, snap.entries().size(), "both live entries exported");

        // 2) source rejects writes while frozen
        ApiException frozen = assertThrows(ApiException.class,
                () -> svc.checkEvent("src", 3L, "o3", 21_000));
        assertEquals(ErrorCode.PARTITION_MIGRATING, frozen.code(), "frozen writes rejected");

        // 3) install on destination under a strictly greater epoch
        Map<String, Object> dst = svc.importSnapshot("dst", null, snap, 4L);
        assertEquals(4L, ((Number) dst.get("epoch")).longValue(), "dst epoch 4");
        assertEquals(2L, ((Number) dst.get("retainedEventCount")).longValue(),
                "entries traveled with the key");
        assertEquals(15_000L, ((Number) dst.get("watermark")).longValue(),
                "watermark traveled with the key");

        // 4) producers redelivering the in-flight duplicates to the new owner are caught.
        assertTrue((Boolean) svc.checkEvent("dst", 4L, "o1", 10_000).get("duplicate"),
                "redelivered o1 caught at destination");
        assertTrue((Boolean) svc.checkEvent("dst", 4L, "o2", 20_000).get("duplicate"),
                "redelivered o2 caught at destination");
        assertFalse((Boolean) svc.checkEvent("dst", 4L, "o3", 21_000).get("duplicate"),
                "genuinely new event accepted at destination");

        // 5) old owner must still refuse a stale producer routing to it after cutover.
        svc.completeMigration("src", 3L);
        ApiException stale = assertThrows(ApiException.class,
                () -> svc.checkEvent("src", 3L, "o1", 10_000));
        assertEquals(ErrorCode.INVALID_ROUTING_VERSION, stale.code(),
                "stale routing version to old owner rejected");
        // Even a producer that learned the tombstone epoch cannot write to the drained source.
        ApiException drained = assertThrows(ApiException.class,
                () -> svc.checkEvent("src", 4L, "o1", 10_000));
        assertEquals(ErrorCode.INVALID_ROUTING_VERSION, drained.code(),
                "drained source rejects all writes");
        assertEquals(0L, ((Number) svc.status("src").get("retainedEventCount")).longValue(),
                "source state empty after cutover");
    }

    @Test
    public void snapshotEntriesAlreadyExpiredAreNotReinstalled() {
        create("src", 1);
        svc.checkEvent("src", 1L, "fresh", 100_000);
        svc.checkEvent("src", 1L, "old", 1_000);
        svc.advanceWatermark("src", 1L, 50_000); // "old" expired, "fresh" alive

        Snapshot snap = exportSnapshot("src", 1L);
        svc.completeMigration("src", 1L);
        Map<String, Object> dst = svc.importSnapshot("dst", null, snap, 2L);
        assertEquals(1L, ((Number) dst.get("retainedEventCount")).longValue(),
                "only the live entry crosses the handoff");
    }

    @Test
    public void abortMigrationUnfreezesWithoutDataLoss() {
        create("p", 0);
        svc.checkEvent("p", 0L, "x", 100);
        exportSnapshot("p", 0L);
        svc.abortMigration("p", 0L);
        assertTrue((Boolean) svc.checkEvent("p", 0L, "x", 100).get("duplicate"),
                "state intact after abort");
    }

    @Test
    public void unknownPartitionAndBasicValidation() {
        ApiException e = assertThrows(ApiException.class,
                () -> svc.checkEvent("nope", null, "x", 0));
        assertEquals(ErrorCode.NOT_FOUND, e.code(), "missing partition 404");

        ApiException bad = assertThrows(ApiException.class,
                () -> svc.createPartition("zero", 0, 0));
        assertEquals(ErrorCode.INVALID_BODY, bad.code(), "retention must be positive");
    }

    @Test
    public void concurrentEventsAndWatermarkStayConsistent() throws Exception {
        create("p", 0);
        int threads = 8;
        int perThread = 2_000;
        Thread[] workers = new Thread[threads];
        for (int w = 0; w < threads; w++) {
            final int base = w * perThread;
            workers[w] = new Thread(() -> {
                for (int i = 0; i < perThread; i++) {
                    // Ids repeat across threads, so the final retained set is bounded.
                    String id = "id-" + (i % 500);
                    svc.checkEvent("p", 0L, id, i * 10L);
                }
            });
        }
        for (Thread t : workers) {
            t.start();
        }
        // Concurrent watermark drives (strictly increasing to stay monotonic).
        Thread wmThread = new Thread(() -> {
            for (long wm = 0; wm <= 25_000; wm += 100) {
                try {
                    svc.advanceWatermark("p", 0L, wm);
                } catch (ApiException ok) {
                    // monotonic rejections under racing advance are acceptable
                    assertEquals(ErrorCode.WATERMARK_MONOTONIC, ok.code(),
                            "only monotonic conflicts expected");
                }
            }
        });
        wmThread.start();
        for (Thread t : workers) {
            t.join();
        }
        wmThread.join();

        long finalCount = count("p");
        // At most 500 distinct ids exist; everything expired is released.
        assertTrue(finalCount <= 500, "retained set bounded: " + finalCount);
        // After the final watermark clears the window, the set must be empty.
        svc.advanceWatermark("p", 0L, (perThread - 1) * 10L + RETENTION);
        assertEquals(0L, count("p"), "all released after final watermark");
    }

    // --- helpers (Partition is package-private; snapshots come via the service) ---

    private Snapshot exportSnapshot(String key, long epoch) {
        Map<String, Object> resp = svc.exportSnapshot(key, epoch);
        Object snapObj = resp.get("snapshot");
        return reconstruct((Map<?, ?>) snapObj);
    }

    @SuppressWarnings("unchecked")
    private static Snapshot reconstruct(Map<?, ?> raw) {
        List<Snapshot.EntryView> entries = new ArrayList<>();
        for (Object o : (List<?>) raw.get("entries")) {
            Map<String, Object> em = (Map<String, Object>) o;
            entries.add(new Snapshot.EntryView((String) em.get("eventId"),
                    ((Number) em.get("anchorEventTime")).longValue()));
        }
        Object wm = raw.get("watermark");
        long watermark = wm == null ? Long.MIN_VALUE : ((Number) wm).longValue();
        return new Snapshot((String) raw.get("partitionKey"),
                ((Number) raw.get("epoch")).longValue(),
                watermark,
                ((Number) raw.get("retentionMillis")).longValue(),
                entries);
    }
}
