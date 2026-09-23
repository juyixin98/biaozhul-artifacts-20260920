package com.example.sessionwindow.tests;

import com.example.sessionwindow.time.ManualTimerService;
import com.example.sessionwindow.time.ScheduledTimerService;
import com.example.sessionwindow.time.TimerHandle;

import java.util.concurrent.atomic.AtomicInteger;

import static com.example.sessionwindow.tests.Assert.assertEquals;
import static com.example.sessionwindow.tests.Assert.assertFalse;
import static com.example.sessionwindow.tests.Assert.assertTrue;

public class TimerServiceTest {

    @Test
    void manualSchedulerRunsTasksOnlyWhenTimeAdvances() {
        ManualTimerService timer = new ManualTimerService();
        AtomicInteger fired = new AtomicInteger();
        timer.scheduleOnce(10, fired::incrementAndGet);

        assertEquals(0, fired.get(), "not fired before advance");
        timer.advance(9);
        assertEquals(0, fired.get(), "not fired at 9ms");
        timer.advance(1);
        assertEquals(1, fired.get(), "fired at 10ms");
        timer.advance(100);
        assertEquals(1, fired.get(), "one-shot fires once");
    }

    @Test
    void manualSchedulerRunsFixedRateTasksInOrder() {
        ManualTimerService timer = new ManualTimerService();
        StringBuilder log = new StringBuilder();
        timer.scheduleAtFixedRate(5, 5, () -> log.append('x'));
        timer.advance(20);
        assertEquals("xxxx", log.toString(), "fires at 5,10,15,20");
    }

    @Test
    void cancelledTimerDoesNotFire() {
        ManualTimerService timer = new ManualTimerService();
        AtomicInteger fired = new AtomicInteger();
        TimerHandle handle = timer.scheduleOnce(10, fired::incrementAndGet);
        handle.cancel();
        assertTrue(handle.isCancelled(), "handle reports cancelled");
        timer.advance(50);
        assertEquals(0, fired.get(), "cancelled task never runs");
    }

    @Test
    void shutdownClearsPendingTasks() {
        ManualTimerService timer = new ManualTimerService();
        timer.scheduleOnce(10, () -> {
        });
        timer.shutdown();
        assertTrue(timer.isShutdown(), "shutdown flag set");
        assertEquals(0, timer.pendingCount(), "no pending tasks after shutdown");
    }

    @Test
    void realSchedulerFiresTask() throws InterruptedException {
        ScheduledTimerService timer = new ScheduledTimerService();
        try {
            java.util.concurrent.CountDownLatch latch = new java.util.concurrent.CountDownLatch(1);
            timer.scheduleOnce(20, latch::countDown);
            assertTrue(latch.await(1, java.util.concurrent.TimeUnit.SECONDS), "task fired within 1s");
        } finally {
            timer.shutdown();
        }
    }

    @Test
    void tasksRunInTimestampOrder() {
        ManualTimerService timer = new ManualTimerService();
        StringBuilder log = new StringBuilder();
        timer.scheduleOnce(30, () -> log.append("c"));
        timer.scheduleOnce(10, () -> log.append("a"));
        timer.scheduleOnce(20, () -> log.append("b"));
        timer.advance(100);
        assertEquals("abc", log.toString(), "ordered by scheduled time");
        assertFalse(timer.isShutdown(), "still running");
    }
}
