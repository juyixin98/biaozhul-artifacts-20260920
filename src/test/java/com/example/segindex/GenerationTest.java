package com.example.segindex;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Acceptance test 2: document id reuse. Every re-add of the same id gets a
 * higher generation; only the newest live generation is ever visible, across
 * commits, deletes, re-adds, merges and restarts.
 */
class GenerationTest {

    @TempDir
    Path dir;

    @Test
    void sameIdUpdateDeleteAndReaddAcrossGenerations() {
        IndexConfig config = IndexConfig.builder().backgroundMergeEnabled(false).build();
        Index index = Index.open(dir, config);

        // generation 1
        assertEquals(1, index.addDocument("a", "apple pie"));
        index.commit();
        assertEquals(List.of("apple pie"), texts(index, "apple"));

        // generation 2 replaces generation 1
        assertEquals(2, index.addDocument("a", "apple crumble"));
        index.commit();
        List<IndexReader.Hit> hits = index.newReader().search("apple", 10);
        assertEquals(1, hits.size());
        assertEquals(2, hits.get(0).generation());
        assertEquals("apple crumble", hits.get(0).text());

        // delete removes all generations <= 2
        assertTrue(index.deleteDocument("a"));
        index.commit();
        assertTrue(index.newReader().search("apple", 10).isEmpty());

        // re-add gets generation 3 and is visible despite the tombstone
        assertEquals(3, index.addDocument("a", "apple tart"));
        index.commit();
        hits = index.newReader().search("apple", 10);
        assertEquals(1, hits.size());
        assertEquals(3, hits.get(0).generation());
        assertEquals("apple tart", hits.get(0).text());

        // merge must not resurrect older generations
        index.forceMerge();
        hits = index.newReader().search("apple", 10);
        assertEquals(1, hits.size());
        assertEquals(3, hits.get(0).generation());

        // restart preserves generation counter (no reset to 1)
        index.close();
        index = Index.open(dir, config);
        assertEquals(4, index.addDocument("a", "apple strudel"));
        index.commit();
        hits = index.newReader().search("apple", 10);
        assertEquals(1, hits.size());
        assertEquals(4, hits.get(0).generation());
        assertEquals("apple strudel", hits.get(0).text());
        index.close();
    }

    @Test
    void uncommittedWritesAreInvisible() {
        Index index = Index.open(dir, IndexConfig.builder().backgroundMergeEnabled(false).build());
        index.addDocument("x", "hidden tiger");
        assertTrue(index.newReader().search("tiger", 10).isEmpty());
        index.commit();
        assertEquals(1, index.newReader().search("tiger", 10).size());
        index.close();
    }

    private static List<String> texts(Index index, String term) {
        return index.newReader().search(term, 10).stream().map(IndexReader.Hit::text).toList();
    }
}
