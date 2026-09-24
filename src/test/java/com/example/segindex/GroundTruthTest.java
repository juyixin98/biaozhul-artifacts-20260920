package com.example.segindex;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

import static org.junit.jupiter.api.Assertions.assertEquals;

/**
 * Acceptance test 1: interleaved adds, deletes and queries, verified against
 * a full-scan ground-truth model. Includes periodic index reopens (simulated
 * restarts) and forced merges between operations.
 */
class GroundTruthTest {

    @TempDir
    Path dir;

    private static final int OPERATIONS = 1500;

    @Test
    void interleavedWritesDeletesAndQueriesMatchFullScan() {
        Random random = new Random(42);
        Map<String, String> model = new LinkedHashMap<>();
        IndexConfig config = IndexConfig.builder().backgroundMergeEnabled(false).build();
        Index index = Index.open(dir, config);

        List<String> knownIds = new ArrayList<>();
        int queriesChecked = 0;
        for (int op = 0; op < OPERATIONS; op++) {
            int roll = random.nextInt(100);
            if (roll < 45) {
                // add or update a document (id reuse happens naturally via the small id space)
                String id = "doc-" + random.nextInt(120);
                String text = Corpus.randomText(random, 5 + random.nextInt(10));
                index.addDocument(id, text);
                model.put(id, text);
                if (!knownIds.contains(id)) {
                    knownIds.add(id);
                }
            } else if (roll < 60) {
                // delete a known id
                if (!knownIds.isEmpty()) {
                    String id = knownIds.get(random.nextInt(knownIds.size()));
                    index.deleteDocument(id);
                    model.remove(id);
                }
            } else if (roll < 75) {
                index.commit();
            } else if (roll < 80) {
                index.commit();
                index.forceMerge();
            } else if (roll < 85) {
                // simulated restart: close and reopen on the same directory
                index.commit();
                index.close();
                index = Index.open(dir, config);
            } else {
                // query and compare against full scan of the model
                index.commit();
                String term = Corpus.VOCABULARY[random.nextInt(Corpus.VOCABULARY.length)];
                List<String> expected = Corpus.scanContaining(model, term);
                List<String> actual = index.newReader().search(term, 10_000).stream()
                        .map(IndexReader.Hit::docId).sorted().toList();
                assertEquals(expected, actual,
                        "query '" + term + "' after op " + op + " diverged from full scan");
                queriesChecked++;
            }
        }
        index.commit();
        // final sweep: every vocabulary term must match the model
        for (String term : Corpus.VOCABULARY) {
            List<String> expected = Corpus.scanContaining(model, term);
            List<String> actual = index.newReader().search(term, 10_000).stream()
                    .map(IndexReader.Hit::docId).sorted().toList();
            assertEquals(expected, actual, "final query '" + term + "' diverged");
        }
        index.close();
        org.junit.jupiter.api.Assertions.assertTrue(queriesChecked > 20,
                "expected a meaningful number of random queries, got " + queriesChecked);
    }
}
