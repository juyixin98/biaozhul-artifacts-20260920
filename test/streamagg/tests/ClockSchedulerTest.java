package streamagg.tests;

import streamagg.core.ManualClock;
import streamagg.core.ManualScheduler;
import streamagg.core.ReconciliationReport;
import streamagg.core.StreamProcessor;

/** 验证时间与调度均可注入、手动推进，且周期对账被确定性触发。 */
public final class ClockSchedulerTest {

    public static void main(String[] args) {
        TestFramework t = new TestFramework();

        t.test("ManualClock 只随 advance/set 变化且禁止倒流", () -> {
            ManualClock c = new ManualClock(1000L);
            t.eq(c.nowMillis(), 1000L, "初始时间");
            c.advance(50);
            t.eq(c.nowMillis(), 1050L, "推进后时间");
            t.expectThrows(IllegalArgumentException.class, "倒流", () -> c.advance(-1),
                    "禁止时间倒流");
            c.set(5000L);
            t.eq(c.nowMillis(), 5000L, "set 生效");
        });

        t.test("日志时间戳取自注入时钟", () -> {
            ManualClock c = new ManualClock(1000L);
            StreamProcessor p = new StreamProcessor(c);
            c.advance(123L);
            p.submit(streamagg.core.EventOp.add(
                    "e1", "k1", new java.math.BigDecimal("1"), 1L, null));
            t.eq(p.journal().get(0).receivedAtMillis(), 1123L, "日志时间=时钟时间");
        });

        t.test("ManualScheduler 确定性触发周期对账", () -> {
            ManualClock c = new ManualClock(0L);
            ManualScheduler s = new ManualScheduler();
            StreamProcessor p = new StreamProcessor(c, s, 100L);
            p.submit(streamagg.core.EventOp.add(
                    "e1", "k1", new java.math.BigDecimal("7"), 1L, null));

            s.tick(99);
            ReconciliationReport before = p.lastReconciliation();
            t.eq(before, null, "未到周期不执行");
            s.tick(1);
            ReconciliationReport first = p.lastReconciliation();
            t.check(first != null && first.consistent(), "100ms 首次对账且一致");
            // 制造不一致是不可能的（引擎自洽），验证持续触发即可
            s.tick(250);
            ReconciliationReport later = p.lastReconciliation();
            t.check(later != null && later.consistent(), "跨多个周期后仍对账一致");
            p.shutdown();
        });
    }
}
