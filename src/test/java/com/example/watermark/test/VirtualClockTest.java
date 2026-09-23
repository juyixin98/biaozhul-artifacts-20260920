package com.example.watermark.test;

import static com.example.watermark.test.Assert.assertEquals;
import static com.example.watermark.test.Assert.assertTrue;

import java.util.ArrayList;
import java.util.List;

import com.example.watermark.test.TestRunner.Test;
import com.example.watermark.time.ManualScheduler;
import com.example.watermark.time.ScheduledTask;
import com.example.watermark.time.VirtualClock;

/** Deterministic-clock / scheduler behavior. */
public class VirtualClockTest {

    @Test
    public void clockOnlyMovesOnAdvanceAndNeverBackwards() {
        VirtualClock clock = new VirtualClock(1000);
        assertEquals(1000, clock.currentTimeMillis());
        clock.advanceBy(50);
        assertEquals(1050, clock.currentTimeMillis());
        clock.advanceTo(1040); // backwards -> ignored
        assertEquals(1050, clock.currentTimeMillis());
        clock.advanceTo(2000);
        assertEquals(2000, clock.currentTimeMillis());
    }

    @Test
    public void schedulerFiresDuePeriodicTasksOnTick() {
        VirtualClock clock = new VirtualClock(0);
        ManualScheduler scheduler = new ManualScheduler(clock);
        clock.addTickListener(scheduler::runDue);

        List<Long> fires = new ArrayList<>();
        ScheduledTask task = scheduler.schedulePeriodically(
                () -> fires.add(clock.currentTimeMillis()), 100, 100);

        clock.advanceTo(50);
        assertEquals(0, fires.size(), "not due yet");
        clock.advanceTo(100);
        assertEquals(List.of(100L), fires);
        clock.advanceTo(350);
        assertEquals(3, fires.size(),
                "one firing per scheduled period reached by the jump (catch-up at 200 and 300)");
        assertEquals(100L, fires.get(0).longValue(), "first firing at 100");
        // Catch-up firings observe the post-jump clock (a delayed periodic task
        // runs at actual time, like ScheduledExecutorService).
        assertEquals(List.of(100L, 350L, 350L), fires);
        task.cancel();
        clock.advanceTo(500);
        assertEquals(3, fires.size(), "cancelled task stays silent");
        assertTrue(task.isCancelled());
    }

    @Test
    public void nextFireTimeTracksQueue() {
        VirtualClock clock = new VirtualClock(0);
        ManualScheduler scheduler = new ManualScheduler(clock);
        ScheduledTask a = scheduler.schedulePeriodically(() -> { }, 200, 200);
        scheduler.schedulePeriodically(() -> { }, 100, 100);
        assertEquals(100, scheduler.nextFireTime());
        clock.advanceTo(150);
        scheduler.runDue();
        assertEquals(200, scheduler.nextFireTime());
        a.cancel();
    }
}
