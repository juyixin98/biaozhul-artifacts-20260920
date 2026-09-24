package com.example.segindex;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Random;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;
import java.util.stream.Stream;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Acceptance test 4: a crash may only ever publish a complete new segment.
 * Simulated crashes at each publish step must leave the index consistent:
 * the last fully-written manifest wins, orphan segments are cleaned on open,
 * and queries still match the ground-truth model afterwards.
 */
class CrashRecoveryTest {

    /** Stands in for a process kill at the exact hooked line. */
    private static final class SimulatedCrash extends RuntimeException {
        SimulatedCrash(String point) {
            super("simulated crash at " + point);
        }
    }

    @TempDir
    Path dir;

    /**
     * Returns a crash hook that throws at {@code point} only after it has been
     * armed, so setup commits can complete first. Fires at most once.
     */
    private static Consumer<String> crashOnceAt(String point, AtomicBoolean armed) {
        return p -> {
            if (p.equals(point) && armed.compareAndSet(true, false)) {
                throw new SimulatedCrash(p);
            }
        };
    }

    @Test
    void crashDuringCommitSegmentWriteLeavesPreviousState() throws Exception {
        AtomicBoolean armed = new AtomicBoolean();
        IndexConfig crashy = config(crashOnceAt("segment.beforeRename", armed));

        Index index = Index.open(dir, crashy);
        index.addDocument("d1", "apple one");
        index.commit(); // durable
        index.addDocument("d2", "banana two");
        armed.set(true); // next segment write "crashes" the process
        assertThrows(SimulatedCrash.class, index::commit);
        // do not close: simulate an unclean shutdown, then reopen
        index = Index.open(dir, config(p -> {
        }));

        assertEquals(1, index.newReader().search("apple", 10).size());
        assertTrue(index.newReader().search("banana", 10).isEmpty(),
                "uncommitted document must not appear after crash");
        assertNoGarbageLeft();
        // index keeps working afterwards
        index.addDocument("d2", "banana two");
        index.commit();
        assertEquals(1, index.newReader().search("banana", 10).size());
        index.close();
    }

    @Test
    void crashBetweenSegmentRenameAndManifestCommitPublishesNothing() throws Exception {
        AtomicBoolean armed = new AtomicBoolean();
        IndexConfig crashy = config(crashOnceAt("commit.beforeManifestCommit", armed));

        Index index = Index.open(dir, crashy);
        index.addDocument("d1", "apple one");
        index.commit();
        index.addDocument("d2", "banana two");
        armed.set(true);
        assertThrows(SimulatedCrash.class, index::commit);
        // the new segment directory was fully renamed into place, but the
        // manifest never referenced it -> it must be collected on open
        index = Index.open(dir, config(p -> {
        }));

        assertTrue(index.newReader().search("banana", 10).isEmpty());
        assertEquals(1, index.manifestSnapshot().segments.size());
        assertNoGarbageLeft();
        index.close();
    }

    @Test
    void crashDuringMergeKeepsOldSegmentsAndResults() throws Exception {
        // phase 1: build an index with several segments and deletes
        IndexConfig normal = config(p -> {
        });
        Index index = Index.open(dir, normal);
        Map<String, String> model = new LinkedHashMap<>();
        Random random = new Random(99);
        for (int i = 0; i < 12; i++) {
            String id = "doc-" + i;
            String text = Corpus.randomText(random, 6);
            index.addDocument(id, text);
            if (i % 4 == 3) {
                index.deleteDocument(id);
                model.remove(id);
            } else {
                model.put(id, text);
            }
            index.commit();
        }
        int segmentsBefore = index.liveSegmentCount();
        assertTrue(segmentsBefore >= 3);
        index.close();

        // phase 2: reopen with a crash hooked into the merge manifest swap
        AtomicBoolean armed = new AtomicBoolean(true); // arm immediately: first merge crashes
        index = Index.open(dir, config(crashOnceAt("merge.beforeManifestCommit", armed)));
        assertThrows(SimulatedCrash.class, index::forceMerge);
        // abandon the crashed instance, reopen clean
        index = Index.open(dir, normal);

        // old segments all still live, orphan merged segment cleaned up
        assertEquals(segmentsBefore, index.liveSegmentCount());
        assertNoGarbageLeft();
        for (String term : Corpus.VOCABULARY) {
            var expected = Corpus.scanContaining(model, term);
            var actual = index.newReader().search(term, 10_000).stream()
                    .map(IndexReader.Hit::docId).sorted().toList();
            assertEquals(expected, actual, "post-crash query '" + term + "' diverged");
        }

        // and a later merge succeeds, proving the index is fully operational
        index.forceMerge();
        assertEquals(1, index.liveSegmentCount());
        for (String term : Corpus.VOCABULARY) {
            var expected = Corpus.scanContaining(model, term);
            var actual = index.newReader().search(term, 10_000).stream()
                    .map(IndexReader.Hit::docId).sorted().toList();
            assertEquals(expected, actual, "post-recovery-merge query '" + term + "' diverged");
        }
        index.close();
    }

    @Test
    void manifestTmpFileFromTornWriteIsIgnored() throws IOException {
        Index index = Index.open(dir, config(p -> {
        }));
        index.addDocument("d1", "apple one");
        index.commit();
        index.close();
        // simulate a torn manifest write: garbage tmp file next to a valid manifest
        Files.writeString(dir.resolve("manifest.json.tmp"), "{ not json !!!");
        index = Index.open(dir, config(p -> {
        }));
        assertEquals(1, index.newReader().search("apple", 10).size());
        assertNoGarbageLeft();
        index.close();
    }

    private IndexConfig config(Consumer<String> hook) {
        return IndexConfig.builder()
                .backgroundMergeEnabled(false)
                .crashHook(hook)
                .build();
    }

    /** After open, the directory must contain exactly: manifest + live segments. */
    private void assertNoGarbageLeft() throws IOException {
        try (Stream<Path> entries = Files.list(dir)) {
            var names = entries.map(p -> p.getFileName().toString()).sorted().toList();
            for (String name : names) {
                boolean expected = name.equals("manifest.json")
                        || (name.startsWith("seg_") && !name.startsWith("_tmp_"));
                assertTrue(expected, "unexpected leftover after recovery: " + name);
            }
        }
    }
}
