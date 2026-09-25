package com.example.neardup;

import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import static com.example.neardup.Shingler.SHINGLE_SEPARATOR;

class ShinglerTest {

    @Test
    void tokenizesLowercaseAndSplitsOnNonAlnum() {
        assertEquals(List.of("hello", "world", "a1", "b2"),
                Shingler.tokenize("Hello, WORLD! a1...b2"));
    }

    @Test
    void buildsWordKShingles() {
        Set<String> shingles = Shingler.shingles(List.of("a", "b", "c", "d"), 3);
        assertEquals(Set.of("a" + SHINGLE_SEPARATOR + "b" + SHINGLE_SEPARATOR + "c", "b" + SHINGLE_SEPARATOR + "c" + SHINGLE_SEPARATOR + "d"), shingles);
    }

    @Test
    void documentShorterThanShingleSizeYieldsEmptySet() {
        assertTrue(Shingler.shingles(List.of("hello", "world"), 3).isEmpty());
    }

    @Test
    void duplicateWindowsCollapseToSet() {
        // windows: xyz, yzx, zxy, xyz -> 3 distinct shingles
        Set<String> shingles = Shingler.shingles(List.of("x", "y", "z", "x", "y", "z"), 3);
        assertEquals(3, shingles.size());
    }

    @Test
    void rejectsInvalidShingleSize() {
        assertThrows(IllegalArgumentException.class,
                () -> Shingler.shingles(List.of("a"), 0));
    }
}
