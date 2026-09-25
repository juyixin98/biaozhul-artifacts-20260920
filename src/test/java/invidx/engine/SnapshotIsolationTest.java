package invidx.engine;

import invidx.search.Hit;
import invidx.search.Searcher;
import invidx.search.Snapshot;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * A snapshot acquired before a mutation must keep answering as of its
 * acquisition point, even after updates, deletes, flushes and merges
 * replace all on-disk segments.
 */
class SnapshotIsolationTest {

    private IndexConfig cfg() {
        return new IndexConfig(2, false, java.time.Duration.ofMinutes(10), 3);
    }

    @Test
    void snapshotStaysStableWhileIndexMutates(@TempDir Path tmp) throws Exception {
        try (InvertedIndex index = InvertedIndex.open(tmp, cfg(), CrashHook.NOOP)) {
            index.putDocument(1, "red apple sweet");
            index.putDocument(2, "green apple tart");
            index.flush();
            index.putDocument(3, "yellow banana soft");
            index.putDocument(4, "red berry tart");
            index.flush();

            Snapshot before = index.snapshot();
            Searcher beforeSearcher = new Searcher(before);
            int beforeVersion = (int) before.manifestVersion();
            List<Hit> beforeHits = beforeSearcher.search("apple");
            assertEquals(2, beforeHits.size());

            // Heavy mutation: update, delete, flush, merge.
            index.putDocument(1, "red plum sweet");
            index.deleteDocument(2);
            index.putDocument(5, "green apple newtree");
            index.flush();
            index.forceMerge();

            // Live view changed: doc 1 lost "apple" via update, doc 2 deleted,
            // doc 5 gained it — only doc 5 matches "apple" now.
            List<Hit> liveHits = index.search("apple");
            assertNotEquals(beforeHits.size(), liveHits.size());
            assertEquals(1, liveHits.size());
            assertEquals(5, liveHits.get(0).id());
            assertTrue(index.search("plum").stream().anyMatch(h -> h.id() == 1));

            // Old snapshot is frozen at the old point in time.
            List<Hit> frozenHits = beforeSearcher.search("apple");
            assertEquals(2, frozenHits.size());
            assertEquals("red apple sweet", frozenHits.get(0).text());
            assertEquals(beforeVersion, (int) before.manifestVersion());
            assertEquals(4, before.docCount());

            // And a term query only old revisions had.
            assertEquals(1, beforeSearcher.search("banana").size());
        }
    }
}
