package com.example.tjoin;

import com.example.tjoin.time.SystemClock;
import com.example.tjoin.time.SystemScheduler;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Smoke test for the wall-clock production scheduler (the deterministic
 * path is covered exhaustively via {@link ManualClockTest}).
 */
class SystemSchedulerTest {

    @Test
    @DisplayName("scheduleAfter fires on real wall clock and close() shuts down")
    void firesAndCloses() throws Exception {
        CountDownLatch latch = new CountDownLatch(1);
        SystemScheduler scheduler = new SystemScheduler(SystemClock.INSTANCE, "test-sched");
        try {
            scheduler.scheduleAfter(20L, latch::countDown);
            assertTrue(latch.await(2, TimeUnit.SECONDS), "timer should fire on wall clock");
        } finally {
            scheduler.close();
        }
    }
}
