package com.example.neardup;

import org.junit.jupiter.api.Test;

import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;

class JaccardTest {

    @Test
    void identicalSetsScoreOne() {
        assertEquals(1.0, Jaccard.similarity(Set.of("a", "b"), Set.of("a", "b")));
    }

    @Test
    void disjointSetsScoreZero() {
        assertEquals(0.0, Jaccard.similarity(Set.of("a"), Set.of("b")));
    }

    @Test
    void knownOverlap() {
        assertEquals(1.0 / 3.0, Jaccard.similarity(Set.of("a", "b"), Set.of("b", "c")), 1e-12);
    }

    @Test
    void emptySetNeverMatchesEvenItself() {
        assertEquals(0.0, Jaccard.similarity(Set.of(), Set.of()));
        assertEquals(0.0, Jaccard.similarity(Set.of(), Set.of("a")));
    }
}
