package bitmapindex;

import java.util.HashSet;
import java.util.Random;
import java.util.Set;

/** Unit tests for the compressed bitmap primitives. */
public final class RoaringBitmapTest {

    public static void run(Asserts a) {
        testBasic(a);
        testChunkBoundary(a);
        testRandomSetOps(a);
        testRangeAndNot(a);
        testCompressionContainers(a);
        testRemove(a);
    }

    private static void testBasic(Asserts a) {
        a.caseName("bitmap/basic");
        RoaringBitmap b = RoaringBitmap.of(3, 10, 65536, 100000);
        a.check(b.contains(3), "contains 3");
        a.check(b.contains(65536), "contains chunk-boundary 65536");
        a.check(!b.contains(4), "not contains 4");
        a.eqInt(b.cardinality(), 4, "cardinality 4");
        int[] arr = b.toArray();
        a.eq(arr.length, 4, "toArray length");
        a.eq(arr[0], 3, "sorted[0]");
        a.eq(arr[3], 100000, "sorted[3]");
        a.eqInt(b.capacity(), 100001, "capacity tracks highest id");
    }

    private static void testChunkBoundary(Asserts a) {
        a.caseName("bitmap/chunk-boundaries");
        RoaringBitmap b = RoaringBitmap.of(65535, 65536);
        a.eqInt(b.chunkCount(), 2, "two chunks");
        a.eqInt(b.capacity(), 65537, "capacity spans chunks");

        RoaringBitmap empty = new RoaringBitmap();
        a.eqInt(empty.capacity(), 0, "empty capacity 0");
        a.eqInt(empty.cardinality(), 0, "empty cardinality 0");
        a.eq(empty.toArray().length, 0, "empty toArray");
    }

    private static void testRandomSetOps(Asserts a) {
        a.caseName("bitmap/random-setops");
        Random rnd = new Random(424242);
        for (int iter = 0; iter < 200; iter++) {
            int universe = 1 + rnd.nextInt(200000);
            int densityA = rnd.nextInt(100);
            int densityB = rnd.nextInt(100);
            Set<Integer> sa = new HashSet<>();
            Set<Integer> sb = new HashSet<>();
            RoaringBitmap ba = new RoaringBitmap();
            RoaringBitmap bb = new RoaringBitmap();
            // sample via random ids rather than full scan for big universes
            int samples = Math.min(universe, 5000);
            for (int k = 0; k < samples; k++) {
                int x = rnd.nextInt(universe);
                if (rnd.nextInt(100) < densityA && sa.add(x)) {
                    ba.add(x);
                }
                if (rnd.nextInt(100) < densityB && sb.add(x)) {
                    bb.add(x);
                }
            }
            // ground truth with sets
            Set<Integer> andSet = new HashSet<>(sa);
            andSet.retainAll(sb);
            Set<Integer> orSet = new HashSet<>(sa);
            orSet.addAll(sb);

            a.eq(toSet(RoaringBitmap.and(ba, bb)), andSet, "AND mismatch iter=" + iter);
            a.eq(toSet(RoaringBitmap.or(ba, bb)), orSet, "OR mismatch iter=" + iter);

            RoaringBitmap universeBm = RoaringBitmap.range(0, universe);
            Set<Integer> notA = new HashSet<>();
            for (int i = 0; i < universe; i++) {
                if (!sa.contains(i)) {
                    notA.add(i);
                }
            }
            a.eq(toSet(RoaringBitmap.notWithin(ba, universeBm)), notA, "NOT-within mismatch iter=" + iter);
        }
    }

    private static void testRangeAndNot(Asserts a) {
        a.caseName("bitmap/range-andNot");
        RoaringBitmap r = RoaringBitmap.range(10, 70000);
        a.eqInt(r.cardinality(), 69990, "range cardinality");
        a.check(r.contains(10) && r.contains(69999) && !r.contains(9) && !r.contains(70000),
                "range edges exact");

        RoaringBitmap r2 = RoaringBitmap.range(0, 0);
        a.eqInt(r2.cardinality(), 0, "empty range");

        RoaringBitmap aBm = RoaringBitmap.range(0, 100);
        RoaringBitmap bBm = RoaringBitmap.of(1, 5, 50, 99);
        RoaringBitmap diff = RoaringBitmap.andNot(aBm, bBm);
        a.eqInt(diff.cardinality(), 96, "andNot cardinality");
        a.check(!diff.contains(50), "andNot removes member");
    }

    private static void testCompressionContainers(Asserts a) {
        a.caseName("bitmap/compression-kinds");
        // sparse -> array containers
        RoaringBitmap sparse = RoaringBitmap.of(1, 1000, 200000, 200001);
        int[] tc = sparse.containerTypeCounts();
        a.eqInt(tc[1], 0, "sparse uses only array containers");

        // dense -> bitmap containers (>4096 per chunk)
        int[] denseIds = new int[5000];
        for (int i = 0; i < 5000; i++) {
            denseIds[i] = i * 2; // all inside chunk 0
        }
        RoaringBitmap dense = RoaringBitmap.fromSorted(denseIds);
        int[] tc2 = dense.containerTypeCounts();
        a.eqInt(tc2[1], 1, "dense chunk uses bitmap container");

        // dense AND sparse must demote back to array container
        RoaringBitmap both = RoaringBitmap.and(dense, sparse);
        for (int id : both.toArray()) {
            a.check(sparse.contains(id) && dense.contains(id), "and membership");
        }
        int[] tc3 = both.containerTypeCounts();
        a.eqInt(tc3[1], 0, "small result demotes to array container");
    }

    private static void testRemove(Asserts a) {
        a.caseName("bitmap/remove");
        RoaringBitmap b = RoaringBitmap.range(0, 100000);
        b.remove(50000);
        a.eqInt(b.cardinality(), 99999, "remove one");
        a.check(!b.contains(50000), "removed id gone");
        a.check(b.contains(49999) && b.contains(50001), "neighbors intact");
        // ids are unchanged, nothing shifted
        a.check(b.contains(99999), "last id unchanged after remove");

        int[] many = new int[99999];
        int p = 0;
        for (int i = 0; i < 100000; i++) {
            if (i != 50000) {
                many[p++] = i;
            }
        }
        a.eq(toSet(b), toSet(RoaringBitmap.fromSorted(many)), "remove vs reference set");
    }

    private static Set<Integer> toSet(RoaringBitmap b) {
        Set<Integer> s = new HashSet<>();
        for (int x : b.toArray()) {
            s.add(x);
        }
        return s;
    }
}
