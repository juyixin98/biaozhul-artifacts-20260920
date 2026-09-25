package invidx.engine;

import invidx.model.DocKey;
import invidx.search.Hit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class GenerationTest {

    private IndexConfig noMerge() {
        return new IndexConfig(2, false, java.time.Duration.ofMinutes(10), 3);
    }

    @Test
    void sameIdUpdatesAdvanceGenerationAndReplacePostings(@TempDir Path tmp) throws Exception {
        try (InvertedIndex index = InvertedIndex.open(tmp, noMerge(), CrashHook.NOOP)) {
            DocKey g1 = index.putDocument(5, "alpha bravo");
            index.flush();
            DocKey g2 = index.putDocument(5, "alpha charlie");
            index.flush();
            DocKey g3 = index.putDocument(5, "alpha delta echo");
            // g3 still in RAM; exercise both flushed and buffered visibility.

            assertEquals(1L, g1.gen());
            assertEquals(2L, g2.gen());
            assertEquals(3L, g3.gen());

            // Only the newest revision is live, even spanning RAM + segments.
            assertEquals(3L, index.get(5).gen());
            assertEquals("alpha delta echo", index.get(5).text());

            // Old text must not leak: "bravo" was only in gen 1, "charlie" only gen 2.
            assertTrue(index.search("bravo").isEmpty());
            assertTrue(index.search("charlie").isEmpty());
            List<Hit> alpha = index.search("alpha");
            assertEquals(1, alpha.size());
            assertEquals(3L, alpha.get(0).gen());
            assertEquals(1, index.search("delta").size());
        }
    }

    @Test
    void deletedIdCanBeReusedWithHigherGeneration(@TempDir Path tmp) throws Exception {
        try (InvertedIndex index = InvertedIndex.open(tmp, noMerge(), CrashHook.NOOP)) {
            index.putDocument(9, "river stone");
            index.flush();
            assertTrue(index.deleteDocument(9));
            assertNull(index.get(9));
            assertTrue(index.search("river").isEmpty());
            assertFalse(index.deleteDocument(9));

            DocKey reborn = index.putDocument(9, "river canyon");
            index.flush();
            assertEquals(2L, reborn.gen());
            List<Hit> hits = index.search("river");
            assertEquals(1, hits.size());
            assertEquals(2L, hits.get(0).gen());
            assertEquals("river canyon", hits.get(0).text());
            assertTrue(index.search("stone").isEmpty());
        }
    }

    @Test
    void bufferedRevisionReplacedBeforeFlushNeverSurfaces(@TempDir Path tmp) throws Exception {
        try (InvertedIndex index = InvertedIndex.open(tmp,
                new IndexConfig(10, false, java.time.Duration.ofMinutes(10), 3),
                CrashHook.NOOP)) {
            index.putDocument(1, "secret word");
            DocKey replaced = index.putDocument(1, "public word");
            index.flush();

            assertEquals(2L, replaced.gen());
            assertTrue(index.search("secret").isEmpty());
            assertEquals(1, index.search("public").size());
            assertEquals(1, index.stats().publishedSegments());
        }
    }

    @Test
    void generationSurvivesRestart(@TempDir Path tmp) throws Exception {
        try (InvertedIndex index = InvertedIndex.open(tmp, noMerge(), CrashHook.NOOP)) {
            index.putDocument(3, "one two");
            index.flush();
            index.putDocument(3, "three four");
            index.flush();
            index.deleteDocument(3);
        }
        try (InvertedIndex reopened = InvertedIndex.open(tmp, noMerge(), CrashHook.NOOP)) {
            assertNull(reopened.get(3));
            DocKey next = reopened.putDocument(3, "five six");
            assertEquals(3L, next.gen(), "generation must advance past both kill log and data");
            reopened.flush();
            assertEquals(3L, reopened.get(3).gen());
        }
    }
}
