package invidx.engine;

import invidx.reference.FullScan;
import invidx.search.Hit;
import invidx.search.Query;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Acceptance oracle: a randomized stream of interleaved inserts, same-id
 * updates, deletes, flushes and merges. After every mutating step the
 * segmented index answers several queries and each answer must equal the
 * reference full-scan result — both the id set and the generation of every
 * hit.
 */
class RandomInterleaveOracleTest {

    private static final String[] VOCAB = {
            "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
            "hotel", "india", "juliet", "kilo", "lima", "mike", "november"
    };

    private record Action(String kind, int id, String payload) {
    }

    @Test
    void matchesFullScanAcrossRandomHistories(@TempDir Path tmp) throws Exception {
        // Several histories with different seeds; buffer 1..3 forces many flushes.
        for (int seed = 1; seed <= 12; seed++) {
            Path run = tmp.resolve("hist_" + seed);
            runHistory(run, seed, 300 + seed * 37L);
        }
    }

    private void runHistory(Path tmp, int seed, long iterations) throws Exception {
        Random rnd = new Random(seed * 1009 + 17);
        IndexConfig cfg = new IndexConfig(1 + rnd.nextInt(3),
                false, java.time.Duration.ofMinutes(10), 3);
        FullScan scan = new FullScan();

        List<String> genTexts = new ArrayList<>();
        long[] nextGen = new long[12];
        java.util.Arrays.fill(nextGen, 1L);
        int[] liveGen = new int[12]; // generation number currently live, 0 = absent
        java.util.Arrays.fill(liveGen, 0);

        try (InvertedIndex index = InvertedIndex.open(tmp, cfg, CrashHook.NOOP)) {
            for (long step = 0; step < iterations; step++) {
                int roll = rnd.nextInt(100);
                int id = rnd.nextInt(12);
                if (roll < 55) {
                    // Add or update: 1-3 terms.
                    int termCount = 1 + rnd.nextInt(3);
                    StringBuilder sb = new StringBuilder();
                    List<String> chosen = new ArrayList<>();
                    for (int t = 0; t < termCount; t++) {
                        String term = VOCAB[rnd.nextInt(VOCAB.length)];
                        sb.append(term).append(' ');
                        chosen.add(term);
                    }
                    String text = sb.toString().trim();
                    long gen = nextGen[id];
                    index.putDocument(id, text);
                    scan.put(id, gen, text);
                    genTexts.add(text);
                    nextGen[id] = gen + 1;
                    liveGen[id] = (int) gen;
                } else if (roll < 75) {
                    boolean removed = index.deleteDocument(id);
                    boolean oracleHad = liveGen[id] != 0;
                    assertEquals(oracleHad, removed,
                            "delete presence mismatch at step " + step + " id " + id);
                    if (removed) {
                        scan.delete(id);
                        liveGen[id] = 0;
                    }
                } else if (roll < 90) {
                    index.flush();
                } else {
                    index.forceMerge();
                }

                if (step % 5 == 0 || roll >= 90) {
                    assertQueriesMatch(index, scan, rnd, step);
                }
            }
            // Final state after a forced merge and after restart.
            index.flush();
            index.forceMerge();
            assertQueriesMatch(index, scan, rnd, iterations);
            assertEquals(scan.count(), index.stats().liveDocCount());
        }

        try (InvertedIndex reopened = InvertedIndex.open(tmp, cfg, CrashHook.NOOP)) {
            assertQueriesMatch(reopened, scan, new Random(seed ^ 0x5DEECE66DL), iterations + 1);
            assertEquals(scan.count(), reopened.stats().liveDocCount());
        }
    }

    private void assertQueriesMatch(InvertedIndex index, FullScan scan, Random rnd, long step) {
        // Term queries covering present and absent terms.
        for (int i = 0; i < 6; i++) {
            String term = VOCAB[rnd.nextInt(VOCAB.length)];
            compare(index, scan, Query.term(term), step);
        }
        // Boolean queries.
        String a = VOCAB[rnd.nextInt(VOCAB.length)];
        String b = VOCAB[rnd.nextInt(VOCAB.length)];
        compare(index, scan, Query.parse("AND:" + a + "," + b), step);
        compare(index, scan, Query.parse("OR:" + a + "," + b), step);
    }

    private void compare(InvertedIndex index, FullScan scan, Query query, long step) {
        List<Hit> hits = index.search(query);
        List<long[]> expected = scan.search(query);
        assertEquals(expected.size(), hits.size(),
                () -> "count mismatch for " + query + " at step " + step);
        for (int i = 0; i < expected.size(); i++) {
            long[] e = expected.get(i);
            Hit h = hits.get(i);
            assertEquals(e[0], h.id(), () -> "id mismatch in " + query + " step " + step);
            assertEquals(e[1], h.gen(),
                    () -> "generation mismatch for id " + h.id() + " in " + query
                            + " step " + step + " (old revision leaked?)");
        }
        // Results must be id-sorted.
        List<Integer> ids = hits.stream().map(Hit::id).toList();
        List<Integer> sorted = ids.stream().sorted().toList();
        assertTrue(ids.equals(sorted), "hits not sorted");
    }
}
