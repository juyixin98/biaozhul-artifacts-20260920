package com.example.watermark.test;

import static com.example.watermark.test.Assert.assertTrue;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

import com.example.watermark.test.TestRunner.Test;
import com.example.watermark.time.ManualScheduler;
import com.example.watermark.time.VirtualClock;
import com.example.watermark.watermarks.PartitionState;
import com.example.watermark.watermarks.StreamEvent;
import com.example.watermark.watermarks.Watermark;
import com.example.watermark.watermarks.WatermarkConfig;
import com.example.watermark.watermarks.WatermarkManager;
import com.example.watermark.watermarks.WatermarkStrategy;

/**
 * Property-style check over randomized workloads. Regardless of pauses,
 * resumes, extreme fast jumps and out-of-order replay events:
 *
 * <ol>
 *   <li>every global watermark ever emitted is strictly greater than the
 *       previous one (global monotonicity, the core acceptance invariant);</li>
 *   <li>whenever the global value advances to G, some ACTIVE partition has a
 *       local watermark of exactly G and every other ACTIVE partition with a
 *       local watermark is at least G — i.e. G is genuinely the minimum over
 *       the active set, never clamped above it and never pulled below it;</li>
 *   <li>a partition is never IDLE before the configured timeout elapses;</li>
 *   <li>replayed old events never move any watermark (they are late).</li>
 * </ol>
 */
public class RandomMonotonicityTest {

    @Test
    public void globalWatermarkMonotoneAcrossRandomWorkloads() {
        long seed = 20260923L; // deterministic
        for (int iteration = 0; iteration < 200; iteration++) {
            runOneRandomScenario(seed + iteration);
        }
    }

    private void runOneRandomScenario(long seed) {
        Random rnd = new Random(seed);
        VirtualClock clock = new VirtualClock(0);
        ManualScheduler scheduler = new ManualScheduler(clock);
        clock.addTickListener(scheduler::runDue);
        long idleTimeout = 1 + rnd.nextInt(500);
        long outOfOrderness = rnd.nextInt(300);
        long tickInterval = 1 + rnd.nextInt(50);
        WatermarkConfig config = new WatermarkConfig(tickInterval, idleTimeout, 0);
        WatermarkManager<Object> manager = new WatermarkManager<>(
                WatermarkStrategy.boundedOutOfOrderness(outOfOrderness, config), clock, scheduler);

        int n = 1 + rnd.nextInt(5);
        long[] maxTs = new long[n];
        long[] lastActivity = new long[n];
        for (int i = 0; i < n; i++) {
            manager.registerPartition("p" + i);
        }
        manager.start();

        List<Long> emissions = new ArrayList<>();
        manager.addGlobalWatermarkListener(g -> {
            if (!emissions.isEmpty()) {
                assertTrue(g > emissions.get(emissions.size() - 1),
                        "global watermark regressed at seed " + seed + ": " + emissions + " -> " + g);
            }
            emissions.add(g);
        });

        long simulated = 0;
        for (int step = 0; step < 500; step++) {
            simulated += rnd.nextInt(120);
            long before = manager.getGlobalWatermark();
            clock.advanceTo(simulated); // scheduled tick(s) fire here
            long after = manager.getGlobalWatermark();

            // If this tick advanced the global watermark, verify the new value
            // is exactly the minimum over active partitions' local watermarks.
            // This must be checked on the pure post-tick state, before any
            // event in this step reactivates an idle (stale-local) partition.
            if (after != before && after != Watermark.NO_WATERMARK) {
                long minActive = Watermark.NO_WATERMARK;
                int activeWithLocal = 0;
                boolean someExactlyAfter = false;
                for (int i = 0; i < n; i++) {
                    if (manager.getPartitionState("p" + i) == PartitionState.ACTIVE) {
                        long local = manager.getLocalWatermark("p" + i);
                        if (local != Watermark.NO_WATERMARK) {
                            activeWithLocal++;
                            minActive = minActive == Watermark.NO_WATERMARK
                                    ? local : Math.min(minActive, local);
                            if (local == after) {
                                someExactlyAfter = true;
                            }
                            assertTrue(local >= after,
                                    "active local " + local + " below newly emitted global "
                                            + after + " at seed " + seed);
                        }
                    }
                }
                assertTrue(activeWithLocal > 0, "global advanced with no active partition at seed " + seed);
                assertTrue(minActive == after,
                        "emitted global " + after + " != active min " + minActive + " at seed " + seed);
                assertTrue(someExactlyAfter,
                        "no active partition holds the emitted global value at seed " + seed);
            }

            int p = rnd.nextInt(n);
            long ts;
            int choice = rnd.nextInt(10);
            if (choice < 6) {
                maxTs[p] += rnd.nextInt(500);
                ts = maxTs[p];
            } else if (choice < 9) {
                // replay an OLD timestamp, as a resumed partition might
                ts = maxTs[p] == 0 ? rnd.nextInt(100) : Math.max(0, maxTs[p] - rnd.nextInt(1000));
            } else {
                // extreme fast partition: jump by a million+
                maxTs[p] += 1_000_000L + rnd.nextInt(1_000_000);
                ts = maxTs[p];
            }
            boolean onTime = manager.onEvent(new StreamEvent(ts, "p" + p, null));
            lastActivity[p] = simulated;

            // A late event must not move the global watermark.
            if (!onTime) {
                long wmAfterLate = manager.getGlobalWatermark();
                assertTrue(wmAfterLate == after,
                        "late event advanced the global watermark at seed " + seed);
            }

            // Idle is never declared before the timeout.
            for (int i = 0; i < n; i++) {
                if (manager.getPartitionState("p" + i) == PartitionState.IDLE) {
                    assertTrue(lastActivity[i] == 0 || simulated - lastActivity[i] >= idleTimeout,
                            "partition idled before timeout at seed " + seed);
                }
            }
        }
        manager.close();
    }
}
