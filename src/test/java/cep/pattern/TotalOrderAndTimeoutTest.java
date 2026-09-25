package cep.pattern;

import cep.config.MatchPolicy;
import cep.config.PatternConfig;
import cep.model.EngineResult;
import cep.test.TestFramework;

import java.util.List;

/** 全序细节、事件/定时器同刻次序、手动水位、虚拟时间/调度器注入的专项测试。 */
public final class TotalOrderAndTimeoutTest {

    private TotalOrderAndTimeoutTest() {}

    public static void register(TestFramework tf) {

        tf.addTest("全序: 同刻多个A与B，ALL_PAIRS 严格按seq配对", () -> {
            PatternEngine e = new PatternEngine(PatternConfig.of(
                    "A", "B", "C", 1000, MatchPolicy.ALL_PAIRS));
            e.ingest("a1", "A", 500, 0);
            e.ingest("a2", "A", 500, 1);
            e.ingest("b1", "B", 500, 2);
            EngineResult r = e.flush();
            HandComputedScenarioTest.assertPairs(tf, r,
                    List.of("a1->b1", "a2->b1"));
        });

        tf.addTest("全序: 同刻A与C交错 A C A C B", () -> {
            PatternEngine e = new PatternEngine(PatternConfig.of(
                    "A", "B", "C", 1000, MatchPolicy.ALL_PAIRS));
            e.ingest("a1", "A", 10, 0);
            e.ingest("c1", "C", 10, 1);
            e.ingest("a2", "A", 10, 2);
            e.ingest("c2", "C", 10, 3);
            e.ingest("b1", "B", 10, 4);
            EngineResult r = e.flush();
            // a1 被 c1 杀；a2 被 c2 杀；b1 无候选
            HandComputedScenarioTest.assertPairs(tf, r, List.of());
            TestFramework.assertEquals(2L, r.stats().cKilled, "两个A都被杀");
        });

        tf.addTest("时间交织: B恰在deadline时刻到达，事件先于定时器 -> 仍匹配", () -> {
            // 用 EARLIEST_A 隔离：只关心 a1(deadline=1000) 与同刻 B 的次序
            PatternEngine e = new PatternEngine(PatternConfig.of(
                    "A", "B", "C", 1000, MatchPolicy.EARLIEST_A));
            e.ingest("a1", "A", 0);
            e.ingest("a2", "A", 1);
            // 用另一事件把 wm 推到 1000（a1 deadline=1000, a2 deadline=1001）
            e.ingest("tick", "X", 1000);
            // a1 不应超时（deadline==wm 不超时，下一条事件才判定）
            EngineResult snap = e.snapshot();
            TestFramework.assertEquals(0L, snap.stats().timeouts, "deadline 时刻不超时");

            e.ingest("b1", "B", 1000); // 与 a1 deadline 同刻：事件先 -> (a1,b1) dt1000
            e.ingest("tick2", "X", 1002); // wm=1002：a2(deadline1001)超时（已被EARLIEST跳过）
            EngineResult r = e.flush();
            HandComputedScenarioTest.assertPairs(tf, r, List.of("a1->b1"));
            TestFramework.assertEquals(1L, r.stats().timeouts, "a2 未被选择，随后超时");
        });

        tf.addTest("时间交织: 跨过deadline后B才到 -> 已超时不复活", () -> {
            PatternEngine e = new PatternEngine(PatternConfig.of(
                    "A", "B", "C", 1000, MatchPolicy.ALL_PAIRS));
            e.ingest("a1", "A", 0);
            e.ingest("tick", "X", 1001); // wm=1001 > deadline1000 -> a1 超时
            TestFramework.assertEquals(1L, e.snapshot().stats().timeouts, "a1 超时");
            e.ingest("b1", "B", 1001);
            EngineResult r = e.flush();
            HandComputedScenarioTest.assertPairs(tf, r, List.of());
            HandComputedScenarioTest.assertTimeoutIds(tf, r, List.of("a1"));
        });

        tf.addTest("水位: 手动注入水位触发超时", () -> {
            PatternEngine e = new PatternEngine(PatternConfig.of(
                    "A", "B", "C", 1000, MatchPolicy.ALL_PAIRS));
            e.ingest("a1", "A", 5000);
            e.advanceWatermark(6000); // deadline=6000，不超时
            TestFramework.assertEquals(0L, e.snapshot().stats().timeouts, "wm==deadline 不超时");
            e.advanceWatermark(6001);
            TestFramework.assertEquals(1L, e.snapshot().stats().timeouts, "wm>deadline 超时");
        });

        tf.addTest("水位: 回退被拒绝", () -> {
            PatternEngine e = new PatternEngine(PatternConfig.of(
                    "A", "B", "C", 1000, MatchPolicy.ALL_PAIRS));
            e.advanceWatermark(100);
            try {
                e.advanceWatermark(99);
                TestFramework.fail("水位回退应抛异常");
            } catch (IllegalArgumentException expected) {
                // 预期
            }
        });

        tf.addTest("调度器: VirtualClock+HeapScheduler 时间注入可驱动任务", () -> {
            cep.time.VirtualClock clock = new cep.time.VirtualClock(0);
            cep.time.HeapScheduler scheduler = new cep.time.HeapScheduler(0);
            int[] fired = {0};
            scheduler.schedule(50, () -> fired[0]++);
            scheduler.schedule(50, () -> fired[0]++); // 同 deadline 按注册顺序
            scheduler.schedule(200, () -> fired[0]++);

            TestFramework.assertEquals(0, scheduler.advanceTime(49), "未到点");
            clock.advance(49);
            TestFramework.assertEquals(49L, clock.nowMillis(), "虚拟时钟独立推进");

            // 半开区间语义：advanceTime(t) 只触发 deadline < t
            TestFramework.assertEquals(0, scheduler.advanceTime(50),
                    "半开：advanceTime(50) 不触发 deadline=50");
            TestFramework.assertEquals(2, scheduler.advanceTime(51), "51 时触发两个");
            TestFramework.assertEquals(0, scheduler.advanceTime(100), "无新任务");
            TestFramework.assertEquals(1, scheduler.advanceTime(201), "第三个在200触发");
            TestFramework.assertEquals(3, fired[0], "共触发3次");

            // 取消语义
            cep.time.HeapScheduler s2 = new cep.time.HeapScheduler(0);
            int[] f2 = {0};
            cep.time.ScheduledTask task = s2.schedule(100, () -> f2[0]++);
            task.cancel();
            s2.advanceTime(200);
            TestFramework.assertEquals(0, f2[0], "取消后不触发");

            try {
                scheduler.advanceTime(100);
                TestFramework.fail("时间回退应抛异常");
            } catch (IllegalArgumentException expected) {
                // 预期
            }
        });

        tf.addTest("乱序: outOfOrderBound 缓冲晚到但未迟到的事件", () -> {
            PatternConfig cfg = PatternConfig.of("A", "B", "C", 1000, MatchPolicy.ALL_PAIRS)
                    .withOutOfOrderBound(500);
            PatternEngine e = new PatternEngine(cfg);
            // 到达乱序：B 的事件时间 600 先到，A@500 晚到但 500+bound=1000 >= wm
            e.ingest("b1", "B", 600); // wm=600-500=100，缓冲
            e.ingest("a1", "A", 500); // wm=max600-500=100，仍缓冲；按事件时间 a1<b1
            e.ingest("tick", "X", 1500); // wm=1000，排空：a1@500 -> b1@600 匹配
            EngineResult r = e.flush();
            HandComputedScenarioTest.assertPairs(tf, r, List.of("a1->b1"));
        });

        tf.addTest("乱序: 超过bound的迟到事件按DROP处理", () -> {
            PatternConfig cfg = PatternConfig.of("A", "B", "C", 1000, MatchPolicy.ALL_PAIRS)
                    .withOutOfOrderBound(500);
            PatternEngine e = new PatternEngine(cfg);
            e.ingest("b1", "B", 600);   // wm=100
            e.ingest("a1", "A", 50);    // 50 < wm=100 -> 迟到，DROP
            EngineResult r = e.flush();
            TestFramework.assertEquals(1L, r.stats().droppedLate, "迟到丢弃");
            HandComputedScenarioTest.assertPairs(tf, r, List.of());
        });

        tf.addTest("超时: 多个A的超时按(deadline,全序)发射", () -> {
            PatternEngine e = new PatternEngine(PatternConfig.of(
                    "A", "B", "C", 1000, MatchPolicy.ALL_PAIRS));
            e.ingest("a1", "A", 0);   // deadline1000
            e.ingest("a2", "A", 100); // deadline1100
            e.ingest("a3", "A", 200); // deadline1200
            e.ingest("tick", "X", 5000);
            EngineResult r = e.flush();
            HandComputedScenarioTest.assertTimeoutIds(tf, r, List.of("a1", "a2", "a3"));
        });
    }
}
