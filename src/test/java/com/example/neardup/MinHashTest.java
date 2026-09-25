package com.example.neardup;

import org.junit.jupiter.api.Test;

import java.util.HashSet;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class MinHashTest {

    private static Set<String> shinglesOf(String... tokens) {
        return Shingler.shingles(java.util.List.of(tokens), 2);
    }

    @Test
    void signatureIsDeterministicForFixedSeed() {
        MinHash minHash = new MinHash(128, NearDupConfig.DEFAULT_SEED);
        Set<String> shingles = shinglesOf("the", "quick", "brown", "fox");
        assertArrayEquals(minHash.signature(shingles), minHash.signature(shingles));
    }

    @Test
    void estimateTracksJaccardForSimilarSets() {
        MinHash minHash = new MinHash(256, NearDupConfig.DEFAULT_SEED);
        Set<String> a = new HashSet<>();
        Set<String> b = new HashSet<>();
        for (int i = 0; i < 200; i++) {
            a.add("shared" + i);
            b.add("shared" + i);
        }
        for (int i = 0; i < 40; i++) {
            a.add("onlyA" + i);
            b.add("onlyB" + i);
        }
        double exact = Jaccard.similarity(a, b); // 200/280 ≈ 0.714
        double estimate = MinHash.estimatedSimilarity(minHash.signature(a), minHash.signature(b));
        assertEquals(exact, estimate, 0.1);
    }

    @Test
    void disjointSetsHaveLowEstimate() {
        MinHash minHash = new MinHash(256, NearDupConfig.DEFAULT_SEED);
        Set<String> a = new HashSet<>();
        Set<String> b = new HashSet<>();
        for (int i = 0; i < 100; i++) {
            a.add("alpha" + i);
            b.add("omega" + i);
        }
        assertTrue(MinHash.estimatedSimilarity(minHash.signature(a), minHash.signature(b)) < 0.1);
    }
}
