package invidx.deletes;

import invidx.model.DocKey;
import invidx.store.Directory;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class DeleteLogTest {

    @Test
    void replaysKillRecords(@TempDir Path tmp) throws Exception {
        Directory dir = new Directory(tmp);
        DeleteLog log = new DeleteLog(dir);
        assertEquals(0, log.open().tombstones().size());

        log.appendKill(7, 1, 2);
        log.appendKill(7, 2, 3);
        log.appendKill(99, 4, 5);

        DeleteLog.Replay replay = new DeleteLog(dir).open();
        assertEquals(List.of(DocKey.encode(7, 1), DocKey.encode(7, 2), DocKey.encode(99, 4)),
                replay.tombstones());
        assertEquals(3L, replay.genAdvances().get(7));
        assertEquals(5L, replay.genAdvances().get(99));
    }

    @Test
    void repairsTornTail(@TempDir Path tmp) throws Exception {
        Directory dir = new Directory(tmp);
        DeleteLog log = new DeleteLog(dir);
        log.open();
        log.appendKill(1, 1, 2);
        log.appendKill(2, 1, 2);

        // Simulate a torn final write: append a partial, unchecksummed record.
        dir.appendDurable(dir.resolve("deletes.log"),
                "D\u0003\u0004garbage".getBytes(StandardCharsets.US_ASCII));

        DeleteLog.Replay replay = new DeleteLog(dir).open();
        assertEquals(2, replay.tombstones().size());
        assertTrue(replay.tombstones().contains(DocKey.encode(1, 1)));

        // The log remains usable after repair.
        DeleteLog repaired = new DeleteLog(dir);
        repaired.appendKill(3, 1, 2);
        assertEquals(3, new DeleteLog(dir).open().tombstones().size());
    }

    @Test
    void killRecordIsAtomicPair(@TempDir Path tmp) throws Exception {
        Directory dir = new Directory(tmp);
        DeleteLog log = new DeleteLog(dir);
        log.open();
        log.appendKill(10, 3, 4);

        // Tear exactly the second record of the batch: nothing of the kill survives.
        byte[] data = dir.readAll(dir.resolve("deletes.log"));
        byte[] cut = new byte[data.length - 10];
        System.arraycopy(data, 0, cut, 0, cut.length);
        java.nio.file.Files.write(dir.resolve("deletes.log"), cut);

        DeleteLog.Replay replay = new DeleteLog(dir).open();
        assertEquals(0, replay.tombstones().size());
        assertTrue(replay.genAdvances().isEmpty());
    }
}
