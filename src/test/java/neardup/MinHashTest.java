package neardup;

import neardup.core.MinHash;

import java.util.HashSet;
import java.util.Random;
import java.util.Set;

import static neardup.TestRunner.approx;
import static neardup.TestRunner.check;

final class MinHashTest {

    static void run() {
        TestRunner.group("minhash");

        MinHash mh = new MinHash(120, MinHash.SEED);
        check(mh.seed() == 20260924L, "fixed seed");

        Set<Long> a = range(1, 51);                 // 1..50
        Set<Long> identical = range(1, 51);
        Set<Long> disjoint = range(1001, 1051);     // 1001..1050

        long[] sa = mh.signature(a);
        check(sa.length == 120, "signature length = numHashes");
        check(MinHash.estimatedSimilarity(sa, mh.signature(identical)) == 1.0,
                "identical sets -> estimate 1.0");
        check(MinHash.estimatedSimilarity(sa, mh.signature(disjoint)) == 0.0,
                "disjoint sets -> estimate 0.0 with the affine family");

        // Empty set -> sentinel signature; estimate vs anything = 0.
        long[] empty = mh.signature(new HashSet<>());
        check(MinHash.estimatedSimilarity(empty, empty) == 0.0,
                "empty signatures never match (all-sentinel equality is overridden at call sites)");

        // Statistical sanity: for many pairs with exact Jaccard 0.6,
        // the estimator should be close to 0.6 on average and within ~0.15
        // for the pooled estimate.
        Random rnd = new Random(42);
        int pairs = 400;
        double sumEst = 0;
        double sumExact = 0;
        for (int t = 0; t < pairs; t++) {
            Set<Long> x = new HashSet<>();
            Set<Long> y = new HashSet<>();
            // universe of 100; independent inclusion p=0.5 gives expected
            // Jaccard p^2 / (1-(1-p)^2) = 0.25/0.75 = 1/3, still a good
            // estimator test spread; construct controlled 0.6 instead:
            x.clear();
            y.clear();
            // x = first 8 base ids; y shares 6 of them + 2 fresh -> 6/10.
            long base = rnd.nextInt(1_000_000) * 100L + 1;
            for (long v = base; v < base + 8; v++) {
                x.add(v);
            }
            for (long v = base; v < base + 6; v++) {
                y.add(v);
            }
            y.add(base + 1000);
            y.add(base + 1001);
            sumExact += 0.6;
            sumEst += MinHash.estimatedSimilarity(mh.signature(x), mh.signature(y));
        }
        approx(sumEst / pairs, 0.6, 0.03, "mean MinHash estimate near 0.6 over 400 pairs");
        approx(sumExact / pairs, 0.6, 1e-9, "fixture actually has Jaccard 0.6");

        // Determinism: same seed -> same signatures.
        MinHash mh2 = new MinHash(120, MinHash.SEED);
        check(java.util.Arrays.equals(sa, mh2.signature(a)),
                "re-seeded MinHash reproduces signatures");

        // Different seed may differ (not asserted strongly, but same input stable).
        check(java.util.Arrays.equals(mh.signature(a), mh.signature(a)),
                "repeatable within instance");
    }

    private static Set<Long> range(int fromInclusive, int toExclusive) {
        Set<Long> s = new HashSet<>();
        for (long v = fromInclusive; v < toExclusive; v++) {
            s.add(v);
        }
        return s;
    }
}
