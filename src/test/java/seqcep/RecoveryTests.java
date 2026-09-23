package seqcep;

import seqcep.engine.Match;
import seqcep.engine.MatchEngine;
import seqcep.engine.WalCorruptionException;

import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.List;

import static seqcep.TestRunner.*;

/**
 * Durability tests: WAL replay after "crash" (close without graceful shutdown semantics —
 * every ingest is fsynced, so close() vs kill makes no difference to the log), partial-state
 * consistency across restart, and strict corruption handling.
 */
public final class RecoveryTests {

    public static void register() {

        // ---- acceptance: partial match state is identical after recovery
        test("partial match state survives restart", () -> {
            Path dir = freshDir("recover-partial");
            Path wal = dir.resolve("events.log");

            MatchEngine e1 = MatchEngine.open("t", wal);
            e1.ingest("A", "e1", 1000);
            e1.ingest("A", "e1", 1500);
            e1.ingest("B", "e1", 2000);          // two pending A·B pairs
            e1.ingest("A", "e2", 1000);          // pending A on another entity
            e1.ingest("X", "e2", 1100);          // noise, also logged
            String fpBefore = e1.partialStateFingerprint();
            List<Match> matchesBefore = e1.matches(null);
            long lastSeqBefore = snapshotLastSeq(e1);
            e1.close(); // simulates crash boundary; log is already fsynced per record

            MatchEngine e2 = MatchEngine.open("t", wal);
            String fpAfter = e2.partialStateFingerprint();
            assertEquals(fpBefore, fpAfter, "partial-state fingerprint must be identical after recovery");
            assertEquals(matchesBefore.size(), e2.matches(null).size(), "match list size after recovery");
            assertEquals(lastSeqBefore, snapshotLastSeq(e2), "lastSeq after recovery");

            // recovered partials must still complete: C on e1 finishes both pending pairs
            e2.ingest("C", "e1", 3000);
            assertEquals(2, e2.matchCount("e1"), "recovered partial pairs complete after restart");
            // seq continues where it left off (no reuse after restart)
            assertEquals(6, e2.matches("e1").get(0).seqC(), "C gets seq 6 after 5 recovered events");
            e2.close();
        });

        // ---- completed matches are also fully recovered
        test("completed matches survive restart", () -> {
            Path dir = freshDir("recover-matches");
            Path wal = dir.resolve("events.log");

            MatchEngine e1 = MatchEngine.open("t", wal);
            e1.ingest("A", "e1", 1000);
            e1.ingest("A", "e1", 1200);
            e1.ingest("B", "e1", 2000);
            e1.ingest("C", "e1", 3000);
            assertEquals(2, e1.matchCount("e1"), "pre-restart matches");
            e1.close();

            MatchEngine e2 = MatchEngine.open("t", wal);
            assertEquals(2, e2.matchCount("e1"), "matches recovered from WAL");
            List<Match> m = e2.matches("e1");
            assertEquals(1, m.get(0).seqA(), "recovered match A seq");
            assertEquals(2, m.get(1).seqA(), "recovered match A seq (overlap)");
            e2.close();
        });

        // ---- timeout semantics are preserved across recovery
        test("window expiry still applies after recovery", () -> {
            Path dir = freshDir("recover-window");
            Path wal = dir.resolve("events.log");

            MatchEngine e1 = MatchEngine.open("t", wal);
            e1.ingest("A", "e1", 1000);
            e1.ingest("B", "e1", 2000);
            e1.close();

            MatchEngine e2 = MatchEngine.open("t", wal);
            e2.ingest("C", "e1", 13000); // 12 s after A -> expired
            assertEquals(0, e2.matchCount("e1"), "expired chain must not match after recovery");
            e2.ingest("C", "e1", 0);     // out-of-window in the other direction
            assertEquals(0, e2.matchCount("e1"), "backwards C must not match");
            e2.close();
        });

        // ---- corruption is reported loudly, never silently truncated
        test("corrupt WAL tail fails recovery explicitly", () -> {
            Path dir = freshDir("recover-corrupt");
            Path wal = dir.resolve("events.log");

            MatchEngine e1 = MatchEngine.open("t", wal);
            e1.ingest("A", "e1", 1000);
            e1.close();

            // Simulate a torn write: chop the last 5 bytes off the log.
            try (RandomAccessFile raf = new RandomAccessFile(wal.toFile(), "rw")) {
                raf.setLength(raf.length() - 5);
            } catch (IOException ioe) {
                throw new RuntimeException(ioe);
            }

            assertThrows(WalCorruptionException.class,
                    () -> MatchEngine.open("t", wal),
                    "truncated WAL must raise, not silently drop the tail");
        });

        // ---- CRC mismatch is detected
        test("bit-flipped WAL record fails CRC check", () -> {
            Path dir = freshDir("recover-crc");
            Path wal = dir.resolve("events.log");

            MatchEngine e1 = MatchEngine.open("t", wal);
            e1.ingest("A", "e1", 1000);
            e1.close();

            // Flip a byte inside the payload (after magic + header).
            try (RandomAccessFile raf = new RandomAccessFile(wal.toFile(), "rw")) {
                long pos = 8 + 16 + 2; // magic + record header + 2 bytes into payload
                raf.seek(pos);
                int b = raf.read();
                raf.seek(pos);
                raf.write(b ^ 0xFF);
            } catch (IOException ioe) {
                throw new RuntimeException(ioe);
            }

            assertThrows(WalCorruptionException.class,
                    () -> MatchEngine.open("t", wal),
                    "CRC mismatch must raise");
        });

        // ---- reset rotates the log and clears state
        test("reset clears state and rotates WAL", () -> {
            Path dir = freshDir("reset");
            Path wal = dir.resolve("events.log");

            MatchEngine e = MatchEngine.open("t", wal);
            e.ingest("A", "e1", 1000);
            e.ingest("B", "e1", 2000);
            e.ingest("C", "e1", 3000);
            assertEquals(1, e.matchCount("e1"), "pre-reset match");

            e.reset();
            assertEquals(0, e.matchCount(null), "post-reset matches");
            assertEquals(0, snapshotLastSeq(e), "post-reset lastSeq");

            // engine keeps working after reset
            e.ingest("A", "e1", 1000);
            e.ingest("B", "e1", 2000);
            e.ingest("C", "e1", 3000);
            assertEquals(1, e.matchCount("e1"), "post-reset chain matches");
            e.close();

            // and a fresh open sees only the post-reset events
            MatchEngine e2 = MatchEngine.open("t", wal);
            assertEquals(1, e2.matchCount("e1"), "reopened engine sees post-reset data only");
            e2.close();
        });
    }

    private static long snapshotLastSeq(MatchEngine e) {
        Object v = e.snapshot().get("lastSeq");
        return ((Number) v).longValue();
    }

    private static Path freshDir(String name) {
        try {
            Path dir = Path.of("build", "test-data", name);
            if (Files.exists(dir)) {
                Files.walk(dir).sorted(Comparator.reverseOrder())
                        .forEach(p -> {
                            try { Files.delete(p); } catch (IOException e) { throw new RuntimeException(e); }
                        });
            }
            Files.createDirectories(dir);
            return dir;
        } catch (IOException e) {
            throw new RuntimeException(e);
        }
    }

    private RecoveryTests() {}
}
