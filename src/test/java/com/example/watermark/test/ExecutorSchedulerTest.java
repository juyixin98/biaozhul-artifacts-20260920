package com.example.watermark.test;

import static com.example.watermark.test.Assert.assertTrue;

import java.util.concurrent.atomic.AtomicInteger;

import com.example.watermark.test.TestRunner.Test;
import com.example.watermark.time.Clock;
import com.example.watermark.time.ExecutorScheduler;

/** Smoke test for the real-time scheduler paired with the system clock. */
public class ExecutorSchedulerTest {

    @Test
    public void periodicTaskFiresInRealTime() throws Exception {
        AtomicInteger fires = new AtomicInteger();
        long start = Clock.system().currentTimeMillis();
        try (ExecutorScheduler scheduler = new ExecutorScheduler()) {
            var task = scheduler.schedulePeriodically(fires::incrementAndGet, 10, 10);
            long deadline = start + 1000;
            while (fires.get() < 3 && Clock.system().currentTimeMillis() < deadline) {
                Thread.sleep(10);
            }
            task.cancel();
            assertTrue(fires.get() >= 3, "expected at least 3 real-time firings, got " + fires.get());
        }
    }
}
