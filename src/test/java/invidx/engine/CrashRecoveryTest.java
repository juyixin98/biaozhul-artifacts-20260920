package invidx.engine;

import invidx.catalog.Manifest;
import invidx.catalog.ManifestStore;
import invidx.model.DocKey;
import invidx.search.Hit;
import invidx.store.Directory;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;
import java.util.stream.Stream;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Kills the emulated process at every named durability point and reopens
 * the index. The crashing index instance is deliberately <b>abandoned
 * without close()</b> — a real hard kill runs no shutdown hooks, so
 * close() must not get a chance to flush the pending batch.
 *
 * <p>Only complete segments referenced by a committed manifest may ever be
 * visible after recovery.
 */
class CrashRecoveryTest {

    private static final Set<String> ALL_POINTS =
            Set.of("flush.tmp", "flush.rename", "flush.manifest",
                    "merge.tmp", "merge.rename", "merge.manifest");

    private IndexConfig cfg() {
        return new IndexConfig(2, false, java.time.Duration.ofMinutes(10), 3);
    }

    private void seedSegments(Path tmp) throws IOException {
        try (InvertedIndex index = InvertedIndex.open(tmp, cfg(), CrashHook.NOOP)) {
            index.putDocument(1, "alpha one");
            index.putDocument(2, "bravo two");
            index.flush();
            index.putDocument(3, "charlie three");
            index.putDocument(4, "delta four");
            index.flush();
            index.putDocument(2, "bravo updated");
            index.putDocument(5, "echo five");
            index.flush();
        }
    }

    @Test
    void survivesCrashAtEveryPoint(@TempDir Path tmp) throws Exception {
        for (String point : ALL_POINTS) {
            Path runDir = tmp.resolve("run_" + point.replace('.', '_'));
            Files.createDirectories(runDir);
            seedSegments(runDir);

            // Abandon on crash: never close() this instance.
            InvertedIndex index = InvertedIndex.open(runDir, cfg(), hookOnce(point));
            boolean crashed;
            try {
                if (point.startsWith("flush")) {
                    index.putDocument(100, "foxtrot crash");
                    index.putDocument(101, "golf crash"); // triggers flush at buffer size 2
                    index.flush();
                } else {
                    index.forceMerge();
                }
                crashed = false;
            } catch (SimulatedCrash crash) {
                assertEquals(point, crash.point);
                crashed = true;
            }
            assertTrue(crashed, "expected a crash at " + point);

            assertRecoveredIndexIsConsistent(runDir, point);
        }
    }

    private CrashHook hookOnce(String point) {
        return new CrashHook() {
            boolean fired = false;

            @Override
            public void onPoint(String p) {
                if (!fired && p.equals(point)) {
                    fired = true;
                    throw new SimulatedCrash(point);
                }
            }
        };
    }

    private void assertRecoveredIndexIsConsistent(Path dir, String crashPoint) throws IOException {
        // flush.manifest fires AFTER the atomic manifest swap + fsync, so the
        // segment carrying docs 100/101 is genuinely committed: 7 live docs.
        // At every earlier point the un-flushed RAM batch is lost: 5.
        int expectedLive = crashPoint.equals("flush.manifest") ? 7 : 5;
        assertFalse(Files.exists(dir.resolve("manifest.json.tmp")),
                "stale manifest tmp survived");
        ManifestStore store = new ManifestStore(new Directory(dir));
        Manifest manifest = store.read();

        try (InvertedIndex index = InvertedIndex.open(dir, cfg(), CrashHook.NOOP)) {
            // Opening the index performs recovery; only now may the on-disk
            // tree be asserted clean of orphan/temp files.
            Path segDir = dir.resolve("segments");
            if (Files.isDirectory(segDir)) {
                try (Stream<Path> files = Files.list(segDir)) {
                    files.forEach(p -> assertTrue(p.getFileName().toString().endsWith(".seg"),
                            "orphan file survived recovery: " + p));
                }
            }
            assertEquals(manifest.segments().size(), index.stats().publishedSegments());

            // Seed truth: id 2 updated to gen 2, old gen texts gone.
            assertEquals("bravo updated", index.get(2).text());
            assertEquals(2L, index.get(2).gen());
            assertEquals(1L, index.get(1).gen());
            assertEquals(expectedLive, index.stats().liveDocCount());

            List<Hit> bravo = index.search("bravo");
            assertEquals(1, bravo.size());
            assertEquals(2L, bravo.get(0).gen());
            assertTrue(index.search("two").isEmpty(),
                    "superseded revision text must not resurface");

            // Uncommitted post-crash documents must be all-or-nothing.
            assertEquals(index.search("foxtrot").size(), index.search("golf").size());
            index.flush();
        }

        // A second reopen is a stable no-op recovery.
        try (InvertedIndex again = InvertedIndex.open(dir, cfg(), CrashHook.NOOP)) {
            assertEquals(2L, again.get(2).gen());
            again.forceMerge();
            assertEquals(expectedLive, again.stats().liveDocCount());
        }
    }

    @Test
    void segmentRenamedButNotCommittedIsReclaimedAsOrphan(@TempDir Path tmp) throws Exception {
        seedSegments(tmp);
        InvertedIndex index = InvertedIndex.open(tmp, cfg(), hookOnce("flush.rename"));
        try {
            index.putDocument(100, "hotel crash");
            index.putDocument(101, "india crash");
            index.flush();
            throw new AssertionError("expected crash");
        } catch (SimulatedCrash expected) {
            assertEquals("flush.rename", expected.point);
        }
        // abandoned without close()

        long filesBefore;
        try (Stream<Path> files = Files.list(tmp.resolve("segments"))) {
            filesBefore = files.count();
        }
        try (InvertedIndex recovered = InvertedIndex.open(tmp, cfg(), CrashHook.NOOP)) {
            // One committed segment less than the .seg files on disk (the orphan).
            assertEquals(filesBefore - 1, recovered.stats().publishedSegments());
            assertTrue(recovered.search("hotel").isEmpty());
            assertTrue(recovered.search("india").isEmpty());
            // Gen counters stay safe: id 2 updates from gen 2, never reuses it.
            DocKey key = recovered.putDocument(2, "bravo third");
            recovered.flush();
            assertEquals(3L, key.gen());
            assertEquals("bravo third", recovered.get(2).text());
        }
    }

    @Test
    void killLogTornTailIsRepairedOnReopen(@TempDir Path tmp) throws Exception {
        seedSegments(tmp);
        try (InvertedIndex index = InvertedIndex.open(tmp, cfg(), CrashHook.NOOP)) {
            index.deleteDocument(1);
        }
        Path log = tmp.resolve("deletes.log");
        byte[] bytes = Files.readAllBytes(log);
        Files.write(log, java.util.Arrays.copyOfRange(bytes, 0, bytes.length - 12));

        try (InvertedIndex repaired = InvertedIndex.open(tmp, cfg(), CrashHook.NOOP)) {
            // Whether the torn kill replayed or rolled back, the view is consistent.
            boolean live = repaired.get(1) != null;
            assertEquals(live, !repaired.search("alpha").isEmpty());
            repaired.flush();
            repaired.forceMerge();
            assertEquals(live ? 5 : 4, repaired.stats().liveDocCount());
        }
    }
}
