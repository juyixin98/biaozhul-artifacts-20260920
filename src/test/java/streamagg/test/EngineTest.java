package streamagg.test;

import streamagg.engine.StreamCorrectionEngine;
import streamagg.model.Aggregate;
import streamagg.model.IngestOp;
import streamagg.model.IngestResult;
import streamagg.model.IngestStatus;
import streamagg.model.OpType;
import streamagg.model.ResolvedOp;
import streamagg.time.ManualClock;

import java.math.BigDecimal;
import java.util.List;
import java.util.Map;

/** Core engine tests: retractions, corrections, out-of-order buffering, idempotency, replay. */
public final class EngineTest {

    private static StreamCorrectionEngine newEngine() {
        return StreamCorrectionEngine.builder().clock(new ManualClock()).build();
    }

    private static IngestOp add(String id, String key, String value) {
        return new IngestOp(null, id, OpType.ADD, key, new BigDecimal(value), null, null);
    }

    private static IngestOp add(String id, String key, String value, Long version, String opId) {
        return new IngestOp(opId, id, OpType.ADD, key, new BigDecimal(value), version, null);
    }

    private static IngestOp retract(String id) {
        return new IngestOp(null, id, OpType.RETRACT, null, null, null, null);
    }

    private static IngestOp retract(String id, Long version, String opId) {
        return new IngestOp(opId, id, OpType.RETRACT, null, null, version, null);
    }

    private static IngestOp correct(String id, String key, String value) {
        return new IngestOp(null, id, OpType.CORRECT, key,
                value == null ? null : new BigDecimal(value), null, null);
    }

    private static IngestOp correct(String id, String key, String value, Long version, String opId) {
        return new IngestOp(opId, id, OpType.CORRECT, key,
                value == null ? null : new BigDecimal(value), version, null);
    }

    public static void register(TestRunner r) {

        // ------------------------------------------------------------
        // Basic lifecycle
        // ------------------------------------------------------------

        r.test("engine: ADD then aggregates appear", () -> {
            var e = newEngine();
            Assert.assertEquals(IngestStatus.APPLIED, e.ingest(add("e1", "k1", "10")).status(), "add e1");
            Assert.assertEquals(IngestStatus.APPLIED, e.ingest(add("e2", "k1", "5")).status(), "add e2");
            Aggregate k1 = e.aggregates().get("k1");
            Assert.assertEq(new BigDecimal("15"), k1.sum(), "k1 sum");
            Assert.assertEquals(2L, k1.count(), "k1 count");
        });

        r.test("engine: RETRACT removes event and updates sum/count", () -> {
            var e = newEngine();
            e.ingest(add("e1", "k1", "10"));
            e.ingest(add("e2", "k1", "5"));
            Assert.assertEquals(IngestStatus.APPLIED, e.ingest(retract("e1")).status(), "retract e1");
            Aggregate k1 = e.aggregates().get("k1");
            Assert.assertEq(new BigDecimal("5"), k1.sum(), "sum after retract");
            Assert.assertEquals(1L, k1.count(), "count after retract");
            Assert.assertNull(e.events().get("e1"), "e1 no longer live");
        });

        r.test("engine: CORRECT changes value on same key", () -> {
            var e = newEngine();
            e.ingest(add("e1", "k1", "10"));
            Assert.assertEquals(IngestStatus.APPLIED,
                    e.ingest(correct("e1", "k1", "25")).status(), "correct value");
            Assert.assertEq(new BigDecimal("25"), e.aggregates().get("k1").sum(), "corrected sum");
            Assert.assertEquals(1L, e.aggregates().get("k1").count(), "count stable");
        });

        r.test("engine: CORRECT moves event to a new key", () -> {
            var e = newEngine();
            e.ingest(add("e1", "k1", "10"));
            e.ingest(add("e2", "k2", "7"));
            e.ingest(correct("e1", "k2", null)); // key change only
            Assert.assertNull(e.aggregates().get("k1"), "old key emptied and removed");
            Assert.assertEq(new BigDecimal("17"), e.aggregates().get("k2").sum(), "new key sum");
            Assert.assertEquals(2L, e.aggregates().get("k2").count(), "moved count");
        });

        // ------------------------------------------------------------
        // Acceptance: retract-before-add (unversioned)
        // ------------------------------------------------------------

        r.test("acceptance: RETRACT before ADD buffers and releases (unversioned)", () -> {
            var e = newEngine();
            // Undo arrives before the original event
            IngestResult first = e.ingest(retract("late"));
            Assert.assertEquals(IngestStatus.BUFFERED, first.status(), "retract buffered");
            Assert.assertEquals(1, e.totalPending(), "one pending");
            Assert.assertNull(e.aggregates().get("k1"), "nothing counted yet");

            // Original event finally arrives: ADD then the waiting RETRACT drains
            IngestResult addResult = e.ingest(add("late", "k1", "10"));
            Assert.assertEquals(IngestStatus.APPLIED, addResult.status(), "add applies");
            Assert.assertEquals(2, addResult.drained().size(), "add + buffered retract drained");
            Assert.assertEquals(OpType.RETRACT, addResult.drained().get(1).op(), "retract drained second");
            Assert.assertNull(e.aggregates().get("k1"), "net aggregate empty after retract");
            Assert.assertEquals(0, e.totalPending(), "pending cleared");
            Assert.assertNull(e.events().get("late"), "event never stays live");
        });

        r.test("acceptance: RETRACT before ADD then re-ADD works", () -> {
            var e = newEngine();
            e.ingest(retract("x"));
            e.ingest(add("x", "k1", "10")); // drains add+retract, chain retired
            IngestResult readd = e.ingest(add("x", "k1", "20"));
            Assert.assertEquals(IngestStatus.APPLIED, readd.status(), "re-add opens new chain");
            Assert.assertEq(new BigDecimal("20"), e.aggregates().get("k1").sum(), "only new event counted");
        });

        r.test("acceptance: CORRECT before ADD buffers and applies on release", () -> {
            var e = newEngine();
            IngestResult c = e.ingest(correct("late", "k1", "99"));
            Assert.assertEquals(IngestStatus.BUFFERED, c.status(), "correct buffered");
            IngestResult a = e.ingest(add("late", "k1", "10"));
            Assert.assertEquals(IngestStatus.APPLIED, a.status(), "add+correct drain");
            Assert.assertEquals(OpType.CORRECT, a.drained().get(1).op(), "correct drained");
            Assert.assertEq(new BigDecimal("99"), e.aggregates().get("k1").sum(), "corrected value wins");
            Assert.assertEquals(1L, e.aggregates().get("k1").count(), "single count");
        });

        // ------------------------------------------------------------
        // Acceptance: versioned out-of-order corrections
        // ------------------------------------------------------------

        r.test("acceptance: out-of-order versions buffer until gaps fill", () -> {
            var e = newEngine();
            // v3 arrives first, then v1, then v2
            Assert.assertEquals(IngestStatus.BUFFERED,
                    e.ingest(correct("e1", "k1", "30", 3L, "o3")).status(), "v3 parks");
            Assert.assertEquals(IngestStatus.APPLIED,
                    e.ingest(add("e1", "k1", "10", 1L, "o1")).status(), "v1 applies alone");
            Assert.assertEq(new BigDecimal("10"), e.aggregates().get("k1").sum(), "still v1 value");

            IngestResult v2 = e.ingest(correct("e1", "k1", "20", 2L, "o2"));
            Assert.assertEquals(IngestStatus.APPLIED, v2.status(), "v2 triggers drain");
            Assert.assertEquals(2, v2.drained().size(), "v2 and parked v3 both resolved");
            Assert.assertEq(new BigDecimal("30"), e.aggregates().get("k1").sum(), "final v3 value");
            Assert.assertEquals(1L, e.aggregates().get("k1").count(), "count never multiplied");
        });

        r.test("acceptance: multiple corrections in version chain", () -> {
            var e = newEngine();
            e.ingest(add("e1", "k1", "100", 1L, null));
            e.ingest(correct("e1", "k1", "110", 2L, null));
            e.ingest(correct("e1", "k1", "120", 3L, null));
            e.ingest(correct("e1", "k2", null, 4L, null));
            Assert.assertNull(e.aggregates().get("k1"), "k1 emptied after key move");
            Assert.assertEq(new BigDecimal("120"), e.aggregates().get("k2").sum(), "moved final value");
            Assert.assertTrue(e.verifyAgainstLedger().isEmpty(), "matches ledger replay");
        });

        r.test("acceptance: versioned retract-before-add (v2 retract before v1 add)", () -> {
            var e = newEngine();
            Assert.assertEquals(IngestStatus.BUFFERED,
                    e.ingest(retract("e1", 2L, "r2")).status(), "v2 retract parks");
            IngestResult r1 = e.ingest(add("e1", "k1", "8", 1L, "a1"));
            Assert.assertEquals(IngestStatus.APPLIED, r1.status(), "v1 drains chain");
            Assert.assertEquals(2, r1.drained().size(), "v1 add + v2 retract");
            Assert.assertNull(e.aggregates().get("k1"), "no net contribution");
        });

        // ------------------------------------------------------------
        // Idempotency
        // ------------------------------------------------------------

        r.test("idempotency: same opId replay is DUPLICATE and state unchanged", () -> {
            var e = newEngine();
            e.ingest(add("e1", "k1", "10", 1L, "token-1"));
            IngestResult again = e.ingest(add("e1", "k1", "10", 1L, "token-1"));
            Assert.assertEquals(IngestStatus.DUPLICATE, again.status(), "same opId duplicate");
            Assert.assertEquals(1L, e.aggregates().get("k1").count(), "count not doubled");
            Assert.assertEquals(1, e.ledger().size(), "ledger unchanged");
        });

        r.test("idempotency: replay of full mixed stream leaves same aggregates", () -> {
            var e = newEngine();
            var ops = List.of(
                    add("a", "k", "1", 1L, "1"),
                    add("b", "k", "2", 1L, "2"),
                    correct("a", "k", "10", 2L, "3"),
                    retract("b", 2L, "4"),
                    add("c", "k", "5", 1L, "5"));
            for (IngestOp op : ops) {
                e.ingest(op);
            }
            BigDecimal sumBefore = e.aggregates().get("k").sum();
            long countBefore = e.aggregates().get("k").count();
            int ledgerBefore = e.ledger().size();
            // Replay out of order too
            for (int i = ops.size() - 1; i >= 0; i--) {
                IngestResult rr = e.ingest(ops.get(i));
                Assert.assertEquals(IngestStatus.DUPLICATE, rr.status(),
                        "replayed op " + ops.get(i).opId() + " duplicate");
            }
            Assert.assertEq(sumBefore, e.aggregates().get("k").sum(), "sum stable under replay");
            Assert.assertEquals(countBefore, e.aggregates().get("k").count(), "count stable under replay");
            Assert.assertEquals(ledgerBefore, e.ledger().size(), "ledger stable under replay");
        });

        r.test("idempotency: identical unversioned ADD replayed is deduplicated by fingerprint", () -> {
            var e = newEngine();
            e.ingest(add("e1", "k1", "10"));
            Assert.assertEquals(IngestStatus.DUPLICATE,
                    e.ingest(add("e1", "k1", "10")).status(), "identical add duplicate");
            Assert.assertEquals(1L, e.aggregates().get("k1").count(), "count stable");
        });

        r.test("idempotency: buffered out-of-order op replayed before release stays DUPLICATE", () -> {
            var e = newEngine();
            e.ingest(correct("e1", "k1", "30", 3L, "o3"));
            IngestResult again = e.ingest(correct("e1", "k1", "30", 3L, "o3"));
            Assert.assertEquals(IngestStatus.DUPLICATE, again.status(), "replayed buffered op duplicate");
        });

        r.test("conflict: CORRECT after terminal RETRACT is rejected, not parked forever", () -> {
            var e = newEngine();
            e.ingest(add("e1", "k1", "10", 1L, "a1"));
            e.ingest(retract("e1", 2L, "r2"));
            IngestResult postRetire = e.ingest(correct("e1", "k1", "20", 3L, "c3"));
            Assert.assertEquals(IngestStatus.CONFLICT, postRetire.status(), "post-retire correct conflicts");
            Assert.assertEquals(0, e.totalPending(), "nothing left pending");

            // Same protection when v3 was parked *before* the v2 retract arrived
            var e2 = newEngine();
            e2.ingest(correct("x", "k", "3", 3L, "p3")); // parks
            e2.ingest(add("x", "k", "1", 1L, "a"));       // v1
            e2.ingest(retract("x", 2L, "r"));             // v2 retires; parked v3 must be rejected
            Assert.assertEquals(0, e2.totalPending(), "parked v3 flushed on retirement");
            Assert.assertTrue(e2.warnings().stream().anyMatch(w -> w.contains("v3")), "v3 warned: " + e2.warnings());
        });

        r.test("conflict: same version different content rejected", () -> {
            var e = newEngine();
            e.ingest(add("e1", "k1", "10", 1L, "a1"));
            e.ingest(correct("e1", "k1", "20", 2L, "c2"));
            IngestResult bad = e.ingest(correct("e1", "k1", "99", 2L, "c2b"));
            Assert.assertEquals(IngestStatus.CONFLICT, bad.status(), "conflicting v2");
            Assert.assertEq(new BigDecimal("20"), e.aggregates().get("k1").sum(), "conflict did not change sum");
        });

        // ------------------------------------------------------------
        // Negative drift guard
        // ------------------------------------------------------------

        r.test("invariant: count never goes negative on cross-key correction churn", () -> {
            var e = newEngine();
            e.ingest(add("a", "k1", "1"));
            e.ingest(add("b", "k1", "1"));
            e.ingest(retract("a"));
            e.ingest(correct("b", "k2", null));
            e.ingest(retract("b"));
            Assert.assertNull(e.aggregates().get("k1"), "k1 removed cleanly");
            Assert.assertNull(e.aggregates().get("k2"), "k2 removed cleanly");
            Assert.assertTrue(e.verifyAgainstLedger().isEmpty(), "recomputation matches");
        });

        r.test("invariant: reference recompute throws on injected negative drift", () -> {
            // Construct a corrupted ledger manually: RETRACT without an ADD.
            ResolvedOp bad = new ResolvedOp("x", "ghost", OpType.RETRACT, 1,
                    null, null, null, null, null, null);
            try {
                StreamCorrectionEngine.recomputeFromLedger(List.of(bad));
                throw new TestFailure("expected negative drift to be detected");
            } catch (IllegalStateException ex) {
                Assert.assertContains(ex.getMessage(), "negative count drift", "guard message");
            }
        });

        // ------------------------------------------------------------
        // Validation / late data
        // ------------------------------------------------------------

        r.test("validation: ADD requires key and value", () -> {
            var e = newEngine();
            IngestOp noValue = new IngestOp(null, "e1", OpType.ADD, "k1", null, null, null);
            Assert.assertEquals(IngestStatus.INVALID, e.ingest(noValue).status(), "missing value");
            IngestOp noKey = new IngestOp(null, "e2", OpType.ADD, null, new BigDecimal("1"), null, null);
            Assert.assertEquals(IngestStatus.INVALID, e.ingest(noKey).status(), "missing key");
        });

        r.test("lateness: operations beyond watermark minus lateness are rejected", () -> {
            var e = StreamCorrectionEngine.builder()
                    .clock(new ManualClock())
                    .allowedLateness(java.time.Duration.ofMillis(100))
                    .build();
            e.ingest(new IngestOp("o1", "new", OpType.ADD, "k", new BigDecimal("1"), null, 1000L));
            IngestResult late = e.ingest(new IngestOp("o2", "old", OpType.ADD, "k",
                    new BigDecimal("1"), null, 500L));
            Assert.assertEquals(IngestStatus.LATE, late.status(), "late op rejected");
            IngestResult inTime = e.ingest(new IngestOp("o3", "ok", OpType.ADD, "k",
                    new BigDecimal("2"), null, 950L));
            Assert.assertEquals(IngestStatus.APPLIED, inTime.status(), "within lateness accepted");
        });

        // ------------------------------------------------------------
        // Ledger bookkeeping
        // ------------------------------------------------------------

        r.test("ledger: records old/new key and value on every resolved change", () -> {
            var e = newEngine();
            e.ingest(add("e1", "k1", "10", 1L, null));
            e.ingest(correct("e1", "k2", "40", 2L, null));
            e.ingest(retract("e1", 3L, null));
            List<ResolvedOp> ledger = e.ledger();
            Assert.assertEquals(3L, ledger.size(), "three resolved changes");
            Assert.assertEquals("k1", ledger.get(0).newKey(), "add new key");
            Assert.assertEquals("k1", ledger.get(1).oldKey(), "correct old key");
            Assert.assertEquals("k2", ledger.get(1).newKey(), "correct new key");
            Assert.assertEquals("k2", ledger.get(2).oldKey(), "retract old key");
            Assert.assertNull(ledger.get(2).newKey(), "retract new key null");
        });

        // ------------------------------------------------------------
        // Big acceptance: random-ish ordered stream, retracts, corrections, replay
        // ------------------------------------------------------------

        r.test("acceptance: complex interleaved stream matches ledger recomputation at every step", () -> {
            var e = newEngine();
            // Unversioned stream exercising retract-before-add, corrections, dedupe
            List<IngestOp> stream = List.of(
                    retract("will-add"),                     // buffered
                    add("a", "alpha", "1.50"),
                    add("b", "beta", "2.25"),
                    add("a2", "alpha", "3.00"),
                    correct("b", "alpha", null),             // move b beta->alpha
                    retract("a"),
                    add("c", "gamma", "10"),
                    correct("c", "gamma", "11"),
                    add("will-add", "beta", "7"),            // releases buffered retract
                    retract("a2"));
            for (int i = 0; i < stream.size(); i++) {
                e.ingest(stream.get(i));
                List<String> diffs = e.verifyAgainstLedger();
                Assert.assertTrue(diffs.isEmpty(), "after step " + i + ": " + diffs);
            }
            // alpha: b moved in with 2.25 (a and a2 retracted); beta empty; gamma c=11
            Assert.assertNull(e.aggregates().get("beta"), "beta emptied (will-add retracted)");
            Assert.assertEq(new BigDecimal("2.25"), e.aggregates().get("alpha").sum(), "alpha sum");
            Assert.assertEquals(1L, e.aggregates().get("alpha").count(), "alpha count");
            Assert.assertEq(new BigDecimal("11"), e.aggregates().get("gamma").sum(), "gamma sum");

            // Reference: recompute from the final ledger independently
            Map<String, Aggregate> reference = StreamCorrectionEngine.recomputeFromLedger(e.ledger());
            Assert.assertEquals(e.aggregates().size(), reference.size(), "reference key set size");
            for (String key : e.aggregates().keySet()) {
                Assert.assertEq(e.aggregates().get(key).sum(), reference.get(key).sum(), "ref sum " + key);
                Assert.assertEquals(e.aggregates().get(key).count(), reference.get(key).count(),
                        "ref count " + key);
            }
        });
    }
}
