package dev.example.cp.tests;

import dev.example.cp.engine.CheckpointScheduler;
import dev.example.cp.engine.Clock;
import dev.example.cp.engine.CountScheduler;
import dev.example.cp.engine.Engine;
import dev.example.cp.engine.IntervalScheduler;

import java.nio.file.Path;
import java.util.concurrent.atomic.AtomicLong;

/** 时间与调度可注入：条数调度、虚拟时钟驱动的时间调度。 */
public class SchedulersTest extends TestCase {

    public SchedulersTest() {
        super("scheduler/injectable count policy and virtual-clock interval policy");
    }

    @Override
    protected void run() {
        // 条数调度：每 4 条一个 epoch，10 条 → 2 个自动检查点 + 排空补 1 个
        Path dir = newDataDir();
        TestSupport.seed(dir);
        Engine e = TestSupport.open(dir, new CountScheduler(4));
        Engine.RunReport report = e.runUntilDrainedAndCommitted();
        assertEquals(10L, report.eventsProcessed(), "processed 10");
        assertEquals(3L, report.checkpointsCompleted(), "checkpoints at 4, 8 and final flush");
        assertEquals(9L, e.status().committedOffset(), "interval run commits all");

        // 虚拟时钟：时间完全由测试掌控
        AtomicLong fakeNow = new AtomicLong(1_000_000L);
        Clock fakeClock = fakeNow::get;
        CheckpointScheduler interval = new IntervalScheduler(100);

        Path dir2 = newDataDir();
        TestSupport.seed(dir2);
        Engine e2 = Engine.open(dir2, interval, fakeClock);
        // 推进 2 条但时间不动 → 不应检查点
        e2.runUntilEventCount(2);
        assertEquals(-1L, e2.status().appliedEpoch(), "no checkpoint before interval elapses");
        // 时钟越过 100ms 后再处理事件 → 触发检查点
        fakeNow.addAndGet(150);
        e2.runUntilEventCount(2);
        assertEquals(1L, e2.status().appliedEpoch(), "checkpoint fires once virtual time advances");
    }
}
