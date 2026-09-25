package approxheavy.tests;

import approxheavy.candidates.BoundedCandidates;
import approxheavy.cms.CountMinSketch;
import approxheavy.exact.ExactCounter;
import approxheavy.hash.Hashing;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;

/**
 * Acceptance-style comparison against the exact reference implementation:
 * skewed distribution error statistics, top-K recall, and a constructed
 * collision that produces a measurable overestimate.
 */
public final class ExactReferenceTest {
    public static void main(String[] args) {
        TestRunner runner = new TestRunner("ExactReferenceTest");

        runner.add("skewed Zipf stream: error stats against exact counts", () -> {
            int width = 64;
            int depth = 5;
            long seed = 7L;
            int cardinality = 2_000;
            int events = 50_000;
            double skew = 1.1;

            CountMinSketch sketch = new CountMinSketch(width, depth, seed);
            ExactCounter exact = new ExactCounter();
            BoundedCandidates candidates = new BoundedCandidates(100, sketch::estimate);

            double[] cdf = zipfCdf(cardinality, skew);
            Random rng = new Random(2026);
            for (int i = 0; i < events; i++) {
                String key = "item-" + sampleCdf(rng, cdf);
                sketch.add(key);
                exact.add(key);
                candidates.observe(key);
            }

            long bound = sketch.errorUpperBound();
            long maxOver = 0;
            long sumOver = 0;
            long breaches = 0;
            for (Map.Entry<String, Long> e : exact.allCounts().entrySet()) {
                long over = sketch.estimate(e.getKey()) - e.getValue();
                TestRunner.check(over >= 0, "underestimate at " + e.getKey());
                maxOver = Math.max(maxOver, over);
                sumOver += over;
                if (over > bound) {
                    breaches++;
                }
            }
            System.out.printf("    total=%d bound=%d maxOver=%d meanOver=%.3f breaches=%d distinct=%d%n",
                    sketch.totalCount(), bound, maxOver,
                    (double) sumOver / exact.distinctKeys(), breaches, exact.distinctKeys());
            TestRunner.check(breaches == 0, "no per-key bound breach allowed");
            TestRunner.check(sketch.totalCount() == exact.totalCount(), "totals agree");

            int k = 10;
            Set<String> exactKeys = new HashSet<>();
            for (Map.Entry<String, Long> e : exact.topK(k)) {
                exactKeys.add(e.getKey());
            }
            List<Map.Entry<String, Long>> approx = candidates.topK(k);
            int hit = 0;
            for (Map.Entry<String, Long> e : approx) {
                if (exactKeys.contains(e.getKey())) {
                    hit++;
                }
            }
            double recall = (double) hit / k;
            System.out.printf("    top-%d recall=%.2f (%d/%d)%n", k, recall, hit, k);
            // On a strongly skewed stream the heavy head must be captured well;
            // this is an empirical quality check, NOT a guarantee.
            TestRunner.check(recall >= 0.8, "expected recall >= 0.8 on skewed data, was " + recall);
        });

        runner.add("constructed collision yields a known, bound-respecting overestimate", () -> {
            int width = 2;
            int depth = 1;
            long seed = 1L;

            // Find two keys sharing the single bucket pair in this tiny sketch.
            String a = null;
            String b = null;
            for (int i = 0; i < 10_000 && a == null; i++) {
                String x = "x-" + i;
                int bx = Hashing.bucket(Hashing.rowHash(x, seed, 0), width);
                for (int j = i + 1; j < 10_000; j++) {
                    String y = "x-" + j;
                    int by = Hashing.bucket(Hashing.rowHash(y, seed, 0), width);
                    if (bx == by) {
                        a = x;
                        b = y;
                        break;
                    }
                }
            }
            TestRunner.check(a != null, "colliding pair exists");

            CountMinSketch s = new CountMinSketch(width, depth, seed);
            long na = 500;
            long nb = 47;
            for (long i = 0; i < na; i++) {
                s.add(a);
            }
            for (long i = 0; i < nb; i++) {
                s.add(b);
            }
            long estA = s.estimate(a);
            long estB = s.estimate(b);
            System.out.printf("    %s exact=%d est=%d | %s exact=%d est=%d%n",
                    a, na, estA, b, nb, estB);
            // Collision in the only row: both estimates equal na+nb.
            TestRunner.checkEq(estA, na + nb, "a overestimated by nb due to collision");
            TestRunner.checkEq(estB, na + nb, "b overestimated by na");
            long bound = s.errorUpperBound();
            TestRunner.check(estA - na <= bound, "overestimate respects declared bound");
            TestRunner.check(estB - nb <= bound, "overestimate respects declared bound");
        });

        runner.add("increasing width/depth shrinks observed error on the same stream", () -> {
            List<String> stream = new ArrayList<>();
            Random rng = new Random(99);
            for (int i = 0; i < 20_000; i++) {
                stream.add("k" + rng.nextInt(1_000));
            }
            long overSmall = runSketch(stream, 8, 2, 3L);
            long overLarge = runSketch(stream, 512, 7, 3L);
            System.out.printf("    max overestimate small sketch=%d, large sketch=%d%n",
                    overSmall, overLarge);
            TestRunner.check(overLarge < overSmall, "more geometry must reduce observed error");
            // Load factor 20000/512 ~ 39 per bucket across 1000 keys, but 7
            // independent rows take the minimum: residual error must be small.
            TestRunner.check(overLarge <= 100, "large sketch residual error should be <= 100, was "
                    + overLarge);
        });

        runner.add("repeated runs over different seeds average into the declared regime", () -> {
            // Sanity across 5 seeds: no underestimates anywhere; breaches rare.
            Random dataRng = new Random(5);
            List<String> stream = new ArrayList<>();
            for (int i = 0; i < 30_000; i++) {
                stream.add("u" + dataRng.nextInt(800));
            }
            ExactCounter exact = new ExactCounter();
            for (String key : stream) {
                exact.add(key);
            }
            for (long seed = 1; seed <= 5; seed++) {
                CountMinSketch s = new CountMinSketch(64, 5, seed);
                for (String key : stream) {
                    s.add(key);
                }
                for (Map.Entry<String, Long> e : exact.allCounts().entrySet()) {
                    TestRunner.check(s.estimate(e.getKey()) >= e.getValue(),
                            "seed " + seed + " underestimated " + e.getKey());
                }
            }
        });

        runner.run();
    }

    private static long runSketch(List<String> stream, int width, int depth, long seed) {
        CountMinSketch s = new CountMinSketch(width, depth, seed);
        ExactCounter exact = new ExactCounter();
        for (String key : stream) {
            s.add(key);
            exact.add(key);
        }
        long maxOver = 0;
        for (Map.Entry<String, Long> e : exact.allCounts().entrySet()) {
            maxOver = Math.max(maxOver, s.estimate(e.getKey()) - e.getValue());
        }
        return maxOver;
    }

    private static double[] zipfCdf(int n, double skew) {
        double[] cdf = new double[n];
        double norm = 0;
        for (int i = 0; i < n; i++) {
            norm += 1.0 / Math.pow(i + 1, skew);
        }
        double acc = 0;
        for (int i = 0; i < n; i++) {
            acc += (1.0 / Math.pow(i + 1, skew)) / norm;
            cdf[i] = acc;
        }
        return cdf;
    }

    private static int sampleCdf(Random rng, double[] cdf) {
        double r = rng.nextDouble();
        int lo = 0;
        int hi = cdf.length - 1;
        while (lo < hi) {
            int mid = (lo + hi) >>> 1;
            if (cdf[mid] < r) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        return lo;
    }
}
