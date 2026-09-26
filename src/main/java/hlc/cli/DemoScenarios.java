package hlc.cli;

import hlc.HLCClock;
import hlc.HLCFileStore;
import hlc.HLCTimestamp;
import hlc.Json;
import hlc.LogicalCounterOverflowException;
import hlc.PhysicalClock;
import hlc.TimeZoneInfo;
import hlc.server.ScenarioService;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Built-in, fully deterministic scenarios using local fixed data. No network, no wall clock:
 * every physical-time value is pinned in the input, so the output is byte-for-byte stable.
 */
final class DemoScenarios {

    private DemoScenarios() {
    }

    static Map<String, Object> runAll() throws Exception {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("tzdb", TimeZoneInfo.version());
        out.put("basicInterleaving", basicInterleaving());
        out.put("physicalClockRegression", physicalClockRegression());
        out.put("concurrentEventsOrderedByTimestamp", concurrentEvents());
        out.put("logicalCounterOverflow", logicalCounterOverflow());
        out.put("persistenceAndRecovery", persistenceAndRecovery());
        return out;
    }

    /**
     * Three nodes with deliberate skew: alice sends m1 to bob, carol works concurrently,
     * bob forwards m2 to carol. Causal chain a1 &lt; b1 &lt; b2 &lt; c2 must be reflected by
     * strictly increasing timestamps even though bob's physical clock lags.
     */
    private static Map<String, Object> basicInterleaving() {
        return ScenarioService.simulate(Map.of(
                "name", "3-node interleaving with skew",
                "events", List.of(
                        event("a1", "alice", "SEND", 1_000_000, null),
                        event("c0", "carol", "LOCAL", 1_000_500, null),
                        event("b1", "bob", "RECV", 1_000_100, "a1"),
                        event("b2", "bob", "SEND", 1_000_200, null),
                        event("c1", "carol", "LOCAL", 1_000_600, null),
                        event("c2", "carol", "RECV", 900_000, "b2"))));
    }

    /**
     * alice's physical clock leaps backwards (10_000 -&gt; 5_000 -&gt; 1_000). Timestamps must
     * never decrease: {@code l} sticks and the counter advances.
     */
    private static Map<String, Object> physicalClockRegression() {
        return ScenarioService.simulate(Map.of(
                "name", "physical clock regression keeps timestamps monotonic",
                "events", List.of(
                        event("r1", "alice", "LOCAL", 10_000, null),
                        event("r2", "alice", "LOCAL", 5_000, null),
                        event("r3", "alice", "LOCAL", 1_000, null),
                        event("r4", "alice", "LOCAL", 10_000, null))));
    }

    /**
     * Two independent nodes never exchange messages. Every cross-node pair is concurrent, yet
     * the timestamps still impose an order — demonstrating that timestamp order cannot be
     * read back as causality.
     */
    private static Map<String, Object> concurrentEvents() {
        return ScenarioService.simulate(Map.of(
                "name", "concurrent events get an arbitrary timestamp order",
                "events", List.of(
                        event("x1", "nodeX", "LOCAL", 2_000, null),
                        event("y1", "nodeY", "LOCAL", 1_000, null),
                        event("x2", "nodeX", "LOCAL", 3_000, null),
                        event("y2", "nodeY", "LOCAL", 4_000, null))));
    }

    /**
     * Drive a node clock to the counter ceiling at a frozen physical instant. The next event
     * must fail explicitly; once physical time advances past {@code l}, the counter resets.
     */
    private static Map<String, Object> logicalCounterOverflow() {
        long frozenL = 5_000L;
        PhysicalClock.VirtualClock vc = PhysicalClock.virtual(frozenL);
        HLCClock clock = new HLCClock(vc,
                new HLCTimestamp(frozenL, LogicalCounterOverflowException.MAX_COUNTER));

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("stateAtCeiling", clock.peek().toString());
        out.put("maxCounter", LogicalCounterOverflowException.MAX_COUNTER);
        try {
            clock.tickLocal();
            out.put("overflowHandled", false);
            out.put("note", "UNEXPECTED: increment at ceiling succeeded");
        } catch (LogicalCounterOverflowException expected) {
            out.put("overflowHandled", true);
            out.put("error", expected.getMessage());
            out.put("timestampUnchanged", clock.peek().toString());
        }
        // Physical time advances past l: counter resets to 0 and operation succeeds.
        vc.set(frozenL + 1);
        HLCTimestamp recovered = clock.tickLocal();
        out.put("afterPhysicalAdvance", recovered.toString());
        out.put("counterReset", recovered.c() == 0L);
        return out;
    }

    /** Snapshot to disk, rebuild a fresh clock from the file, and continue monotonically. */
    private static Map<String, Object> persistenceAndRecovery() throws Exception {
        Path tmpDir = Files.createTempDirectory("hlc-demo-");
        Path file = tmpDir.resolve("state.properties");
        try {
            PhysicalClock.VirtualClock vc = PhysicalClock.virtual(7_000L);
            HLCClock before = new HLCClock(vc);
            before.tickLocal();
            HLCTimestamp stamped = before.send();

            HLCFileStore store = new HLCFileStore(file);
            store.save(Map.of("alice", before.snapshot()));
            String fileContents = Files.readString(file);

            // Simulate a brand-new process: fresh clock, restored from disk.
            HLCClock afterRestart = new HLCClock(PhysicalClock.virtual(6_000L));
            HLCTimestamp loaded = new HLCFileStore(file).load().get("alice");
            afterRestart.restore(loaded);
            HLCTimestamp next = afterRestart.receive(stamped); // receive our own last message

            Map<String, Object> out = new LinkedHashMap<>();
            out.put("stateFile", file.toString());
            out.put("stateFileContents", fileContents.strip());
            out.put("beforeShutdown", before.snapshot().toString());
            out.put("afterRecovery", loaded.toString());
            out.put("nextAfterRecovery", next.toString());
            out.put("monotonicAcrossRestart", next.isAfter(before.snapshot()));
            out.put("jsonRoundTrip", Json.writePretty(
                    Map.of("hlc", next.toString(), "l", next.l(), "c", next.c())).strip());
            return out;
        } finally {
            Files.deleteIfExists(file);
            Files.deleteIfExists(tmpDir);
        }
    }

    private static Map<String, Object> event(String label, String node, String type,
                                             long physicalMicros, String from) {
        Map<String, Object> e = new LinkedHashMap<>();
        e.put("label", label);
        e.put("node", node);
        e.put("type", type);
        e.put("physicalMicros", physicalMicros);
        if (from != null) {
            e.put("from", from);
        }
        return e;
    }
}
