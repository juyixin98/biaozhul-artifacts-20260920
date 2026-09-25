package invidx.engine;

import invidx.search.Hit;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

class MergeTest {

    private IndexConfig noBackground() {
        return new IndexConfig(2, false, java.time.Duration.ofMinutes(10), 3);
    }

    @Test
    void mergeDropsTombstonesAndOldGenerations(@TempDir Path tmp) throws Exception {
        try (InvertedIndex index = InvertedIndex.open(tmp, noBackground(), CrashHook.NOOP)) {
            index.putDocument(1, "apple apple red");
            index.putDocument(2, "banana yellow");
            index.flush();
            index.putDocument(1, "apple green");     // update same id
            index.putDocument(3, "cherry dark");
            index.flush();
            index.putDocument(2, "banana ripe");      // update
            index.putDocument(4, "date sweet");
            index.flush();

            // 3 segments now; merge all.
            assertEquals(3, index.stats().publishedSegments());
            assertEquals(3, index.forceMerge());
            IndexStats stats = index.stats();
            assertEquals(1, stats.publishedSegments());

            // Query results across gen/segment boundaries.
            assertEquals(List.of(1), ids(index.search("apple")));
            List<Hit> apple = index.search("apple");
            assertEquals(2L, apple.get(0).gen());
            assertEquals("apple green", apple.get(0).text());
            assertNull(index.get(99));
            assertTrue(index.search("red").isEmpty(), "old gen text reclaimed");
            assertTrue(index.search("yellow").isEmpty());
            assertEquals("banana ripe", index.get(2).text());
            assertEquals(4, index.search("date").get(0).id());
        }
    }

    private List<Integer> ids(List<Hit> hits) {
        return hits.stream().map(Hit::id).toList();
    }

    @Test
    void mergingFullyDeletedSegmentYieldsEmptyButValidSegment(@TempDir Path tmp) throws Exception {
        try (InvertedIndex index = InvertedIndex.open(tmp, noBackground(), CrashHook.NOOP)) {
            index.putDocument(1, "remove me");
            index.putDocument(2, "remove me too");
            index.flush();
            index.putDocument(3, "keep this one");
            index.putDocument(4, "also keep");
            index.flush();
            index.deleteDocument(1);
            index.deleteDocument(2);

            index.forceMerge();
            assertEquals(1, index.stats().publishedSegments());
            assertTrue(index.search("remove").isEmpty());
            assertEquals(2, index.search("keep").size());

            // Restart: empty merged segment reloads fine.
        }
        try (InvertedIndex reopened = InvertedIndex.open(tmp, noBackground(), CrashHook.NOOP)) {
            assertTrue(reopened.search("remove").isEmpty());
            assertEquals(2, reopened.search("keep").size());
        }
    }

    @Test
    void backgroundMergeEventuallyConsolidates(@TempDir Path tmp) throws Exception {
        IndexConfig cfg = new IndexConfig(2, true, java.time.Duration.ofMillis(30), 3);
        try (InvertedIndex index = InvertedIndex.open(tmp, cfg, CrashHook.NOOP)) {
            for (int i = 0; i < 12; i++) {
                index.putDocument(i, "term" + (i % 4) + " unique" + i);
                Thread.sleep(20);
            }
            long segments = 0;
            for (int i = 0; i < 50; i++) {
                segments = index.stats().publishedSegments();
                if (segments <= 2) {
                    break;
                }
                Thread.sleep(40);
            }
            assertTrue(segments <= 3, "background merge should consolidate, segments=" + segments);
            assertEquals(12, index.stats().liveDocCount());
            for (int i = 0; i < 4; i++) {
                assertEquals(3, index.search("term" + i).size(), "term" + i);
            }
        }
    }
}
