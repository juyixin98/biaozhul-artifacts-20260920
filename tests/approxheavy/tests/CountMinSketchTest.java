package approxheavy.tests;

import approxheavy.cms.CountMinSketch;
import approxheavy.cms.IncompatibleSketchException;
import approxheavy.hash.Hashing;
import approxheavy.json.Json;

import java.util.HashMap;
import java.util.Map;

/** Core Count-Min Sketch guarantees: never underestimate, bound respected, merge rules. */
public final class CountMinSketchTest {
    public static void main(String[] args) {
        TestRunner runner = new TestRunner("CountMinSketchTest");

        runner.add("estimate is exact on empty stream", () -> {
            CountMinSketch s = new CountMinSketch(64, 4, 1L);
            TestRunner.checkEq(s.estimate("x"), 0L, "unknown key must estimate 0");
            TestRunner.checkEq(s.totalCount(), 0L, "total 0");
        });

        runner.add("single key is counted exactly (no collision with itself)", () -> {
            CountMinSketch s = new CountMinSketch(64, 4, 1L);
            for (int i = 0; i < 1000; i++) {
                s.add("alpha");
            }
            TestRunner.checkEq(s.estimate("alpha"), 1000L, "single key exact");
            TestRunner.checkEq(s.totalCount(), 1000L, "total exact");
        });

        runner.add("never underestimates any key", () -> {
            CountMinSketch s = new CountMinSketch(32, 5, 2L);
            Map<String, Long> exact = new HashMap<>();
            java.util.Random rng = new java.util.Random(7);
            for (int i = 0; i < 20_000; i++) {
                String key = "k" + rng.nextInt(500);
                s.add(key);
                exact.merge(key, 1L, Long::sum);
            }
            for (Map.Entry<String, Long> e : exact.entrySet()) {
                TestRunner.check(s.estimate(e.getKey()) >= e.getValue(),
                        "underestimate for " + e.getKey());
            }
        });

        runner.add("observed overestimate never exceeds the declared bound", () -> {
            // Width 32 -> bound = ceil(2/31 * total); depth 5 -> failure prob 1/32.
            CountMinSketch s = new CountMinSketch(32, 5, 2L);
            Map<String, Long> exact = new HashMap<>();
            java.util.Random rng = new java.util.Random(11);
            int n = 10_000;
            for (int i = 0; i < n; i++) {
                String key = "k" + rng.nextInt(300);
                s.add(key);
                exact.merge(key, 1L, Long::sum);
            }
            long bound = s.errorUpperBound();
            long maxOver = 0;
            for (Map.Entry<String, Long> e : exact.entrySet()) {
                long over = s.estimate(e.getKey()) - e.getValue();
                maxOver = Math.max(maxOver, over);
            }
            System.out.println("    bound=" + bound + " maxObservedOverestimate=" + maxOver);
            TestRunner.check(maxOver <= bound,
                    "overestimate " + maxOver + " exceeded bound " + bound);
        });

        runner.add("merge of compatible sketches adds counts", () -> {
            CountMinSketch a = new CountMinSketch(32, 5, 9L);
            CountMinSketch b = new CountMinSketch(32, 5, 9L);
            a.add("shared", 10);
            b.add("shared", 5);
            b.add("onlyB", 3);
            a.mergeWith(b);
            TestRunner.checkEq(a.estimate("shared"), 15L, "shared adds");
            TestRunner.checkEq(a.estimate("onlyB"), 3L, "onlyB visible");
            TestRunner.checkEq(a.totalCount(), 18L, "total adds");
        });

        runner.add("merge rejects different seed", () -> {
            CountMinSketch a = new CountMinSketch(32, 5, 1L);
            CountMinSketch b = new CountMinSketch(32, 5, 2L);
            expectIncompatible(a, b);
        });

        runner.add("merge rejects different width", () -> {
            CountMinSketch a = new CountMinSketch(32, 5, 1L);
            CountMinSketch b = new CountMinSketch(33, 5, 1L);
            expectIncompatible(a, b);
        });

        runner.add("merge rejects different depth", () -> {
            CountMinSketch a = new CountMinSketch(32, 5, 1L);
            CountMinSketch b = new CountMinSketch(32, 4, 1L);
            expectIncompatible(a, b);
        });

        runner.add("isCompatibleWith reflects geometry and seed", () -> {
            CountMinSketch a = new CountMinSketch(32, 5, 1L);
            TestRunner.check(a.isCompatibleWith(new CountMinSketch(32, 5, 1L)), "same -> compatible");
            TestRunner.check(!a.isCompatibleWith(new CountMinSketch(32, 5, 2L)), "seed diff");
            TestRunner.check(!a.isCompatibleWith(new CountMinSketch(16, 5, 1L)), "width diff");
            TestRunner.check(!a.isCompatibleWith(new CountMinSketch(32, 6, 1L)), "depth diff");
        });

        runner.add("different seeds hash the same key to different buckets (usually)", () -> {
            int width = 256;
            int differ = 0;
            for (int i = 0; i < 100; i++) {
                String key = "key-" + i;
                int b1 = Hashing.bucket(Hashing.rowHash(key, 1L, 0), width);
                int b2 = Hashing.bucket(Hashing.rowHash(key, 2L, 0), width);
                if (b1 != b2) {
                    differ++;
                }
            }
            TestRunner.check(differ > 90, "seeds should differ nearly always, was " + differ);
        });

        runner.add("JSON round trip preserves geometry, seed, totals and estimates", () -> {
            CountMinSketch s = new CountMinSketch(16, 3, 123L);
            s.add("foo", 7);
            s.add("bar", 9);
            String json = s.toJson();
            CountMinSketch restored = CountMinSketch.fromJson(json);
            TestRunner.checkEq(restored.width(), 16, "width");
            TestRunner.checkEq(restored.depth(), 3, "depth");
            TestRunner.checkEq(restored.seed(), 123L, "seed");
            TestRunner.checkEq(restored.totalCount(), 16L, "total");
            TestRunner.checkEq(restored.estimate("foo"), s.estimate("foo"), "foo estimate");
            TestRunner.checkEq(restored.estimate("bar"), s.estimate("bar"), "bar estimate");
        });

        runner.add("JSON with inconsistent row totals is rejected", () -> {
            String bad = "{\"type\":\"CountMinSketch\",\"width\":4,\"depth\":2,\"seed\":1,"
                    + "\"totalCount\":99,\"cells\":[[5,0,0,0],[1,0,0,0]]}";
            expectBadJson(bad);
        });

        runner.add("JSON with wrong row count is rejected", () -> {
            String bad = "{\"type\":\"CountMinSketch\",\"width\":4,\"depth\":3,\"seed\":1,"
                    + "\"cells\":[[0,0,0,0],[0,0,0,0]]}";
            expectBadJson(bad);
        });

        runner.add("withError sizes width/depth per epsilon,delta", () -> {
            CountMinSketch s = CountMinSketch.withError(0.01, 0.01, 0L);
            TestRunner.check(s.width() >= 271, "width ~ ceil(e/0.01)=272, was " + s.width());
            TestRunner.check(s.depth() >= 4, "depth ~ ceil(ln 100)=5, was " + s.depth());
            System.out.println("    epsilon=0.01 delta=0.01 -> width=" + s.width()
                    + " depth=" + s.depth());
        });

        runner.add("weighted adds and multiple independent partitions merge exactly", () -> {
            CountMinSketch merged = new CountMinSketch(64, 5, 42L);
            long[] weights = {3, 7, 11};
            for (long w : weights) {
                CountMinSketch part = new CountMinSketch(64, 5, 42L);
                part.add("x", w);
                merged.mergeWith(part);
            }
            TestRunner.checkEq(merged.estimate("x"), 21L, "sum of partitions");
        });

        runner.add("serialized sketch is itself valid JSON parseable by the generic parser", () -> {
            CountMinSketch s = new CountMinSketch(8, 2, 5L);
            s.add("z", 4);
            Map<String, Object> parsed = Json.parseObject(s.toJson());
            TestRunner.check("CountMinSketch".equals(parsed.get("type")), "type field");
            TestRunner.check(((Number) parsed.get("seed")).longValue() == 5L, "seed field");
        });

        runner.run();
    }

    private static void expectIncompatible(CountMinSketch a, CountMinSketch b) {
        try {
            a.mergeWith(b);
            throw new AssertionError("merge should have been rejected");
        } catch (IncompatibleSketchException expected) {
            // intended
        }
    }

    private static void expectBadJson(String json) {
        try {
            CountMinSketch.fromJson(json);
            throw new AssertionError("bad sketch JSON should have been rejected");
        } catch (IllegalArgumentException expected) {
            // intended
        }
    }
}
