package com.example.segindex;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Random;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Background merge: segments coalesce without changing query results, and
 * tombstoned postings are physically dropped from merged segment files.
 */
class MergeTest {

    @TempDir
    Path dir;

    @Test
    void backgroundMergeCoalescesSegmentsAndPurgesDeletes() throws Exception {
        IndexConfig config = IndexConfig.builder().mergeFactor(3).backgroundMergeEnabled(true).build();
        Index index = Index.open(dir, config);
        Map<String, String> model = new LinkedHashMap<>();
        Random random = new Random(7);

        // produce many small segments, deleting roughly every third document
        for (int i = 0; i < 30; i++) {
            String id = String.format("doc-%03d", i);
            String text = Corpus.randomText(random, 8);
            index.addDocument(id, text);
            if (i % 3 == 2) {
                index.deleteDocument(id);
                model.remove(id);
            } else {
                model.put(id, text);
            }
            index.commit(); // one segment per commit
        }

        assertTrue(index.liveSegmentCount() >= 3, "expected several segments before merge");
        // wait for the background merger to do its work; it may legitimately
        // stop just below the merge factor, so accept <= mergeFactor-1 segments
        long deadline = System.currentTimeMillis() + 10_000;
        while (index.liveSegmentCount() >= 3 && System.currentTimeMillis() < deadline) {
            Thread.sleep(50);
        }
        assertTrue(index.liveSegmentCount() <= 2,
                "background merge should shrink 30 segments, still have " + index.liveSegmentCount());
        // finish the job synchronously so the remaining assertions are deterministic
        index.forceMerge();
        assertEquals(1, index.liveSegmentCount());

        // results still match the model after merging
        for (String term : Corpus.VOCABULARY) {
            var expected = Corpus.scanContaining(model, term);
            var actual = index.newReader().search(term, 10_000).stream()
                    .map(IndexReader.Hit::docId).sorted().toList();
            assertEquals(expected, actual, "post-merge query '" + term + "' diverged");
        }

        // tombstones fully applied: cleared from manifest, deleted docs gone from segment files
        assertEquals(0, index.manifestSnapshot().tombstones.size());
        String segmentDir = index.manifestSnapshot().segments.get(0);
        String docsJson = Files.readString(dir.resolve(segmentDir).resolve("docs.json"));
        for (int i = 0; i < 30; i++) {
            String id = String.format("doc-%03d", i);
            boolean shouldExist = model.containsKey(id);
            assertEquals(shouldExist, docsJson.contains(id),
                    "segment file should " + (shouldExist ? "contain " : "not contain ") + id);
        }
        index.close();
    }

    @Test
    void forceMergeIsIdempotentAndKeepsResults() {
        Index index = Index.open(dir, IndexConfig.builder().backgroundMergeEnabled(false).build());
        index.addDocument("m1", "stone river");
        index.commit();
        index.addDocument("m2", "stone valley");
        index.commit();
        index.forceMerge();
        index.forceMerge(); // nothing left to merge
        assertEquals(1, index.liveSegmentCount());
        assertEquals(2, index.newReader().search("stone", 10).size());
        index.close();
    }
}
