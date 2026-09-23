package com.example.quantiles.time;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class MockClockAndManualSchedulerTest {

    @Test
    void mockClockAdvancesForwardOnly() {
        var clock = new MockClock(100);
        assertEquals(100, clock.nowMillis());
        clock.advanceMillis(50);
        assertEquals(150, clock.nowMillis());
        clock.setTimeMillis(200);
        assertEquals(200, clock.nowMillis());
        assertThrows(IllegalArgumentException.class, () -> clock.setTimeMillis(199));
        assertThrows(IllegalArgumentException.class, () -> clock.advanceMillis(-1));
    }

    @Test
    void manualSchedulerFiresAtPeriodBoundaries() {
        var scheduler = new ManualScheduler();
        int[] count = {0};
        scheduler.schedulePeriodic(10, () -> count[0]++);
        scheduler.advanceMillis(5);
        assertEquals(0, count[0]);
        scheduler.advanceMillis(5); // now=10，第一次触发
        assertEquals(1, count[0]);
        scheduler.advanceMillis(20); // now=30，触发两次
        assertEquals(3, count[0]);
    }

    @Test
    void cancelledTaskStopsFiring() {
        var scheduler = new ManualScheduler();
        int[] count = {0};
        var cancel = scheduler.schedulePeriodic(10, () -> count[0]++);
        scheduler.advanceMillis(10);
        assertEquals(1, count[0]);
        cancel.cancel();
        scheduler.advanceMillis(50);
        assertEquals(1, count[0]);
    }

    @Test
    void multipleTasksFireInRegistrationOrderAtSameInstant() {
        var scheduler = new ManualScheduler();
        StringBuilder order = new StringBuilder();
        scheduler.schedulePeriodic(10, () -> order.append("A"));
        scheduler.schedulePeriodic(10, () -> order.append("B"));
        scheduler.advanceMillis(10);
        assertEquals("AB", order.toString());
    }

    @Test
    void invalidPeriodRejected() {
        var scheduler = new ManualScheduler();
        assertThrows(IllegalArgumentException.class,
                () -> scheduler.schedulePeriodic(0, () -> {
                }));
    }

    @Test
    void systemClockReturnsPlausibleTime() {
        long before = System.currentTimeMillis();
        long t = new SystemClock().nowMillis();
        long after = System.currentTimeMillis();
        assertTrue(t >= before && t <= after);
        assertFalse(t == 0);
    }

    @Test
    void boundedOutOfOrdernessWatermarkIsMonotonicAndDelayed() {
        var wg = WatermarkGenerator.boundedOutOfOrderness(5);
        assertEquals(Long.MIN_VALUE, wg.onPeriodicEmit());
        wg.onEvent(100);
        assertEquals(95, wg.onPeriodicEmit());
        wg.onEvent(90); // 更老的事件不回退 watermark
        assertEquals(95, wg.onPeriodicEmit());
        wg.onEvent(120);
        assertEquals(115, wg.onPeriodicEmit());
    }
}
