package com.example.hlc;

import com.example.hlc.clock.VirtualClock;
import com.example.hlc.core.HlcTimestamp;
import com.example.hlc.core.HybridLogicalClock;
import com.example.hlc.persist.FileHlcStateStore;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class PersistenceTest {

    private final ObjectMapper mapper = new ObjectMapper();

    @TempDir
    Path tempDir;

    @Test
    void saveThenLoadRoundTripsState() {
        Path stateFile = tempDir.resolve("hlc-state.json");
        FileHlcStateStore store = new FileHlcStateStore(stateFile, mapper);

        assertEquals(Optional.empty(), store.load());

        HlcTimestamp ts = new HlcTimestamp(1_727_000_000_123L, 17, "node-1");
        store.save(ts);

        assertEquals(Optional.of(ts), store.load());
        // A second store instance over the same file sees the same state.
        assertEquals(Optional.of(ts), new FileHlcStateStore(stateFile, mapper).load());
    }

    @Test
    void restartWithRolledBackWallClockStaysMonotonic() {
        Path stateFile = tempDir.resolve("hlc-state.json");
        FileHlcStateStore store = new FileHlcStateStore(stateFile, mapper);

        // First "process": wall clock at 100_000, produce and persist state.
        VirtualClock vc = new VirtualClock(100_000);
        HybridLogicalClock first = HybridLogicalClock.builder(vc, "node-1").build();
        HlcTimestamp last = first.tick();
        store.save(last);

        // "Restart": wall clock has been set 50 seconds backwards.
        vc.rewind(50_000);
        HybridLogicalClock restarted = HybridLogicalClock.builder(vc, "node-1")
                .restored(store.load())
                .build();

        HlcTimestamp afterRestart = restarted.tick();
        assertTrue(afterRestart.compareTo(last) > 0,
                "post-restart timestamp " + afterRestart + " must exceed persisted " + last
                        + " despite wall clock rollback");
        assertTrue(afterRestart.physicalMillis() >= last.physicalMillis());
    }

    @Test
    void corruptStateFileFallsBackToEmpty() throws Exception {
        Path stateFile = tempDir.resolve("hlc-state.json");
        Files.writeString(stateFile, "{not valid json!!!");

        FileHlcStateStore store = new FileHlcStateStore(stateFile, mapper);
        assertEquals(Optional.empty(), store.load());
    }
}
