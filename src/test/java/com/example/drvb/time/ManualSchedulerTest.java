package com.example.drvb.time;

import org.junit.jupiter.api.Test;

import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class ManualSchedulerTest {

    @Test
    void firesOnlyOnExplicitTicks() throws InterruptedException {
        ManualScheduler s = new ManualScheduler();
        AtomicInteger n = new AtomicInteger();
        s.start(n::incrementAndGet, 50);
        Thread.sleep(80); // real time passes; nothing should fire
        assertEquals(0, n.get());
        s.tick(3);
        assertEquals(3, n.get());
        s.stop();
        assertFalse(s.isStarted());
    }

    @Test
    void doubleStartRejectedAndTickAfterStopFails() {
        ManualScheduler s = new ManualScheduler();
        s.start(() -> { }, 10);
        assertTrue(s.isStarted());
        assertThrows(IllegalStateException.class, () -> s.start(() -> { }, 10));
        s.stop();
        assertThrows(IllegalStateException.class, s::tick);
    }

    @Test
    void simClockMovesForwardOnly() {
        SimClock c = new SimClock(1000);
        assertEquals(1000, c.nowMillis());
        c.advance(250);
        assertEquals(1250, c.nowMillis());
        assertThrows(IllegalArgumentException.class, () -> c.advance(-1));
        c.set(5000);
        assertEquals(5000, c.nowMillis());
    }
}
