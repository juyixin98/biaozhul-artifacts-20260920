package neardup.core;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Full near-duplicate pipeline with explicit connected-component semantics:
 *
 *   text --Shingler(k=3)--> shingle sets
 *        --MinHash(120, fixed seed)--> signatures
 *        --LSH(60 bands x 2 rows)--> candidate pairs (recall stage)
 *        --EXACT Jaccard re-check at threshold--> accepted edges
 *        --union-find--> connected components = clusters
 *
 * A cluster is therefore EXACTLY a connected component of the graph whose
 * vertices are documents and whose edges are pairs with exact Jaccard >=
 * threshold. Documents connected by a transitive chain are placed in one
 * cluster even when their pairwise Jaccard is below the threshold; the
 * transitivePairs list makes those below-threshold non-adjacent pairs explicit,
 * so the result is never mis-described as "every pair in the cluster exceeds
 * the threshold".
 *
 * The candidate stage's recall and false-positive rate are measured against a
 * brute-force O(n^2) exact-Jaccard ground truth.
 */
public final class Clusterer {

    /** A candidate emitted by LSH, annotated after exact verification. */
    public record Candidate(int i, int j, double exactJaccard, double minhashEstimate,
                            int bandHits, boolean accepted) {}

    /** An accepted near-duplicate edge (exact Jaccard >= threshold). */
    public record Edge(int i, int j, double exactJaccard, double minhashEstimate, int bandHits) {}

    /** A pair inside one cluster whose exact Jaccard is below the threshold
     *  (reachable only transitively, or an extra edge with lower similarity). */
    public record TransitivePair(int i, int j, double exactJaccard) {}

    public record Cluster(List<Integer> members, List<Edge> internalEdges,
                          List<TransitivePair> belowThresholdPairs) {}

    public record Stats(
            int documents,
            int emptyShingleDocuments,
            long bruteForcePairs,
            int trueEdges,
            int candidatePairs,
            int candidatesAccepted,
            int candidateFalsePositives,
            int trueEdgesRecalled,
            int trueEdgesMissed,
            double candidateRecall,
            double candidatePrecision,
            double falsePositiveShareOfCandidates) {}

    public record Result(List<Cluster> clusters,
                         List<Cluster> singletonClusters,
                         List<Edge> edges,
                         List<Candidate> candidates,
                         List<int[]> missedEdges,
                         Stats stats,
                         int thresholdNumerator,
                         int thresholdDenominator) {}

    private final Shingler shingler;
    private final MinHash minHash;
    private final Lsh lsh;
    private final double threshold;

    public Clusterer() {
        this(0.6);
    }

    public Clusterer(double threshold) {
        this.shingler = new Shingler();
        this.minHash = new MinHash();
        this.lsh = new Lsh();
        if (threshold <= 0.0 || threshold > 1.0) {
            throw new IllegalArgumentException("threshold must be in (0,1]");
        }
        this.threshold = threshold;
    }

    public double threshold() {
        return threshold;
    }

    public Result cluster(List<String> texts) {
        int n = texts.size();

        // 1. Shingles
        List<Set<Long>> sets = new ArrayList<>(n);
        int emptyDocs = 0;
        for (String t : texts) {
            Set<Long> s = shingler.shingle(t);
            if (s.isEmpty()) {
                emptyDocs++;
            }
            sets.add(s);
        }

        // 2. MinHash signatures
        long[][] sigs = new long[n][];
        for (int i = 0; i < n; i++) {
            sigs[i] = minHash.signature(sets.get(i));
        }

        // 3. LSH candidate recall
        List<Lsh.CandidatePair> raw = lsh.candidates(sigs);

        // 4. Exact Jaccard verification of every candidate + brute-force truth
        List<Candidate> candidates = new ArrayList<>();
        boolean[][] candidateFlag = new boolean[n][n];
        boolean[][] edgeFlag = new boolean[n][n];
        List<Edge> acceptedEdges = new ArrayList<>();

        UnionFind uf = new UnionFind(n);

        for (Lsh.CandidatePair cp : raw) {
            int i = cp.i();
            int j = cp.j();
            candidateFlag[i][j] = true;
            double exact = Jaccard.similarity(sets.get(i), sets.get(j));
            double est = MinHash.estimatedSimilarity(sigs[i], sigs[j]);
            boolean accepted = exact >= threshold;
            candidates.add(new Candidate(i, j, exact, est, cp.bandHits(), accepted));
            if (accepted) {
                edgeFlag[i][j] = true;
                acceptedEdges.add(new Edge(i, j, exact, est, cp.bandHits()));
                uf.union(i, j);
            }
        }

        // Brute-force ground truth over ALL pairs (including empty-shingle docs,
        // which have exact Jaccard 0 and never form edges).
        int trueEdges = 0;
        int recalled = 0;
        List<int[]> missed = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            for (int j = i + 1; j < n; j++) {
                double exact = Jaccard.similarity(sets.get(i), sets.get(j));
                if (exact >= threshold) {
                    trueEdges++;
                    if (candidateFlag[i][j]) {
                        recalled++;
                    } else {
                        missed.add(new int[] {i, j});
                    }
                }
            }
        }

        long brutePairs = (long) n * (n - 1) / 2;
        int candCount = raw.size();
        int accepted = acceptedEdges.size();
        int fp = candCount - accepted;
        Stats stats = new Stats(
                n,
                emptyDocs,
                brutePairs,
                trueEdges,
                candCount,
                accepted,
                fp,
                recalled,
                trueEdges - recalled,
                trueEdges == 0 ? 1.0 : (double) recalled / trueEdges,
                candCount == 0 ? 1.0 : (double) accepted / candCount,
                candCount == 0 ? 0.0 : (double) fp / candCount);

        // 5. Materialize connected components
        Map<Integer, List<Integer>> comps = new LinkedHashMap<>();
        for (int i = 0; i < n; i++) {
            int root = uf.find(i);
            comps.computeIfAbsent(root, k -> new ArrayList<>()).add(i);
        }

        List<Cluster> clusters = new ArrayList<>();
        List<Cluster> singletons = new ArrayList<>();
        for (List<Integer> members : comps.values()) {
            List<Edge> internal = new ArrayList<>();
            List<TransitivePair> below = new ArrayList<>();
            for (int a = 0; a < members.size(); a++) {
                for (int b = a + 1; b < members.size(); b++) {
                    int x = members.get(a);
                    int y = members.get(b);
                    double exact = Jaccard.similarity(sets.get(x), sets.get(y));
                    int lo = Math.min(x, y);
                    int hi = Math.max(x, y);
                    if (edgeFlag[lo][hi]) {
                        long[] sx = sigs[x];
                        long[] sy = sigs[y];
                        internal.add(new Edge(lo, hi, exact,
                                MinHash.estimatedSimilarity(sx, sy), bandHits(candidates, lo, hi)));
                    } else {
                        // Same component, but no direct threshold edge: this is
                        // exactly the transitive-chain evidence.
                        below.add(new TransitivePair(lo, hi, exact));
                    }
                }
            }
            Cluster c = new Cluster(members, internal, below);
            (members.size() == 1 ? singletons : clusters).add(c);
        }

        clusters.sort((a, b) -> Integer.compare(b.members().size(), a.members().size()));
        singletons.sort((a, b) -> Integer.compare(a.members().get(0), b.members().get(0)));

        return new Result(clusters, singletons, acceptedEdges, candidates, missed, stats, 0, 0);
    }

    private static int bandHits(List<Candidate> cands, int i, int j) {
        for (Candidate c : cands) {
            if (c.i() == i && c.j() == j) {
                return c.bandHits();
            }
        }
        return 0;
    }
}
