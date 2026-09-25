package com.example.tjoin;

import com.example.tjoin.time.ManualClock;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** Deterministic injectable clock/scheduler semantics. */
class ManualClockTest {

    @Test
    @DisplayName("time stands still until advanced; timers fire in timestamp/order sequence")
    void timersFireInOrder() {
        ManualClock clock = new ManualClock(100L);
        List<String> fired = new ArrayList<>();

        clock.scheduleAt(300L, () -> fired.add("at300"));
        clock.scheduleAfter(150L, () -> fired.add("after150"));   // at 250
        clock.scheduleAt(200L, () -> fired.add("at200"));

        assertEquals(0, fired.size(), "nothing fires while time is frozen");
        assertEquals(100L, clock.currentTimeMillis());

        int count = clock.advanceTo(300L);
        assertEquals(3, count);
        assertEquals(List.of("at200", "after150", "at300"), fired);
        assertEquals(300L, clock.currentTimeMillis());
        assertEquals(0, clock.pendingTimers());
    }

    @Test
    @DisplayName("advanceBy and chained rescheduling work")
    void advanceByAndChaining() {
        ManualClock clock = new ManualClock(0L);
        int[] ticks = {0};
        Runnable[] loop = new Runnable[1];
        loop[0] = () -> {
            ticks[0]++;
            if (ticks[0] < 3) {
                clock.scheduleAfter(100L, loop[0]);
            }
        };
        clock.scheduleAfter(100L, loop[0]);
        clock.advanceBy(1000L);
        assertEquals(3, ticks[0], "self-rescheduled timer fires at each due instant, then stops");
    }

    @Test
    @DisplayName("cannot move backwards; negative delay rejected")
    void rejectsBackwards() {
        ManualClock clock = new ManualClock(500L);
        assertThrows(IllegalArgumentException.class, () -> clock.advanceTo(499L));
        assertThrows(IllegalArgumentException.class, () -> clock.advanceBy(-1L));
    }

    @Test
    @DisplayName("timer scheduled in the past runs on next advance")
    void pastTimerRunsImmediately() {
        ManualClock clock = new ManualClock(1000L);
        boolean[] ran = {false};
        clock.scheduleAt(500L, () -> ran[0] = true);
        clock.advanceBy(1L);
        assertTrue(ran[0]);
    }
}
