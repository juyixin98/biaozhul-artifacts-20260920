package com.example.vecsearch;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * Acceptance evaluation.
 *
 * Generates a FIXED-SEED synthetic dataset of Gaussian clusters plus
 * uniform outliers, then measures the approximate IVF index against the
 * exact brute-force baseline:
 *
 *   1. recall@10 and distance-calculation counts across an nprobe sweep
 *      (the search budget), for both L2 and COSINE
 *   2. recall under an explicit vector-distance budget cap
 *   3. recall for queries placed next to outliers
 *   4. a delete + filter invariant check (filtered results must never
 *      contain deleted ids)
 *
 * Run: scripts/eval.sh  (output is printed as plain text).
 */
public class EvalMain {

    // Fixed constants make every run reproducible.
    static final long SEED = 42L;
    static final int DIM = 16;
    static final int CLUSTERS = 8;
    static final int PER_CLUSTER = 150;
    static final int OUTLIERS = 60;
    static final int QUERIES = 50;
    static final int K = 10;
    static final int NLIST = 32;
    static final double CLUSTER_SPREAD = 30.0;
    static final double POINT_NOISE = 1.0;
    static final double OUTLIER_SPAN = 400.0;

    record Point(String id, double[] v, int cluster, String color) {}

    private final List<Point> points = new ArrayList<>();
    private final List<double[]> queries = new ArrayList<>();
    private final int[] queryCluster = new int[QUERIES];

    public static void main(String[] args) {
        new EvalMain().run();
    }

    private void run() {
        generate();

        System.out.println("============================================================");
        System.out.println(" Vector nearest-neighbour evaluation (fixed seed " + SEED + ")");
        System.out.println("============================================================");
        System.out.println("Dataset: " + CLUSTERS + " clusters x " + PER_CLUSTER
                + " points + " + OUTLIERS + " outliers = " + points.size() + " vectors");
        System.out.println("Dimension: " + DIM + ", nlist (IVF clusters): " + NLIST
                + ", k: " + K + ", query count: " + QUERIES);
        System.out.println();

        for (Metric metric : new Metric[]{Metric.L2, Metric.COSINE}) {
            evaluateMetric(metric);
        }
        evaluateOutlierQueries();
        evaluateDeleteFilterInvariant();
    }

    // ------------------------------------------------------------------
    // Data generation
    // ------------------------------------------------------------------

    private void generate() {
        Random rng = new Random(SEED);
        double[][] centers = new double[CLUSTERS][DIM];
        for (int c = 0; c < CLUSTERS; c++) {
            for (int d = 0; d < DIM; d++) {
                centers[c][d] = rng.nextGaussian() * CLUSTER_SPREAD;
            }
        }

        int id = 0;
        for (int c = 0; c < CLUSTERS; c++) {
            for (int i = 0; i < PER_CLUSTER; i++) {
                points.add(new Point("c" + c + "_" + i, noisy(centers[c], rng),
                        c, i % 3 == 0 ? "red" : "blue"));
                id++;
            }
        }
        for (int i = 0; i < OUTLIERS; i++) {
            double[] v = new double[DIM];
            for (int d = 0; d < DIM; d++) {
                v[d] = (rng.nextDouble() * 2 - 1) * OUTLIER_SPAN;
            }
            points.add(new Point("out_" + i, v, -1, "gold"));
        }

        // Query points: sampled near random cluster centres (independent draws
        // from the same fixed RNG stream).
        Random queryRng = new Random(SEED ^ 0x9E3779B97F4A7C15L);
        for (int q = 0; q < QUERIES; q++) {
            int c = queryRng.nextInt(CLUSTERS);
            queryCluster[q] = c;
            queries.add(noisy(centers[c], queryRng));
        }
    }

    private static double[] noisy(double[] center, Random rng) {
        double[] v = center.clone();
        for (int d = 0; d < v.length; d++) {
            v[d] += rng.nextGaussian() * POINT_NOISE;
        }
        return v;
    }

    private SearchService newLoadedService(Metric metric) {
        VectorStore store = new VectorStore();
        SearchService svc = new SearchService(store, NLIST, 20, SEED);
        for (Point p : points) {
            svc.upsert(p.id(), p.v(),
                    Map.of("cluster", p.cluster() < 0 ? "outlier" : (double) p.cluster(),
                            "color", p.color()),
                    metric);
        }
        return svc;
    }

    // ------------------------------------------------------------------
    // Metric evaluation
    // ------------------------------------------------------------------

    private void evaluateMetric(Metric metric) {
        SearchService svc = newLoadedService(metric);
        svc.rebuildIndex();

        // Ground truth from the exact baseline.
        List<Set<String>> truth = new ArrayList<>();
        long[] exactDistances = new long[QUERIES];
        for (int q = 0; q < QUERIES; q++) {
            SearchService.Outcome ex = svc.search(queries.get(q), metric, K,
                    Filter.from(Map.of()), "exact", null, 0);
            truth.add(ex.hits.stream().map(SearchHit::id).collect(Collectors.toSet()));
            exactDistances[q] = ex.vectorDistances;
        }
        long exactPerQuery = exactDistances[0];

        System.out.println("------------------------------------------------------------");
        System.out.println(" Metric: " + metric);
        System.out.println("------------------------------------------------------------");
        System.out.println("Exact baseline: " + exactPerQuery
                + " vector-distance calculations per query (scans all live vectors)");
        System.out.println();
        System.out.println("nprobe sweep (search budget = number of clusters probed):");
        System.out.printf("  %6s | %10s | %18s | %18s | %s%n",
                "nprobe", "recall@" + K, "vec dists/query", "cent dists/query", "vec dist savings");
        System.out.println("  " + "-".repeat(90));

        int[] nprobes = {1, 2, 4, 8, 16, NLIST};
        for (int nprobe : nprobes) {
            double recallSum = 0;
            long vecSum = 0;
            long centSum = 0;
            for (int q = 0; q < QUERIES; q++) {
                SearchService.Outcome ap = svc.search(queries.get(q), metric, K,
                        Filter.from(Map.of()), "approx", null, nprobe);
                Set<String> got = ap.hits.stream().map(SearchHit::id).collect(Collectors.toSet());
                got.retainAll(truth.get(q));
                recallSum += (double) got.size() / K;
                vecSum += ap.vectorDistances;
                centSum += ap.centroidDistances;
            }
            double avgVec = (double) vecSum / QUERIES;
            double avgCent = (double) centSum / QUERIES;
            double savings = 1.0 - avgVec / exactPerQuery;
            System.out.printf("  %6d | %9.1f%% | %18.1f | %18.1f | %16.1f%%%n",
                    nprobe, 100 * recallSum / QUERIES, avgVec, avgCent, 100 * savings);
        }

        System.out.println();
        System.out.println("Explicit vector-distance budget cap (nprobe=32, scan aborts at cap):");
        System.out.printf("  %8s | %10s | %18s%n",
                "budget", "recall@" + K, "vec dists/query");
        System.out.println("  " + "-".repeat(48));
        long[] budgets = {50, 100, 200, 400};
        for (long budget : budgets) {
            double recallSum = 0;
            long vecSum = 0;
            for (int q = 0; q < QUERIES; q++) {
                SearchService.Outcome ap = svc.search(queries.get(q), metric, K,
                        Filter.from(Map.of()), "approx", budget, NLIST);
                Set<String> got = ap.hits.stream().map(SearchHit::id).collect(Collectors.toSet());
                got.retainAll(truth.get(q));
                recallSum += (double) got.size() / K;
                vecSum += ap.vectorDistances;
            }
            System.out.printf("  %8d | %9.1f%% | %18.1f%n",
                    budget, 100 * recallSum / QUERIES, (double) vecSum / QUERIES);
        }
        System.out.println();
    }

    // ------------------------------------------------------------------
    // Outlier queries
    // ------------------------------------------------------------------

    private void evaluateOutlierQueries() {
        SearchService svc = newLoadedService(Metric.L2);
        svc.rebuildIndex();

        // Queries placed directly next to 20 outliers.
        Random rng = new Random(SEED ^ 0x5DEECE66DL);
        List<Point> outs = points.stream().filter(p -> p.cluster() < 0).limit(20).toList();
        double recallSum = 0;
        double exactTop1Outlier = 0;
        for (Point o : outs) {
            double[] q = noisy(o.v(), rng); // close to the outlier
            SearchService.Outcome ex = svc.search(q, Metric.L2, K,
                    Filter.from(Map.of()), "exact", null, 0);
            SearchService.Outcome ap = svc.search(q, Metric.L2, K,
                    Filter.from(Map.of()), "approx", null, 8);
            if (ex.hits.get(0).id().equals(o.id())) {
                exactTop1Outlier++;
            }
            Set<String> truth = ex.hits.stream().map(SearchHit::id).collect(Collectors.toSet());
            Set<String> got = ap.hits.stream().map(SearchHit::id).collect(Collectors.toSet());
            got.retainAll(truth);
            recallSum += (double) got.size() / K;
        }
        double frac = exactTop1Outlier / outs.size();
        System.out.println("------------------------------------------------------------");
        System.out.println(" Outlier queries (" + outs.size() + " queries next to outliers)");
        System.out.println("------------------------------------------------------------");
        System.out.printf("Exact top-1 is the seeded outlier for %.0f%% of queries%n", frac * 100);
        System.out.printf("IVF nprobe=8 recall@%d: %.1f%% (outliers are far from cluster "
                + "centroids, so this is the hard case)%n", K, 100 * recallSum / outs.size());
        System.out.println();
    }

    // ------------------------------------------------------------------
    // Delete + filter invariant
    // ------------------------------------------------------------------

    private void evaluateDeleteFilterInvariant() {
        SearchService svc = newLoadedService(Metric.L2);

        // Delete every "red" vector in cluster 0 and 1.
        List<String> deleted = points.stream()
                .filter(p -> "red".equals(p.color()) && (p.cluster() == 0 || p.cluster() == 1))
                .map(Point::id)
                .toList();
        for (String id : deleted) {
            boolean removed = svc.delete(id);
            if (!removed) {
                throw new IllegalStateException("delete returned false for " + id);
            }
        }
        Set<String> deletedSet = Set.copyOf(deleted);

        Random rng = new Random(SEED ^ 0xABCDEFL);
        int trials = 100;
        int leaksExact = 0;
        int leaksApprox = 0;
        int filterViolations = 0;
        long approxVecSum = 0;
        double recallSum = 0;
        for (int t = 0; t < trials; t++) {
            double[] q = new double[DIM];
            for (int d = 0; d < DIM; d++) {
                q[d] = rng.nextGaussian() * CLUSTER_SPREAD;
            }

            SearchService.Outcome ex = svc.search(q, Metric.L2, K,
                    Filter.from(Map.of("color", "red")), "exact", null, 0);
            SearchService.Outcome ap = svc.search(q, Metric.L2, K,
                    Filter.from(Map.of("color", "red")), "approx", null, 16);
            approxVecSum += ap.vectorDistances;

            for (SearchHit h : ex.hits) {
                if (deletedSet.contains(h.id())) leaksExact++;
                if (!"red".equals(h.metadata().get("color"))) filterViolations++;
            }
            Set<String> truth = ex.hits.stream().map(SearchHit::id).collect(Collectors.toSet());
            Set<String> got = ap.hits.stream().map(SearchHit::id).collect(Collectors.toSet());
            got.retainAll(truth);
            recallSum += (double) got.size() / Math.max(1, ex.hits.size());
            for (SearchHit h : ap.hits) {
                if (deletedSet.contains(h.id())) leaksApprox++;
                if (!"red".equals(h.metadata().get("color"))) filterViolations++;
            }
        }

        System.out.println("------------------------------------------------------------");
        System.out.println(" Delete + filter invariant (" + trials + " filtered queries)");
        System.out.println("------------------------------------------------------------");
        System.out.println("Deleted " + deleted.size()
                + " vectors (color=red in cluster 0/1); queries filter color=red");
        System.out.println("Deleted ids returned by exact search:  " + leaksExact);
        System.out.println("Deleted ids returned by approx search: " + leaksApprox);
        System.out.println("Non-red hits returned by either mode:  " + filterViolations);
        System.out.printf("Approx recall vs filtered exact set: %.1f%%, avg vec distances/query: %.1f%n",
                100 * recallSum / trials, (double) approxVecSum / trials);
        System.out.println();
        System.out.println("All checks completed.");
    }
}
