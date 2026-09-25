package drvb.tests;

import drvb.time.ManualClock;
import drvb.time.ManualScheduler;
import drvb.time.Scheduler;

import java.util.List;
import java.util.concurrent.atomic.AtomicLong;

import static drvb.tests.Asserts.assertEquals;
import static drvb.tests.Asserts.assertThrows;
import static drvb.tests.Asserts.assertTrue;

public class TimeTest {

    @Test
    public void manualClockOnlyMovesWhenTold() {
        ManualClock clock = new ManualClock(1000L);
        assertEquals(1000L, clock.nowMillis(), "起始时间");
        clock.advanceBy(500L);
        assertEquals(1500L, clock.nowMillis(), "前进 500");
        clock.advanceTo(3000L);
        assertEquals(3000L, clock.nowMillis(), "跳到 3000");
        assertThrows(IllegalArgumentException.class, () -> clock.advanceTo(2999L),
                "时钟不允许倒退");
    }

    @Test
    public void manualSchedulerIsDeterministicWithCatchUp() {
        ManualClock clock = new ManualClock(0L);
        ManualScheduler scheduler = new ManualScheduler(clock);
        AtomicLong counter = new AtomicLong();
        Scheduler.ScheduledTask task =
                scheduler.scheduleFixedRate("job", 1000L, counter::incrementAndGet);

        // 时间未推进，不触发
        assertTrue(scheduler.runDue().isEmpty(), "未到点不触发");
        assertEquals(0L, task.runCount(), "运行次数为 0");

        // 推进 3.5 个周期后 runDue：补跑 3 次（catch-up）
        clock.advanceBy(3500L);
        List<String> fired = scheduler.runDue();
        assertEquals(3, fired.size(), "补跑 3 次");
        assertEquals("job", fired.get(0), "触发任务名");
        assertEquals(3L, task.runCount(), "运行次数为 3");

        // 再推进恰好一个周期边界
        clock.advanceBy(500L); // 到 4000
        scheduler.runDue();
        assertEquals(4L, task.runCount(), "边界时刻补第 4 次");
    }

    @Test
    public void taskExceptionDoesNotStopScheduler() {
        ManualClock clock = new ManualClock(0L);
        ManualScheduler scheduler = new ManualScheduler(clock);
        AtomicLong ok = new AtomicLong();
        scheduler.scheduleFixedRate("boom", 100L, () -> {
            throw new IllegalStateException("任务内部失败");
        });
        scheduler.scheduleFixedRate("ok", 100L, ok::incrementAndGet);
        clock.advanceBy(100L);
        List<String> fired = scheduler.runDue();
        assertEquals(2, fired.size(), "两个任务都被尝试");
        assertEquals(1L, ok.get(), "健康任务照常执行");
    }
}
