package com.example.vecsearch;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * Core tests without any network: distance math, dimension validation,
 * zero-vector rejection, exact search, delete semantics, filters,
 * budget accounting and IVF recall on clustered data.
 */
public class CoreTests {

    public static void run(TestFramework t) {
        testDistances(t);
        testInsertValidation(t);
        testExactL2Search(t);
        testExactCosineSearch(t);
        testZeroVectorRejection(t);
        testDeletes(t);
        testFilters(t);
        testBudget(t);
        testIvfRecall(t);
    }

    private static void testDistances(TestFramework t) {
        t.section("distance math");
        Distance.Counter c = new Distance.Counter();

        double[] a = {0, 0};
        double[] b = {3, 4};
        double d = Distance.l2(a, b, c);
        checkClose(t, d, 25.0, 1e-12, "L2 squared distance of (0,0)-(3,4) is 25");
        t.check(c.vectorDistances() == 1, "one L2 call counted as one vector distance");

        double[] u = {1, 0};
        double[] v = {0, 1};
        double cd = Distance.cosine(u, v, 1, 1, c);
        checkClose(t, cd, 1.0, 1e-12, "cosine distance of orthogonal unit vectors is 1");

        double[] same = {2, 0};
        checkClose(t, Distance.cosine(u, same, 1, 2, c), 0.0, 1e-12,
                "cosine distance of identical-direction vectors is 0");
        checkClose(t, Distance.cosine(u, new double[]{-1, 0}, 1, 1, c), 2.0, 1e-12,
                "cosine distance of opposite vectors is 2");
    }

    private static void testInsertValidation(TestFramework t) {
        t.section("dimension validation on insert / upsert");
        VectorStore store = new VectorStore();
        store.upsert("a", new double[]{1, 2, 3}, Math.sqrt(14), Map.of(), Metric.L2);

        t.expectApiError(400,
                () -> store.upsert("b", new double[]{1, 2}, Math.sqrt(5), Map.of(), Metric.L2),
                "inserting a 2-D vector into a 3-D store is rejected");

        // Upsert keeps dimension validation.
        t.expectApiError(400,
                () -> store.upsert("a", new double[]{1, 2, 3, 4}, Math.sqrt(30), Map.of(), Metric.L2),
                "upserting an existing id with a different dimension is rejected");

        boolean replaced = store.upsert("a", new double[]{4, 5, 6}, Math.sqrt(77), Map.of(), Metric.L2);
        t.check(replaced, "same-dimension upsert replaces and reports replaced=true");
        t.check(store.size() == 1, "upsert does not grow the store");
    }

    private static void testExactL2Search(TestFramework t) {
        t.section("exact L2 search correctness");
        SearchService svc = newService();
        svc.upsert("p1", new double[]{0, 0}, Map.of(), Metric.L2);
        svc.upsert("p2", new double[]{1, 0}, Map.of(), Metric.L2);
        svc.upsert("p3", new double[]{10, 10}, Map.of(), Metric.L2);
        svc.upsert("p4", new double[]{0.5, 0.5}, Map.of(), Metric.L2);

        SearchService.Outcome out = svc.search(new double[]{0.1, 0.1}, Metric.L2, 3,
                Filter.from(Map.of()), "exact", null, 0);
        List<String> ids = out.hits.stream().map(SearchHit::id).toList();
        t.check(ids.equals(List.of("p1", "p4", "p2")),
                "top-3 returned in distance order, got " + ids);
        t.check(out.vectorDistances == 4,
                "exact search computes one distance per live vector (4), got "
                        + out.vectorDistances);

        // Monotonic non-decreasing distances.
        boolean sorted = true;
        for (int i = 1; i < out.hits.size(); i++) {
            if (out.hits.get(i).distance() < out.hits.get(i - 1).distance()) {
                sorted = false;
            }
        }
        t.check(sorted, "hit distances are non-decreasing");
    }

    private static void testExactCosineSearch(TestFramework t) {
        t.section("exact cosine search correctness");
        SearchService svc = newService();
        // Direction, not magnitude, determines cosine ranking.
        svc.upsert("same", new double[]{2, 2}, Map.of(), Metric.COSINE);
        svc.upsert("orth", new double[]{0, 5}, Map.of(), Metric.COSINE);
        svc.upsert("opp", new double[]{-3, -3}, Map.of(), Metric.COSINE);

        SearchService.Outcome out = svc.search(new double[]{1, 1}, Metric.COSINE, 2,
                Filter.from(Map.of()), "exact", null, 0);
        List<String> ids = out.hits.stream().map(SearchHit::id).toList();
        t.check(ids.equals(List.of("same", "orth")),
                "cosine ranks by direction (same-direction first), got " + ids);
        checkClose(t, out.hits.get(0).distance(), 0.0, 1e-12,
                "closest cosine distance is 0 regardless of magnitude");
    }

    private static void testZeroVectorRejection(TestFramework t) {
        t.section("cosine rejects zero vectors");
        SearchService svc = newService();

        t.expectApiError(400,
                () -> svc.upsert("z", new double[]{0, 0}, Map.of(), Metric.COSINE),
                "inserting a zero vector under COSINE is rejected");

        svc.upsert("ok", new double[]{1, 1}, Map.of(), Metric.COSINE);
        t.expectApiError(400,
                () -> svc.search(new double[]{0, 0}, Metric.COSINE, 1,
                        Filter.from(Map.of()), "exact", null, 0),
                "querying with a zero vector under COSINE is rejected");

        // Zero vectors are legitimate under L2.
        svc.upsert("zl2", new double[]{0, 0}, Map.of(), Metric.L2);
        SearchService.Outcome out = svc.search(new double[]{0, 0}, Metric.L2, 1,
                Filter.from(Map.of()), "exact", null, 0);
        t.check(out.hits.get(0).id().equals("zl2"),
                "zero vectors are stored and matched under L2");
    }

    private static void testDeletes(TestFramework t) {
        t.section("delete semantics");
        SearchService svc = newService();
        svc.upsert("a", new double[]{0, 0}, Map.of("k", "x"), Metric.L2);
        svc.upsert("b", new double[]{1, 1}, Map.of("k", "y"), Metric.L2);
        svc.upsert("c", new double[]{2, 2}, Map.of("k", "x"), Metric.L2);

        t.check(svc.delete("b"), "deleting an existing id returns true");
        t.check(!svc.delete("b"), "deleting the same id again returns false");
        t.check(!svc.delete("ghost"), "deleting a never-existing id returns false");

        SearchService.Outcome exact = svc.search(new double[]{0.9, 0.9}, Metric.L2, 10,
                Filter.from(Map.of()), "exact", null, 0);
        t.check(exact.hits.stream().noneMatch(h -> h.id().equals("b")),
                "exact search never returns a deleted id");

        // Even when the deleted vector is the true nearest neighbour.
        SearchService.Outcome approx = svc.search(new double[]{1.0, 1.0}, Metric.L2, 10,
                Filter.from(Map.of()), "approx", null, 100);
        t.check(approx.hits.stream().noneMatch(h -> h.id().equals("b")),
                "approx search never returns a deleted id");

        // Re-insert after delete works and respects the fixed dimension.
        svc.upsert("b", new double[]{3, 3}, Map.of("k", "y"), Metric.L2);
        SearchService.Outcome after = svc.search(new double[]{3, 3}, Metric.L2, 1,
                Filter.from(Map.of()), "exact", null, 0);
        t.check(after.hits.get(0).id().equals("b"), "a re-inserted deleted id is searchable again");
    }

    private static void testFilters(TestFramework t) {
        t.section("metadata filters");
        SearchService svc = newService();
        svc.upsert("a", new double[]{0, 0}, md("cat", "red", "n", 1.0, "flag", true), Metric.L2);
        svc.upsert("b", new double[]{0.1, 0.1}, md("cat", "red", "n", 2.0, "flag", false), Metric.L2);
        svc.upsert("c", new double[]{0.2, 0.2}, md("cat", "blue", "n", 1.0, "flag", true), Metric.L2);

        SearchService.Outcome red = svc.search(new double[]{0, 0}, Metric.L2, 10,
                Filter.from(Map.of("cat", "red")), "exact", null, 0);
        Set<String> ids = red.hits.stream().map(SearchHit::id).collect(Collectors.toSet());
        t.check(ids.equals(Set.of("a", "b")), "single-key equality filter, got " + ids);

        SearchService.Outcome both = svc.search(new double[]{0, 0}, Metric.L2, 10,
                Filter.from(Map.of("cat", "red", "n", 1.0)), "exact", null, 0);
        t.check(both.hits.size() == 1 && both.hits.get(0).id().equals("a"),
                "multiple conditions are ANDed");

        SearchService.Outcome none = svc.search(new double[]{0, 0}, Metric.L2, 10,
                Filter.from(Map.of("missing", "x")), "exact", null, 0);
        t.check(none.hits.isEmpty(), "a condition on a missing key matches nothing");

        // Filtered candidates are rejected before any distance is computed.
        t.check(red.vectorDistances == 2,
                "filter short-circuits before distance work: 2 red vectors cost 2 "
                        + "distance calculations (not 3), got " + red.vectorDistances);

        // Filters apply to approx search too.
        SearchService.Outcome approxBlue = svc.search(new double[]{0, 0}, Metric.L2, 10,
                Filter.from(Map.of("cat", "blue")), "approx", null, 100);
        Set<String> approxIds = approxBlue.hits.stream().map(SearchHit::id).collect(Collectors.toSet());
        t.check(approxIds.equals(Set.of("c")), "approx search honors filters, got " + approxIds);
    }

    private static void testBudget(TestFramework t) {
        t.section("search budget caps distance calculations");
        SearchService svc = newService();
        for (int i = 0; i < 100; i++) {
            svc.upsert("v" + i, new double[]{i, 0}, Map.of(), Metric.L2);
        }
        SearchService.Outcome out = svc.search(new double[]{0, 0}, Metric.L2, 5,
                Filter.from(Map.of()), "exact", 20L, 0);
        t.check(out.vectorDistances == 20,
                "exact search stops at budget 20, got " + out.vectorDistances);
        t.check(out.hits.size() <= 5, "at most k hits returned");

        SearchService.Outcome ap = svc.search(new double[]{50, 0}, Metric.L2, 5,
                Filter.from(Map.of()), "approx", 10L, 100);
        t.check(ap.vectorDistances <= 10,
                "approx search respects vector distance budget, got " + ap.vectorDistances);
    }

    private static void testIvfRecall(TestFramework t) {
        t.section("IVF approximate search sanity on clustered data");
        SearchService svc = newService();
        long seed = 42;
        // 3 well-separated clusters.
        double[][] centers = {{0, 0, 0, 0}, {20, 20, 20, 20}, {-20, -20, -20, -20}};
        java.util.Random rng = new java.util.Random(seed);
        int perCluster = 60;
        int id = 0;
        for (double[] c : centers) {
            for (int i = 0; i < perCluster; i++) {
                double[] v = new double[4];
                for (int d = 0; d < 4; d++) {
                    v[d] = c[d] + rng.nextGaussian();
                }
                svc.upsert("n" + (id++), v, Map.of(), Metric.L2);
            }
        }

        // Query inside cluster 0; probing all clusters must recover exact top-k.
        double[] q = {0.5, -0.5, 0.5, -0.5};
        SearchService.Outcome exact = svc.search(q, Metric.L2, 10,
                Filter.from(Map.of()), "exact", null, 0);
        SearchService.Outcome approx = svc.search(q, Metric.L2, 10,
                Filter.from(Map.of()), "approx", null, 32);
        Set<String> exactIds = exact.hits.stream().map(SearchHit::id).collect(Collectors.toSet());
        Set<String> approxIds = approx.hits.stream().map(SearchHit::id).collect(Collectors.toSet());
        approxIds.retainAll(exactIds);
        t.check(approxIds.size() == 10,
                "IVF with nprobe=nlist has perfect recall (10/10), got " + approxIds.size());
        t.check(approx.centroidDistances >= 32,
                "centroid distances are counted separately, got " + approx.centroidDistances);
        t.check(approx.vectorDistances == exact.vectorDistances,
                "probing every cluster scans every live vector exactly once (approx="
                        + approx.vectorDistances + ", exact=" + exact.vectorDistances + ")");
    }

    // ------------------------------------------------------------------

    private static SearchService newService() {
        VectorStore store = new VectorStore();
        return new SearchService(store, 32, 20, 42);
    }

    private static Map<String, Object> md(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    private static void checkClose(TestFramework t, double actual, double expected,
                                   double tol, String description) {
        if (description.isEmpty()) return;
        t.check(Math.abs(actual - expected) <= tol,
                description + " (got " + actual + ", expected " + expected + ")");
    }
}
