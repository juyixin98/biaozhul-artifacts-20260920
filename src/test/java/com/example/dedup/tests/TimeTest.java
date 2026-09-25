package com.example.dedup.tests;

import com.example.dedup.time.ManualClock;
import com.example.dedup.time.MonotonicClock;
import com.example.dedup.time.Scheduler;

/** Clock rollback behaviour and scheduler no-op contract. */
public final class TimeTest {

    public static void register(TestRunner r) {
        r.run("clock: manual clock can move both ways", () -> {
            ManualClock c = new ManualClock(100);
            TestRunner.assertEquals(100L, c.currentTimeMillis(), "start");
            c.advance(50);
            TestRunner.assertEquals(150L, c.currentTimeMillis(), "+50");
            c.advance(-30);
            TestRunner.assertEquals(120L, c.currentTimeMillis(), "rolled back 30");
            c.setTime(5);
            TestRunner.assertEquals(5L, c.currentTimeMillis(), "absolute set back");
        });

        r.run("clock: monotonic wrapper holds value on rollback and counts it", () -> {
            ManualClock base = new ManualClock(1000);
            MonotonicClock m = new MonotonicClock(base);
            TestRunner.assertEquals(1000L, m.currentTimeMillis(), "first read");
            base.setTime(900); // OS clock jumped back
            TestRunner.assertEquals(1000L, m.currentTimeMillis(), "held, not decreased");
            TestRunner.assertEquals(1L, m.rollbackCount(), "one rollback observed");
            base.setTime(1200);
            TestRunner.assertEquals(1200L, m.currentTimeMillis(), "advances again");
            TestRunner.assertEquals(1L, m.rollbackCount(), "still one");
        });

        r.run("clock: noop scheduler accepts periodic without firing", () -> {
            Scheduler s = Scheduler.noop();
            boolean[] fired = {false};
            Scheduler.Cancellable c = s.schedulePeriodic(1, () -> fired[0] = true);
            c.cancel();
            TestRunner.assertFalse(fired[0], "never fires");
        });
    }
}
