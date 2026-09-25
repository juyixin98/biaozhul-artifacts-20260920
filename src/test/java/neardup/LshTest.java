package neardup;

import neardup.core.Lsh;
import neardup.core.MinHash;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;

import static neardup.TestRunner.check;

final class LshTest {

    static void run() {
        TestRunner.group("lsh");

        check(Lsh.DEFAULT_BANDS * Lsh.DEFAULT_ROWS == 120,
                "60 bands x 2 rows cover the 120 MinHash hashes");

        MinHash mh = new MinHash();
        Lsh lsh = new Lsh(); // 60x2

        // ---- 1. Disjoint synthetic pairs are NEVER candidates -------------
        // (With the exact affine hash family, disjoint sets never agree on a
        // MinHash row.)
        long[][] disjointSigs = new long[200][];
        for (int i = 0; i < 200; i++) {
            disjointSigs[i] = mh.signature(range(10_000 + i * 50, 10_000 + i * 50 + 50));
        }
        List<Lsh.CandidatePair> cands = lsh.candidates(disjointSigs);
        check(cands.isEmpty(),
                "zero candidate pairs among 200 pairwise-disjoint sets",
                "got " + cands.size());

        // ---- 2. Statistical recall at Jaccard 0.6 -------------------------
        // 300 controlled 8-element pairs sharing 6 (exact 0.6). The S-curve
        // 1-(1-0.6^2)^60 ~ 0.99986, so essentially every pair must be recalled.
        List<long[]> batch = new ArrayList<>();
        Random rnd = new Random(7);
        int pairs = 300;
        for (int t = 0; t < pairs; t++) {
            long base = rnd.nextInt(2_000_000) * 100L + 1;
            Set<Long> x = new HashSet<>();
            Set<Long> y = new HashSet<>();
            for (long v = base; v < base + 8; v++) {
                x.add(v);
            }
            for (long v = base; v < base + 6; v++) {
                y.add(v);
            }
            y.add(base + 5000);
            y.add(base + 5001);
            batch.add(mh.signature(x));
            batch.add(mh.signature(y));
        }
        long[][] sigs = batch.toArray(new long[0][]);
        List<Lsh.CandidatePair> recalled = lsh.candidates(sigs);
        // Count how many of the designed (2t, 2t+1) pairs are candidates.
        boolean[][] flag = new boolean[sigs.length][sigs.length];
        for (Lsh.CandidatePair cp : recalled) {
            flag[cp.i()][cp.j()] = true;
        }
        int hit = 0;
        for (int t = 0; t < pairs; t++) {
            if (flag[2 * t][2 * t + 1]) {
                hit++;
            }
        }
        double recall = (double) hit / pairs;
        check(recall >= 0.97,
                "LSH recall at Jaccard 0.6 >= 0.97 (S-curve ~0.9999)",
                "observed recall " + recall + " (" + hit + "/" + pairs + ")");

        // ---- 3. Identical sets always collide in every band ----------------
        Set<Long> a = range(1, 41);
        long[][] same = {mh.signature(a), mh.signature(range(1, 41))};
        List<Lsh.CandidatePair> onePair = lsh.candidates(same);
        check(onePair.size() == 1, "identical sets yield exactly one candidate pair");
        check(onePair.get(0).bandHits() == 60, "identical sets collide in all 60 bands",
                "bandHits=" + onePair.get(0).bandHits());

        // ---- 4. Empty signatures never participate -------------------------
        long[] empty = mh.signature(new HashSet<>());
        long[][] withEmpty = {empty, empty, mh.signature(range(1, 20))};
        check(lsh.candidates(withEmpty).isEmpty(),
                "empty-shingle documents produce no candidates, even to each other");
    }

    private static Set<Long> range(int fromInclusive, int toExclusive) {
        Set<Long> s = new HashSet<>();
        for (long v = fromInclusive; v < toExclusive; v++) {
            s.add(v);
        }
        return s;
    }
}
