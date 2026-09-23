package test.cms;

import java.util.HashMap;
import java.util.Map;

import approx.cms.CountMinSketch;
import approx.cms.SketchIncompatibleException;
import approx.cms.SketchSnapshot;
import test.Asserts;
import test.TestRunner;

public final class CountMinSketchTest {

    private CountMinSketchTest() {
    }

    public static void register(TestRunner r) {
        r.add("cms: exact when no collisions (distinct items < width)", CountMinSketchTest::testNoCollisionExact);
        r.add("cms: estimate never underestimates", CountMinSketchTest::testNeverUnderestimates);
        r.add("cms: collisions over-estimate deterministically", CountMinSketchTest::testCollisionOverestimate);
        r.add("cms: merge compatible sketches adds counts", CountMinSketchTest::testMergeCompatible);
        r.add("cms: merge rejects different width", CountMinSketchTest::testMergeRejectsWidth);
        r.add("cms: merge rejects different depth", CountMinSketchTest::testMergeRejectsDepth);
        r.add("cms: merge rejects different seed", CountMinSketchTest::testMergeRejectsSeed);
        r.add("cms: errorUpperBound scales with total count", CountMinSketchTest::testErrorBound);
        r.add("cms: standard sizing uses e/epsilon and ln(1/delta)", CountMinSketchTest::testStandardSizing);
        r.add("cms: snapshot round trip preserves counts", CountMinSketchTest::testSnapshotRoundTrip);
        r.add("cms: same seed yields deterministic buckets", CountMinSketchTest::testDeterministicBuckets);
    }

    private static void testNoCollisionExact() {
        CountMinSketch cms = new CountMinSketch(256, 5, 42L);
        Map<String, Long> truth = new HashMap<>();
        for (int i = 0; i < 50; i++) {
            String item = "item-" + i;
            long n = i + 1L;
            for (long j = 0; j < n; j++) {
                cms.add(item);
            }
            truth.put(item, n);
        }
        // With wide rows and only 50 items, accidental collisions are overwhelmingly
        // unlikely on all 5 rows; assert exactness for every item.
        for (Map.Entry<String, Long> e : truth.entrySet()) {
            Asserts.assertEquals(e.getValue(), cms.estimate(e.getKey()),
                    "exact estimate for " + e.getKey());
        }
        Asserts.assertEquals(truth.values().stream().mapToLong(Long::longValue).sum(),
                cms.totalCount(), "total count");
    }

    private static void testNeverUnderestimates() {
        CountMinSketch cms = new CountMinSketch(32, 4, 7L); // narrow: forces collisions
        Map<String, Long> truth = new HashMap<>();
        java.util.Random rnd = new java.util.Random(123);
        for (int i = 0; i < 20_000; i++) {
            String item = "k" + rnd.nextInt(500);
            cms.add(item);
            truth.merge(item, 1L, Long::sum);
        }
        for (Map.Entry<String, Long> e : truth.entrySet()) {
            Asserts.assertGreaterEqual(cms.estimate(e.getKey()), e.getValue(),
                    "estimate >= true for " + e.getKey());
        }
        // Unseen item: can be non-zero only by collision; with this wide sketch and
        // the 500 inserted keys it almost surely is 0. Use a fresh wide sketch for a
        // deterministic zero check.
        CountMinSketch fresh = new CountMinSketch(4096, 5, 123);
        Asserts.assertEquals(0L, fresh.estimate("never-seen"), "unseen item estimate on empty sketch");
    }

    private static void testCollisionOverestimate() {
        // Find two distinct strings hashing to the same bucket on every row at width=8, seed=1.
        // Pack the per-row bucket tuple into one signature key; a key repeat is a full collision.
        int width = 8;
        int depth = 3;
        long seed = 1L;
        CountMinSketch probe = new CountMinSketch(width, depth, seed);
        Map<Long, String> seen = new HashMap<>();
        String victim = null;
        String collider = null;
        for (int i = 0; i < 100_000 && victim == null; i++) {
            String candidate = "pair-" + i;
            int[] idx = probe.bucketIndices(candidate);
            long sig = 0;
            for (int row = 0; row < depth; row++) {
                sig = sig * width + idx[row];
            }
            String earlier = seen.get(sig);
            if (earlier != null) {
                victim = earlier;
                collider = candidate;
            } else {
                seen.put(sig, candidate);
            }
        }
        Asserts.assertTrue(victim != null, "found a colliding pair");

        CountMinSketch cms = new CountMinSketch(width, depth, seed);
        for (int i = 0; i < 10; i++) {
            cms.add(victim);
        }
        for (int i = 0; i < 90; i++) {
            cms.add(collider);
        }
        Asserts.assertEquals(100L, cms.estimate(victim),
                "collision inflates victim estimate to 100 (true=10)");
        Asserts.assertEquals(100L, cms.estimate(collider), "collider estimate");
    }

    private static void testMergeCompatible() {
        CountMinSketch a = new CountMinSketch(64, 4, 99L);
        CountMinSketch b = new CountMinSketch(64, 4, 99L);
        a.add("x", 5);
        a.add("shared", 7);
        b.add("y", 3);
        b.add("shared", 2);
        a.merge(b);
        Asserts.assertEquals(9L, a.estimate("shared"), "merged shared count");
        Asserts.assertEquals(17L, a.totalCount(), "merged total");
        Asserts.assertEquals(5L, a.estimate("x"), "x preserved");
        Asserts.assertEquals(3L, a.estimate("y"), "y added");
    }

    private static void testMergeRejectsWidth() {
        CountMinSketch a = new CountMinSketch(64, 4, 99L);
        CountMinSketch b = new CountMinSketch(32, 4, 99L);
        expectIncompatible(a, b, "width");
    }

    private static void testMergeRejectsDepth() {
        CountMinSketch a = new CountMinSketch(64, 4, 99L);
        CountMinSketch b = new CountMinSketch(64, 5, 99L);
        expectIncompatible(a, b, "depth");
    }

    private static void testMergeRejectsSeed() {
        CountMinSketch a = new CountMinSketch(64, 4, 99L);
        CountMinSketch b = new CountMinSketch(64, 4, 100L);
        // Sketches may happen to align buckets for some items, but the merge must
        // be refused purely on seed mismatch.
        expectIncompatible(a, b, "seed");
    }

    private static void expectIncompatible(CountMinSketch a, CountMinSketch b, String what) {
        try {
            a.merge(b);
            Asserts.fail("merge with different " + what + " must be rejected");
        } catch (SketchIncompatibleException expected) {
            // expected
        }
        Asserts.assertFalse(a.compatibleWith(b), "compatibleWith false on " + what);
    }

    private static void testErrorBound() {
        CountMinSketch cms = CountMinSketch.withErrorTarget(0.01, 0.01, 0L);
        Asserts.assertEquals(272, cms.width(), "ceil(e/0.01)=272");
        Asserts.assertEquals(5, cms.depth(), "ceil(ln(100))=5");
        Asserts.assertEquals(0L, cms.errorUpperBound(), "bound zero on empty sketch");
        for (int i = 0; i < 100_000; i++) {
            cms.add("anything");
        }
        long bound = cms.errorUpperBound();
        // epsilon * N = (e/272) * 100000 ~= 999.6 -> ceil 1000
        Asserts.assertGreaterEqual(bound, 999L, "bound ~= epsilon*N");
        Asserts.assertLessEqual(bound, 1005L, "bound ~= epsilon*N");
    }

    private static void testStandardSizing() {
        CountMinSketch cms = CountMinSketch.withErrorTarget(0.1, 0.05, 0L);
        Asserts.assertEquals(28, cms.width(), "ceil(e/0.1)=28");
        Asserts.assertEquals(3, cms.depth(), "ceil(ln20)=3");
    }

    private static void testSnapshotRoundTrip() {
        CountMinSketch cms = new CountMinSketch(16, 3, 5L);
        cms.add("a", 4);
        cms.add("b", 6);
        SketchSnapshot snap = cms.snapshot();
        CountMinSketch restored = CountMinSketch.fromSnapshot(snap);
        Asserts.assertEquals(16, cms.width(), "layout preserved width");
        Asserts.assertEquals(3, restored.depth(), "layout preserved depth");
        Asserts.assertEquals(6L, restored.estimate("b"), "counts preserved");
        Asserts.assertEquals(10L, restored.totalCount(), "total preserved");
        // Snapshot must be a copy, not a live view.
        cms.add("a", 100);
        Asserts.assertEquals(4L, restored.estimate("a"), "snapshot immutable after copy");
    }

    private static void testDeterministicBuckets() {
        CountMinSketch a = new CountMinSketch(32, 4, 777L);
        CountMinSketch b = new CountMinSketch(32, 4, 777L);
        int[] ia = a.bucketIndices("deterministic");
        int[] ib = b.bucketIndices("deterministic");
        for (int row = 0; row < 4; row++) {
            Asserts.assertEquals(ia[row], ib[row], "same seed -> same bucket row " + row);
        }
    }
}
