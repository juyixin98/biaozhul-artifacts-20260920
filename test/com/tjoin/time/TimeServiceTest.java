package com.tjoin.time;

import static com.tjoin.Asserts.assertEquals;
import static com.tjoin.Asserts.assertFalse;
import static com.tjoin.Asserts.assertThrows;
import static com.tjoin.Asserts.assertTrue;

import com.tjoin.Test;
import com.tjoin.core.Event;
import com.tjoin.core.JoinConfig;
import com.tjoin.core.IntervalJoinOperator;
import com.tjoin.core.StreamSide;

import java.util.concurrent.atomic.AtomicInteger;

/** 可注入时钟/调度与水位线生成器测试。 */
public class TimeServiceTest {

    private static Event event(String id, long ts) {
        return new Event(id, StreamSide.LEFT, "k", ts, null);
    }

    @Test
    public void manualClockOnlyMovesWhenAdvanced() {
        ManualClock clock = new ManualClock(100L);
        assertEquals(100L, clock.now(), "start");
        clock.advanceBy(5);
        assertEquals(105L, clock.now(), "advance");
        assertThrows(IllegalArgumentException.class, () -> clock.advanceBy(-1), "no backwards");
    }

    @Test
    public void manualTimerFiresOnlyOnAdvance() {
        ManualProcessingTimeService svc = new ManualProcessingTimeService(0L);
        AtomicInteger calls = new AtomicInteger();
        svc.scheduleOnce(50L, calls::incrementAndGet);
        assertEquals(1, svc.pendingCount(), "one timer pending");
        assertEquals(0, calls.get(), "not fired in real time");

        assertEquals(0, svc.advanceBy(49), "nothing fires at t=49");
        assertEquals(0, calls.get(), "still not fired");

        int fired = svc.advanceBy(1);
        assertEquals(1, fired, "fires exactly at t=50");
        assertEquals(1, calls.get(), "called once");
        assertEquals(0, svc.pendingCount(), "one-shot removed");
    }

    @Test
    public void periodicTimerFiresMultipleTimesWithinJump() {
        ManualProcessingTimeService svc = new ManualProcessingTimeService(1000L);
        AtomicInteger ticks = new AtomicInteger();
        ScheduledTask task = svc.scheduleAtFixedRate(10L, 10L, ticks::incrementAndGet);

        // 从 1000 跳到 1105：t=1010,1020,...,1100 共 10 次
        int fired = svc.advanceTo(1105L);
        assertEquals(10, fired, "10 periodic firings");
        assertEquals(10, ticks.get(), "counter");

        // 任务取消后不再触发
        assertTrue(task.cancel(), "cancel active task");
        svc.advanceBy(50L);
        assertEquals(10, ticks.get(), "no firings after cancel");
    }

    @Test
    public void timersFireInTimeOrder() {
        ManualProcessingTimeService svc = new ManualProcessingTimeService(0L);
        StringBuilder order = new StringBuilder();
        svc.scheduleOnce(30L, () -> order.append("C"));
        svc.scheduleOnce(10L, () -> order.append("A"));
        svc.scheduleOnce(20L, () -> order.append("B"));
        svc.advanceBy(100L);
        assertEquals("ABC", order.toString(), "time-ordered execution");
    }

    @Test
    public void watermarkGeneratorBoundedOutOfOrderness() {
        BoundedOutOfOrdernessWatermarks g = new BoundedOutOfOrdernessWatermarks(3L);
        assertEquals(Long.MIN_VALUE, g.currentWatermark(), "no events => MIN_VALUE");
        g.onEvent(event("a", 10));
        g.onEvent(event("b", 8)); // 乱序
        assertEquals(7L, g.currentWatermark(), "wm = maxTs - ooo = 10 - 3");
        g.onEvent(event("c", 12));
        assertEquals(9L, g.currentWatermark(), "wm advances to 9");
    }

    @Test
    public void periodicAssignerDrivesOperatorWithInjectedTime() {
        IntervalJoinOperator op = new IntervalJoinOperator(JoinConfig.symmetric(5L));
        ManualProcessingTimeService svc = new ManualProcessingTimeService(0L);
        BoundedOutOfOrdernessWatermarks leftGen = new BoundedOutOfOrdernessWatermarks(0L);
        BoundedOutOfOrdernessWatermarks rightGen = new BoundedOutOfOrdernessWatermarks(0L);

        PeriodicWatermarkAssigner leftAssigner =
                new PeriodicWatermarkAssigner(StreamSide.LEFT, leftGen, op, svc, 10L);
        PeriodicWatermarkAssigner rightAssigner =
                new PeriodicWatermarkAssigner(StreamSide.RIGHT, rightGen, op, svc, 10L);
        leftAssigner.start();
        rightAssigner.start();

        op.processEvent(new Event("L1", StreamSide.LEFT, "k", 10, null), null);
        op.processEvent(new Event("R1", StreamSide.RIGHT, "k", 10, null), null);
        leftGen.onEvent(new Event("L1", StreamSide.LEFT, "k", 10, null));
        rightGen.onEvent(new Event("R1", StreamSide.RIGHT, "k", 10, null));

        // 右流随后停滞：只推进左生成器
        leftGen.onEvent(new Event("L2", StreamSide.LEFT, "k", 20, null));
        op.processEvent(new Event("L2", StreamSide.LEFT, "k", 20, null), null);

        svc.advanceBy(10L);
        assertEquals(20L, op.watermark(StreamSide.LEFT), "left wm advanced to 20");
        assertEquals(10L, op.watermark(StreamSide.RIGHT), "right wm stays at 10 (stalled)");
        assertEquals(10L, op.outputWatermark(), "output wm min = 10");

        // 右侧停滞期间：左事件不能被左 wm 自己清掉（本就不由它清理），也不能被右 wm 提前清掉
        assertEquals(2, op.bufferedCount(StreamSide.LEFT), "left records retained while right stalls");

        // 右流恢复
        rightGen.onEvent(new Event("R2", StreamSide.RIGHT, "k", 20, null));
        op.processEvent(new Event("R2", StreamSide.RIGHT, "k", 20, null), null);
        svc.advanceBy(10L);
        assertEquals(20L, op.watermark(StreamSide.RIGHT), "right wm catches up");

        leftAssigner.close();
        rightAssigner.close();
        assertFalse(svc.pendingCount() > 0, "all periodic tasks cancelled");
    }
}
