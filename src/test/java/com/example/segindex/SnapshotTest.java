package com.example.segindex;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Acceptance test 3 (queries): a reader is a consistent snapshot. Commits,
 * deletes and merges that happen after the reader was opened must not change
 * its results.
 */
class SnapshotTest {

    @TempDir
    Path dir;

    @Test
    void readerSeesConsistentSnapshotAcrossCommitsAndMerges() {
        Index index = Index.open(dir, IndexConfig.builder().backgroundMergeEnabled(false).build());
        index.addDocument("d1", "ocean blue");
        index.addDocument("d2", "ocean deep");
        index.commit();

        IndexReader snapshot = index.newReader();
        assertEquals(2, snapshot.search("ocean", 10).size());

        // more commits, a delete and a merge happen after the snapshot was taken
        index.addDocument("d3", "ocean wave");
        index.commit();
        index.deleteDocument("d1");
        index.commit();
        index.forceMerge();

        // old reader still sees the old world
        assertEquals(2, snapshot.search("ocean", 10).size());
        // new reader sees the new world
        IndexReader fresh = index.newReader();
        var hits = fresh.search("ocean", 10);
        assertEquals(2, hits.size());
        assertTrue(hits.stream().noneMatch(h -> h.docId().equals("d1")));
        assertTrue(hits.stream().anyMatch(h -> h.docId().equals("d3")));
        index.close();
    }
}
