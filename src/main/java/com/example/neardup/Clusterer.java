package com.example.neardup;

import com.example.neardup.ClusterResult.ClusterInfo;
import com.example.neardup.ClusterResult.ClusterStats;
import com.example.neardup.ClusterResult.EdgeInfo;
import com.example.neardup.LshIndex.DocPair;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Near-duplicate clustering pipeline:
 *
 * <pre>
 *   text -> tokens -> k-shingles -> MinHash signature -> LSH candidates
 *        -> exact Jaccard verification (>= threshold) -> union-find
 *        -> connected components = clusters
 * </pre>
 *
 * <p>Cluster semantics are CONNECTED COMPONENTS over verified edges, not
 * cliques: membership does not imply every pair in the cluster exceeds the
 * threshold. Ground-truth stats are computed by brute-force all-pairs
 * Jaccard, which is feasible for the corpus sizes this service targets
 * (see {@link #MAX_DOCUMENTS}).
 */
public final class Clusterer {

    public static final int MAX_DOCUMENTS = 2000;

    public ClusterResult cluster(List<Document> documents, NearDupConfig config) {
        validate(documents);

        List<Set<String>> shingleSets = new ArrayList<>(documents.size());
        for (Document doc : documents) {
            shingleSets.add(Shingler.shingles(Shingler.tokenize(doc.text()), config.shingleSize()));
        }
        int emptyShingleDocs = (int) shingleSets.stream().filter(Set::isEmpty).count();

        MinHash minHash = new MinHash(config.numHashes(), config.seed());
        List<long[]> signatures = shingleSets.stream().map(minHash::signature).toList();

        // Candidate recall (approximate).
        Set<DocPair> candidates = new LshIndex(config.numHashes(), config.bands())
                .candidatePairs(signatures);

        // Exact verification of every candidate.
        List<EdgeInfo> edges = new ArrayList<>();
        for (DocPair pair : candidates) {
            double sim = Jaccard.similarity(shingleSets.get(pair.a()), shingleSets.get(pair.b()));
            if (sim >= config.threshold()) {
                edges.add(new EdgeInfo(documents.get(pair.a()).id(), documents.get(pair.b()).id(),
                        round(sim)));
            }
        }

        // Ground truth by brute force: every pair above threshold, whether
        // or not LSH recalled it. Drives the recall / false-negative stats.
        int truePairs = 0;
        Set<DocPair> truePairSet = new LinkedHashSet<>();
        for (int i = 0; i < documents.size(); i++) {
            for (int j = i + 1; j < documents.size(); j++) {
                if (Jaccard.similarity(shingleSets.get(i), shingleSets.get(j)) >= config.threshold()) {
                    truePairs++;
                    truePairSet.add(new DocPair(i, j));
                }
            }
        }
        Set<DocPair> verifiedPairSet = new LinkedHashSet<>();
        for (DocPair pair : candidates) {
            if (truePairSet.contains(pair)) {
                verifiedPairSet.add(pair);
            }
        }
        int missedTruePairs = truePairs - verifiedPairSet.size();

        // Connected components over verified edges.
        UnionFind uf = new UnionFind(documents.size());
        Map<String, Integer> indexById = new HashMap<>();
        for (int i = 0; i < documents.size(); i++) {
            indexById.put(documents.get(i).id(), i);
        }
        for (EdgeInfo edge : edges) {
            uf.union(indexById.get(edge.sourceId()), indexById.get(edge.targetId()));
        }

        List<ClusterInfo> clusters = buildClusters(documents, shingleSets, edges, uf, config);

        int candidatePairs = candidates.size();
        int verifiedPairs = edges.size();
        ClusterStats stats = new ClusterStats(
                documents.size(),
                emptyShingleDocs,
                candidatePairs,
                verifiedPairs,
                candidatePairs - verifiedPairs,
                truePairs,
                missedTruePairs,
                truePairs == 0 ? 1.0 : (double) verifiedPairs / truePairs,
                candidatePairs == 0 ? 1.0 : (double) verifiedPairs / candidatePairs);
        return new ClusterResult(clusters, documents.size() - clusters.stream()
                .mapToInt(c -> c.memberIds().size()).sum(), stats, config);
    }

    private List<ClusterInfo> buildClusters(List<Document> documents,
                                            List<Set<String>> shingleSets,
                                            List<EdgeInfo> edges,
                                            UnionFind uf,
                                            NearDupConfig config) {
        Map<Integer, List<Integer>> components = uf.components();
        Map<String, Integer> indexById = new HashMap<>();
        for (int i = 0; i < documents.size(); i++) {
            indexById.put(documents.get(i).id(), i);
        }

        List<ClusterInfo> clusters = new ArrayList<>();
        int clusterId = 0;
        for (List<Integer> members : components.values()) {
            if (members.size() < 2) {
                continue; // singleton: no verified edge, not a near-dup cluster
            }
            List<String> memberIds = members.stream().map(i -> documents.get(i).id()).sorted().toList();
            List<EdgeInfo> clusterEdges = edges.stream()
                    .filter(e -> memberIds.contains(e.sourceId()) && memberIds.contains(e.targetId()))
                    .toList();

            // Exact similarity for EVERY intra-cluster pair, so the report
            // shows the real range instead of implying all pairs >= threshold.
            double min = 1.0;
            double max = 0.0;
            boolean allAbove = true;
            for (int i = 0; i < members.size(); i++) {
                for (int j = i + 1; j < members.size(); j++) {
                    double sim = Jaccard.similarity(shingleSets.get(members.get(i)),
                            shingleSets.get(members.get(j)));
                    min = Math.min(min, sim);
                    max = Math.max(max, sim);
                    if (sim < config.threshold()) {
                        allAbove = false;
                    }
                }
            }
            clusters.add(new ClusterInfo(clusterId++, memberIds, clusterEdges,
                    round(min), round(max), allAbove));
        }
        return clusters;
    }

    private static void validate(List<Document> documents) {
        if (documents == null || documents.isEmpty()) {
            throw new IllegalArgumentException("documents must be a non-empty list");
        }
        if (documents.size() > MAX_DOCUMENTS) {
            throw new IllegalArgumentException(
                    "too many documents: " + documents.size() + " > " + MAX_DOCUMENTS);
        }
        Set<String> ids = new LinkedHashSet<>();
        for (Document doc : documents) {
            if (doc == null || doc.id() == null || doc.id().isBlank()) {
                throw new IllegalArgumentException("every document needs a non-blank id");
            }
            if (doc.text() == null) {
                throw new IllegalArgumentException("document " + doc.id() + " has null text");
            }
            if (!ids.add(doc.id())) {
                throw new IllegalArgumentException("duplicate document id: " + doc.id());
            }
        }
    }

    private static double round(double value) {
        return Math.round(value * 10_000.0) / 10_000.0;
    }
}
