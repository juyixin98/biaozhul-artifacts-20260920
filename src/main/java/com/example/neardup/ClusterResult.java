package com.example.neardup;

import java.util.List;

/** Result of one clustering run. */
public record ClusterResult(
        List<ClusterInfo> clusters,
        int singletonCount,
        ClusterStats stats,
        NearDupConfig config) {

    /** A verified near-duplicate edge: exact Jaccard >= threshold. */
    public record EdgeInfo(String sourceId, String targetId, double similarity) {
    }

    /**
     * One connected component of verified edges.
     *
     * <p>Membership is by CONNECTIVITY, not by pairwise similarity: an edge
     * chain A-B, B-C puts A and C in one cluster even when Jaccard(A, C) is
     * below the threshold. {@code allPairsAboveThreshold} makes this
     * explicit, and {@code minPairSimilarity}/{@code maxPairSimilarity}
     * report the exact range over all intra-cluster pairs.
     */
    public record ClusterInfo(
            int clusterId,
            List<String> memberIds,
            List<EdgeInfo> edges,
            double minPairSimilarity,
            double maxPairSimilarity,
            boolean allPairsAboveThreshold) {
    }

    /**
     * Candidate-recall and false-positive accounting.
     *
     * @param candidatePairs         pairs emitted by LSH banding
     * @param verifiedPairs          candidates with exact Jaccard >= threshold (true positives)
     * @param falsePositiveCandidates candidates with exact Jaccard < threshold
     * @param truePairs              ground truth: ALL pairs with exact Jaccard >= threshold (brute force)
     * @param missedTruePairs        true pairs LSH failed to recall (false negatives)
     * @param candidateRecall        verifiedPairs / truePairs (1.0 when truePairs == 0)
     * @param candidatePrecision     verifiedPairs / candidatePairs (1.0 when candidatePairs == 0)
     */
    public record ClusterStats(
            int documentCount,
            int emptyShingleDocuments,
            int candidatePairs,
            int verifiedPairs,
            int falsePositiveCandidates,
            int truePairs,
            int missedTruePairs,
            double candidateRecall,
            double candidatePrecision) {
    }
}
