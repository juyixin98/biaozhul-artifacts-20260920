package com.example.wm.time;

import org.junit.jupiter.api.Test;

import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class VirtualClockAndSchedulerTest {

    @Test
    void virtualClock_cannotMoveBackwards() {
        VirtualClock clock = new VirtualClock(100);
        clock.advanceTo(200);
        clock.advanceBy(50);
        assertEquals(250, clock.currentTimeMillis());
        assertThrows(IllegalArgumentException.class, () -> clock.advanceTo(249));
        assertThrows(IllegalArgumentException.class, () -> clock.advanceBy(-1));
    }

    @Test
    void manualScheduler_runsOnlyDueTasksInTimeOrder() {
        VirtualClock clock = new VirtualClock(0);
        ManualScheduler scheduler = new ManualScheduler(clock);
        AtomicInteger fast = new AtomicInteger();
        AtomicInteger slow = new AtomicInteger();

        scheduler.schedulePeriodic(fast::incrementAndGet, 10, 10);
        scheduler.schedulePeriodic(slow::incrementAndGet, 30, 30);

        assertEquals(0, scheduler.runDue()); // t=0，无到期
        assertEquals(10, scheduler.millisUntilNextDue());

        clock.advanceTo(9);
        assertEquals(0, scheduler.runDue());
        clock.advanceTo(10);
        assertEquals(1, scheduler.runDue()); // fast 首次
        assertEquals(1, fast.get());
        assertEquals(0, slow.get());

        clock.advanceTo(20);
        assertEquals(1, scheduler.runDue()); // fast 在 t=20 第二次
        assertEquals(2, fast.get());
        assertEquals(0, slow.get());

        clock.advanceTo(30);
        // fast 在 t=30 第三次；slow 在 t=30 首次（若跳过 t=20 直接到 30，
        // fast 积压的 t=20 周期会被折叠，只在当前周期执行一次——此处先到 20 故为 3 次）
        int ran = scheduler.runDue();
        assertEquals(2, ran);
        assertEquals(3, fast.get()); // t=10,20,30
        assertEquals(1, slow.get());
    }

    @Test
    void manualScheduler_coalescesMissedPeriodsIntoOneRun() {
        VirtualClock clock = new VirtualClock(0);
        ManualScheduler scheduler = new ManualScheduler(clock);
        AtomicInteger n = new AtomicInteger();
        scheduler.schedulePeriodic(n::incrementAndGet, 10, 10);

        clock.advanceTo(100); // 跳过 10..90 共 9 个周期
        int ran = scheduler.runDue();
        assertEquals(1, ran, "积压的多个周期折叠为一次执行（fixed-rate 丢积压语义）");
        assertEquals(1, n.get());
        // 折叠后下一次从 now+period 开始
        clock.advanceTo(109);
        assertEquals(0, scheduler.runDue());
        clock.advanceTo(110);
        assertEquals(1, scheduler.runDue());
        assertEquals(2, n.get());
    }

    @Test
    void manualScheduler_cancelStopsTask() {
        VirtualClock clock = new VirtualClock(0);
        ManualScheduler scheduler = new ManualScheduler(clock);
        AtomicInteger n = new AtomicInteger();
        var task = scheduler.schedulePeriodic(n::incrementAndGet, 5, 5);
        clock.advanceTo(5);
        scheduler.runDue();
        assertEquals(1, n.get());
        task.cancel();
        assertTrue(task.isCancelled());
        clock.advanceTo(100);
        assertEquals(0, scheduler.runDue());
        assertEquals(1, n.get());
        assertTrue(scheduler.millisUntilNextDue() == Long.MAX_VALUE);
    }

    @Test
    void periodicTick_drivesIdleDetection_deterministically() throws Exception {
        // 组合验证：周期 tick 任务 + 虚拟时钟，确定性地触发空闲检测
        VirtualClock clock = new VirtualClock(0);
        ManualScheduler scheduler = new ManualScheduler(clock);
        var coord = new com.example.wm.engine.WatermarkCoordinator(
                com.example.wm.model.WatermarkConfig.of(0, 100), clock);
        coord.ingest(new com.example.wm.model.StreamEvent("a", 1000, null));
        scheduler.schedulePeriodic(coord::tick, 50, 50);

        clock.advanceTo(50);
        scheduler.runDue();
        assertEquals("ACTIVE", coord.snapshot().partitions().get(0).status().name());

        clock.advanceTo(100);
        scheduler.runDue(); // tick @100：恰好空闲边界
        assertEquals("IDLE", coord.snapshot().partitions().get(0).status().name());

        scheduler.close();
    }
}
