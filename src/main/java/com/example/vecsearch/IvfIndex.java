package com.example.vecsearch;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * Self-implemented approximate index: IVF (inverted file index).
 *
 * Training uses fixed-seed k-means in the ambient vector space:
 *   - centroids are initialised with a seeded sample of live vectors
 *   - assignment uses squared L2 (works for both supported metrics:
 *     the cosine clusters produced are coarser but effective, and probe
 *     ranking at query time uses the actual requested metric)
 *   - empty clusters are reseeded from a random live vector
 *
 * Query time: rank the nprobe closest centroids under the search metric,
 * scan only the vectors assigned to those clusters. Distance calculations
 * to centroids and to vectors are counted separately.
 *
 * The index is immutable once built; mutations mark the owning service's
 * index dirty so it is rebuilt lazily on the next approximate search.
 */
public final class IvfIndex {

    private final Metric metric;
    private final double[][] centroids;
    private final List<List<VectorStore.Entry>> postings;

    private IvfIndex(Metric metric,
                     double[][] centroids,
                     List<List<VectorStore.Entry>> postings) {
        this.metric = metric;
        this.centroids = centroids;
        this.postings = postings;
    }

    public int clusterCount() {
        return centroids.length;
    }

    public Metric metric() {
        return metric;
    }

    /**
     * Train an index over the given live vectors.
     *
     * @param numClusters desired nlist (clamped to the number of vectors)
     * @param iterations  k-means iterations
     * @param seed        RNG seed (fixed → reproducible indices)
     */
    public static IvfIndex build(List<VectorStore.Entry> entries, Metric metric,
                                 int numClusters, int iterations, long seed) {
        int n = entries.size();
        int k = Math.max(1, Math.min(numClusters, n));
        int dim = entries.get(0).vector.length;
        Random rng = new Random(seed);

        // --- Seeded centroid initialisation: shuffled sample of vectors ---
        List<Integer> order = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            order.add(i);
        }
        java.util.Collections.shuffle(order, rng);
        double[][] centroids = new double[k][];
        for (int c = 0; c < k; c++) {
            centroids[c] = entries.get(order.get(c)).vector.clone();
        }

        int[] assignment = new int[n];

        for (int iter = 0; iter < iterations; iter++) {
            // Assignment step (L2 in vector space).
            for (int i = 0; i < n; i++) {
                double[] v = entries.get(i).vector;
                int best = 0;
                double bestDist = Double.POSITIVE_INFINITY;
                for (int c = 0; c < k; c++) {
                    double d = squaredL2(v, centroids[c]);
                    if (d < bestDist) {
                        bestDist = d;
                        best = c;
                    }
                }
                assignment[i] = best;
            }

            // Update step: centroid = mean of members.
            double[][] sums = new double[k][dim];
            int[] counts = new int[k];
            for (int i = 0; i < n; i++) {
                int c = assignment[i];
                counts[c]++;
                double[] v = entries.get(i).vector;
                for (int d = 0; d < dim; d++) {
                    sums[c][d] += v[d];
                }
            }
            for (int c = 0; c < k; c++) {
                if (counts[c] > 0) {
                    for (int d = 0; d < dim; d++) {
                        centroids[c][d] = sums[c][d] / counts[c];
                    }
                } else {
                    // Reseed empty cluster with a random live vector.
                    centroids[c] = entries.get(rng.nextInt(n)).vector.clone();
                }
            }
        }

        // Final assignment after the last centroid update.
        for (int i = 0; i < n; i++) {
            double[] v = entries.get(i).vector;
            int best = 0;
            double bestDist = Double.POSITIVE_INFINITY;
            for (int c = 0; c < k; c++) {
                double d = squaredL2(v, centroids[c]);
                if (d < bestDist) {
                    bestDist = d;
                    best = c;
                }
            }
            assignment[i] = best;
        }

        List<List<VectorStore.Entry>> postings = new ArrayList<>(k);
        for (int c = 0; c < k; c++) {
            postings.add(new ArrayList<>());
        }
        for (int i = 0; i < n; i++) {
            postings.get(assignment[i]).add(entries.get(i));
        }
        return new IvfIndex(metric, centroids, postings);
    }

    /**
     * Rank clusters by distance from the query under the search metric.
     * Returns cluster indices in ascending distance order.
     */
    public int[] rankClusters(double[] query, double queryNorm, Distance.Counter counter) {
        Integer[] order = new Integer[centroids.length];
        double[] dists = new double[centroids.length];
        for (int c = 0; c < centroids.length; c++) {
            order[c] = c;
            double cnorm = Distance.norm(centroids[c]);
            if (metric == Metric.L2 || cnorm == 0.0) {
                dists[c] = Distance.centroidL2(query, centroids[c], counter);
            } else {
                double d = Distance.cosine(query, centroids[c], queryNorm, cnorm, null);
                if (counter != null) {
                    counter.countCentroid();
                }
                dists[c] = d;
            }
        }
        java.util.Arrays.sort(order, (a, b) -> Double.compare(dists[a], dists[b]));
        int[] result = new int[order.length];
        for (int i = 0; i < order.length; i++) {
            result[i] = order[i];
        }
        return result;
    }

    public List<VectorStore.Entry> cluster(int c) {
        return postings.get(c);
    }

    private static double squaredL2(double[] a, double[] b) {
        double sum = 0.0;
        for (int i = 0; i < a.length; i++) {
            double d = a[i] - b[i];
            sum += d * d;
        }
        return sum;
    }
}
