package com.example.neardup;

import com.example.neardup.ClusterResult.ClusterInfo;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ClustererTest {

    private static final NearDupConfig CONFIG = NearDupConfig.defaults();
    private static ClusterResult result;

    @BeforeAll
    static void clusterSyntheticCorpus() {
        result = new Clusterer().cluster(CorpusGenerator.generate(), CONFIG);
    }

    @Test
    void corpusIsDeterministic() {
        assertEquals(CorpusGenerator.generate(), CorpusGenerator.generate());
    }

    @Test
    void topicVariantsClusterWithTheirBase() {
        for (int topic = 0; topic < 6; topic++) {
            String baseId = "topic" + topic + "-base";
            Set<String> expected = Set.of(baseId, "topic" + topic + "-v1", "topic" + topic + "-v2");
            Optional<ClusterInfo> cluster = result.clusters().stream()
                    .filter(c -> c.memberIds().contains(baseId))
                    .findFirst();
            assertTrue(cluster.isPresent(), "no cluster for topic" + topic);
            assertEquals(expected, Set.copyOf(cluster.get().memberIds()),
                    "topic" + topic + " cluster members");
        }
    }

    @Test
    void transitiveChainFormsOneClusterViaConnectivity() {
        ClusterInfo chain = result.clusters().stream()
                .filter(c -> c.memberIds().contains("chain-0"))
                .findFirst()
                .orElseThrow(() -> new AssertionError("chain cluster missing"));
        assertEquals(Set.of("chain-0", "chain-1", "chain-2", "chain-3", "chain-4"),
                Set.copyOf(chain.memberIds()));

        // The endpoints are in the same cluster even though their exact
        // similarity is far below the threshold: membership is by
        // connectivity, NOT by pairwise threshold. The report must say so.
        assertFalse(chain.allPairsAboveThreshold(),
                "chain cluster must not claim all pairs exceed the threshold");
        assertTrue(chain.minPairSimilarity() < CONFIG.threshold(),
                "chain endpoints should be below threshold, got " + chain.minPairSimilarity());

        // Only adjacent chain docs are verified edges (chain-0..chain-4
        // have ~0.66 / 0.42 / 0.24 / 0.10 similarity to each other).
        Set<Set<String>> edgePairs = chain.edges().stream()
                .map(e -> Set.of(e.sourceId(), e.targetId()))
                .collect(Collectors.toSet());
        assertTrue(edgePairs.contains(Set.of("chain-0", "chain-1")));
        assertTrue(edgePairs.contains(Set.of("chain-3", "chain-4")));
        assertFalse(edgePairs.contains(Set.of("chain-0", "chain-4")),
                "endpoints must not be a verified edge");
    }

    @Test
    void identicalShortDocumentsAreNotClustered() {
        // "hello world" == "hello world", but 2 tokens < shingle size 3,
        // so both shingle sets are empty and there is nothing to compare.
        for (ClusterInfo cluster : result.clusters()) {
            assertFalse(cluster.memberIds().contains("short-a"),
                    "short-a must not appear in any cluster");
            assertFalse(cluster.memberIds().contains("short-b"),
                    "short-b must not appear in any cluster");
        }
        assertEquals(2, result.stats().emptyShingleDocuments(),
                "short-a and short-b should have empty shingle sets");
    }

    @Test
    void distractorsStaySingletons() {
        Set<String> clustered = result.clusters().stream()
                .flatMap(c -> c.memberIds().stream())
                .collect(Collectors.toSet());
        for (int d = 0; d < 5; d++) {
            assertFalse(clustered.contains("distractor-" + d));
        }
    }

    @Test
    void statsAccountForRecallAndFalsePositives() {
        ClusterResult.ClusterStats stats = result.stats();
        assertEquals(CorpusGenerator.generate().size(), stats.documentCount());
        assertEquals(stats.verifiedPairs() + stats.falsePositiveCandidates(), stats.candidatePairs());
        assertEquals(stats.truePairs() - stats.verifiedPairs(), stats.missedTruePairs());
        assertTrue(stats.candidateRecall() >= 0.9,
                "LSH candidate recall too low: " + stats.candidateRecall());
        assertTrue(stats.candidatePrecision() > 0.0 && stats.candidatePrecision() <= 1.0);
        // The corpus is built to produce some false-positive candidates
        // (chain non-adjacent pairs, cross-topic filler overlap).
        assertTrue(stats.falsePositiveCandidates() > 0,
                "expected some false-positive candidates to demonstrate verification");
    }

    @Test
    void singletonCountMatchesUnclusteredDocs() {
        int clustered = result.clusters().stream().mapToInt(c -> c.memberIds().size()).sum();
        assertEquals(result.stats().documentCount() - clustered, result.singletonCount());
    }

    @Test
    void rejectsInvalidInput() {
        Clusterer clusterer = new Clusterer();
        assertThrows(IllegalArgumentException.class, () -> clusterer.cluster(List.of(), CONFIG));
        assertThrows(IllegalArgumentException.class, () -> clusterer.cluster(
                List.of(new Document("a", "x"), new Document("a", "y")), CONFIG));
        assertThrows(IllegalArgumentException.class, () -> clusterer.cluster(
                List.of(new Document("a", null)), CONFIG));
        assertThrows(IllegalArgumentException.class, () -> clusterer.cluster(
                List.of(new Document(" ", "text")), CONFIG));
    }

    @Test
    void rejectsInvalidConfig() {
        assertThrows(IllegalArgumentException.class,
                () -> new NearDupConfig(3, 100, 7, 0.5, 42L)); // 100 % 7 != 0
        assertThrows(IllegalArgumentException.class,
                () -> NearDupConfig.defaults().withThreshold(0.0));
        assertThrows(IllegalArgumentException.class,
                () -> NearDupConfig.defaults().withThreshold(1.5));
    }

    @Test
    void resultIsSerializableShape() {
        assertNotNull(result.config());
        assertNotNull(result.clusters());
        assertNotNull(result.stats());
    }
}
