package com.example.watermark.test;

import static com.example.watermark.test.Assert.assertEquals;
import static com.example.watermark.test.Assert.assertFalse;
import static com.example.watermark.test.Assert.assertTrue;

import java.util.ArrayList;
import java.util.List;

import com.example.watermark.test.TestRunner.Test;

import com.example.watermark.time.Clock;
import com.example.watermark.time.ManualScheduler;
import com.example.watermark.time.VirtualClock;
import com.example.watermark.watermarks.LateEvent;
import com.example.watermark.watermarks.PartitionState;
import com.example.watermark.watermarks.StreamEvent;
import com.example.watermark.watermarks.Watermark;
import com.example.watermark.watermarks.WatermarkConfig;
import com.example.watermark.watermarks.WatermarkManager;
import com.example.watermark.watermarks.WatermarkStrategy;

/**
 * Acceptance tests for the multi-partition aggregator: pause/resume, extreme
 * fast partition, global monotonicity, and idle boundary conditions — all
 * driven deterministically by a virtual clock.
 */
public class WatermarkManagerTest {

    private VirtualClock clock;
    private ManualScheduler scheduler;
    private WatermarkManager<Object> manager;
    private List<Long> globalHistory;
    private List<String> stateChanges;

    /** interval=100, idleTimeout=300, bounded out-of-orderness=0. */
    private void setup(long idleTimeout, long outOfOrderness) {
        clock = new VirtualClock(0);
        scheduler = new ManualScheduler(clock);
        clock.addTickListener(scheduler::runDue);
        WatermarkConfig config = new WatermarkConfig(100, idleTimeout, 0);
        WatermarkStrategy<Object> strategy =
                WatermarkStrategy.boundedOutOfOrderness(outOfOrderness, config);
        manager = new WatermarkManager<>(strategy, clock, scheduler);
        globalHistory = new ArrayList<>();
        stateChanges = new ArrayList<>();
        manager.addGlobalWatermarkListener(globalHistory::add);
        manager.addPartitionStateListener((k, o, n) ->
                stateChanges.add(k + ":" + o + "->" + n));
        manager.start();
    }

    private void event(String key, long ts) {
        manager.onEvent(new StreamEvent(ts, key, ts));
    }

    @Test
    public void globalWatermarkIsMinimumOfActivePartitions() {
        setup(10_000, 0);
        manager.registerPartition("p1");
        manager.registerPartition("p2");
        event("p1", 1000);
        event("p2", 900);
        clock.advanceTo(100);
        assertEquals(900, manager.getGlobalWatermark(), "min(1000,900) = 900");
        assertEquals(1000, manager.getLocalWatermark("p1"), "p1 local");
        assertEquals(900, manager.getLocalWatermark("p2"), "p2 local");
    }

    @Test
    public void extremeFastPartitionCannotDragGlobalAhead() {
        setup(10_000, 0);
        manager.registerPartition("fast");
        manager.registerPartition("slow");
        event("fast", 1_000_000_000_000L);
        event("slow", 10);
        clock.advanceTo(100);
        assertEquals(10, manager.getGlobalWatermark(), "slow partition holds back the fast one");
        event("fast", 2_000_000_000_000L);
        clock.advanceTo(200);
        assertEquals(10, manager.getGlobalWatermark(), "fast advances, global still = slow");
    }

    @Test
    public void pausedPartitionIsDetectedIdleAndExcludedThenResumeNeverRegresses() {
        setup(300, 0);
        manager.registerPartition("p1");
        manager.registerPartition("p2");

        // t=0: both start at event time 1000.
        event("p1", 1000);
        event("p2", 1000);
        clock.advanceTo(100);
        assertEquals(1000, manager.getGlobalWatermark(), "both at 1000");

        // p2 pauses; p1 keeps producing.
        event("p1", 2000);
        clock.advanceTo(200);
        assertEquals(1000, manager.getGlobalWatermark(), "p2 still active at 200ms: holds wm back");
        assertEquals(PartitionState.ACTIVE, manager.getPartitionState("p2"), "200ms < 300ms");

        event("p1", 3000);
        clock.advanceTo(300);
        assertEquals(PartitionState.IDLE, manager.getPartitionState("p2"),
                "idle boundary: 300ms - 0ms >= 300ms => IDLE");
        assertEquals(3000, manager.getGlobalWatermark(),
                "idle p2 excluded: global follows p1 to 3000");
        assertTrue(stateChanges.contains("p2:ACTIVE->IDLE"), "IDLE transition notified");

        // p2 resumes with an OLD event: it is late, global does not regress.
        boolean onTime = manager.onEvent(new StreamEvent(500, "p2", 500));
        assertFalse(onTime, "replayed old event goes to the late channel");
        assertEquals(PartitionState.ACTIVE, manager.getPartitionState("p2"), "p2 reactivated");
        assertEquals(3000, manager.getGlobalWatermark(), "global unchanged by stale resume");
        assertEquals(1, manager.getLateEvents().size(), "one late event");
        LateEvent late = manager.getLateEvents().get(0);
        assertEquals(500L, late.event().timestamp(), "late timestamp");
        assertTrue(late.fromResumedPartition(), "flagged as arriving from a resumed partition");
        assertEquals(3000L, late.globalWatermark(), "late event records wm that rejected it");

        // Next tick: p2's stale local watermark (1000) must not pull global down.
        clock.advanceTo(400);
        assertEquals(3000, manager.getGlobalWatermark(),
                "no regression: resumed stale partition is clamped at the global wm");

        // p2 catches up past the global watermark; the aggregate moves on.
        event("p1", 4000);
        event("p2", 3500);
        clock.advanceTo(500);
        assertEquals(3500, manager.getGlobalWatermark(), "min(4000,3500) after catch-up");

        // Verify strict monotonicity of every emission observed by listeners.
        for (int i = 1; i < globalHistory.size(); i++) {
            assertTrue(globalHistory.get(i) > globalHistory.get(i - 1),
                    "watermark listener saw regression: " + globalHistory);
        }
        // Observed global sequence must skip the stale 1000 dip.
        assertEquals(List.of(1000L, 3000L, 3500L), globalHistory, "strictly advancing history");
    }

    @Test
    public void idleBoundaryIsCheckedAtEachTick() {
        setup(300, 0);
        manager.registerPartition("p1");
        event("p1", 100);
        clock.advanceTo(100);
        assertEquals(PartitionState.ACTIVE, manager.getPartitionState("p1"), "100ms active");
        clock.advanceTo(200);
        assertEquals(PartitionState.ACTIVE, manager.getPartitionState("p1"), "200ms active");
        clock.advanceTo(299);
        assertEquals(PartitionState.ACTIVE, manager.getPartitionState("p1"),
                "299ms still active (ticks are at 100ms multiples here)");
        clock.advanceTo(300);
        assertEquals(PartitionState.IDLE, manager.getPartitionState("p1"), "300ms exactly => idle");

        // Any new event reactivates immediately.
        event("p1", 200);
        assertEquals(PartitionState.ACTIVE, manager.getPartitionState("p1"), "event reactivates");
    }

    @Test
    public void waitingPartitionWithoutEventsDoesNotBlockGlobalWatermark() {
        setup(300, 0);
        manager.registerPartition("a");
        manager.registerPartition("b");
        event("a", 100);
        clock.advanceTo(100);
        assertEquals(100, manager.getGlobalWatermark(), "only 'a' contributes");
        assertEquals(PartitionState.WAITING, manager.getPartitionState("b"),
                "never-seen partition stays WAITING");
        clock.advanceTo(400);
        assertEquals(100, manager.getGlobalWatermark(), "'a' idles; global holds 100");
        assertEquals(PartitionState.IDLE, manager.getPartitionState("a"), "a idled out");
        assertEquals(PartitionState.WAITING, manager.getPartitionState("b"),
                "WAITING is not IDLE and never becomes it");
    }

    @Test
    public void eventAtOrBelowGlobalWatermarkIsLate() {
        setup(10_000, 0);
        manager.registerPartition("p1");
        event("p1", 1000);
        clock.advanceTo(100);
        assertEquals(1000, manager.getGlobalWatermark());
        assertFalse(manager.onEvent(new StreamEvent(1000, "p1", null)),
                "timestamp == globalWatermark is late (<=)");
        assertFalse(manager.onEvent(new StreamEvent(999, "p1", null)), "older is late");
        assertTrue(manager.onEvent(new StreamEvent(1001, "p1", null)), "one above is on time");
        assertEquals(2, manager.getLateEvents().size());
    }

    @Test
    public void noWatermarkBeforeFirstEvent() {
        setup(300, 0);
        manager.registerPartition("p1");
        clock.advanceTo(500);
        assertEquals(Watermark.NO_WATERMARK, manager.getGlobalWatermark(),
                "registered-but-empty partitions emit no watermark");
    }

    @Test
    public void disabledIdleTimeoutNeverIdles() {
        setup(0, 0);
        manager.registerPartition("p1");
        event("p1", 100);
        clock.advanceTo(10_000);
        assertEquals(PartitionState.ACTIVE, manager.getPartitionState("p1"),
                "idleTimeoutMillis=0 disables detection");
    }

    @Test
    public void unknownPartitionIsAutoRegisteredAndActivated() {
        setup(300, 0);
        event("dynamic", 42);
        assertEquals(PartitionState.ACTIVE, manager.getPartitionState("dynamic"),
                "first event auto-registers as ACTIVE");
        clock.advanceTo(100);
        assertEquals(42, manager.getGlobalWatermark());
    }

    @Test
    public void boundedOutOfOrdernessSubtractsBound() {
        setup(10_000, 200);
        manager.registerPartition("p1");
        event("p1", 1000);
        clock.advanceTo(100);
        assertEquals(800, manager.getGlobalWatermark(), "1000 - 200");
        event("p1", 1100);
        clock.advanceTo(200);
        assertEquals(900, manager.getGlobalWatermark(), "1100 - 200");
    }

    @Test
    public void localWatermarkNeverRegressesWithinPartition() {
        setup(10_000, 0);
        manager.registerPartition("p1");
        event("p1", 1000);
        clock.advanceTo(100);
        event("p1", 900); // out-of-order but within bound 0; max stays 1000
        clock.advanceTo(200);
        assertEquals(1000, manager.getLocalWatermark("p1"), "max timestamp retained");
        assertEquals(1000, manager.getGlobalWatermark());
    }

    @Test
    public void manualTickMirrorsScheduledTick() {
        // No clock listener wired: drive ticks explicitly.
        VirtualClock vc = new VirtualClock(0);
        ManualScheduler ms = new ManualScheduler(vc);
        WatermarkConfig config = new WatermarkConfig(100, 300, 0);
        WatermarkManager<Object> m = new WatermarkManager<>(
                WatermarkStrategy.boundedOutOfOrderness(0, config), vc, ms);
        m.registerPartition("p1");
        m.start();
        m.onEvent(new StreamEvent(77, "p1", null));
        assertEquals(Watermark.NO_WATERMARK, m.getGlobalWatermark(), "no tick yet");
        m.emitNow();
        assertEquals(77, m.getGlobalWatermark(), "explicit tick emits");
        m.close();
    }

    @Test
    public void systemClockSmokeTest() throws InterruptedException {
        Clock system = Clock.system();
        long before = system.currentTimeMillis();
        Thread.sleep(2);
        long after = system.currentTimeMillis();
        assertTrue(after >= before + 2, "system clock advances in real time");
    }
}
