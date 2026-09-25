package cep.pattern;

import cep.config.LatePolicy;
import cep.config.MatchPolicy;
import cep.config.PatternConfig;
import cep.model.EngineResult;
import cep.test.TestFramework;

import java.util.List;

/** 迟到判定、宽限 allowedLateness、ACCEPT 尽力车道语义的专项测试。 */
public final class LateDataTest {

    private LateDataTest() {}

    public static void register(TestFramework tf) {

        tf.addTest("迟到: allowedLateness 边界（ts+allowed == wm 不算迟到）", () -> {
            PatternConfig cfg = PatternConfig.of("A", "B", "C", 1000, MatchPolicy.ALL_PAIRS)
                    .withLate(LatePolicy.DROP, 400);
            PatternEngine e = new PatternEngine(cfg);
            e.ingest("a2", "A", 1000); // wm=1000
            e.ingest("a1", "A", 600);  // 600+400=1000 >= wm -> 不迟到，缓冲；wm 不变
            // 此时 a1 会在缓冲中按事件时间排在 a2 前
            e.ingest("b1", "B", 1100); // wm=1100：处理顺序 a1@600,a2@1000,b1@1100
            EngineResult r = e.flush();
            HandComputedScenarioTest.assertPairs(tf, r,
                    List.of("a1->b1", "a2->b1"));
            TestFramework.assertEquals(0L, r.stats().droppedLate, "边界内不丢弃");
        });

        tf.addTest("迟到: 越过宽限即 DROP", () -> {
            PatternConfig cfg = PatternConfig.of("A", "B", "C", 1000, MatchPolicy.ALL_PAIRS)
                    .withLate(LatePolicy.DROP, 400);
            PatternEngine e = new PatternEngine(cfg);
            e.ingest("a2", "A", 1000); // wm=1000
            e.ingest("a1", "A", 599);  // 599+400=999 < 1000 -> 迟到 DROP
            EngineResult r = e.flush();
            TestFramework.assertEquals(1L, r.stats().droppedLate, "1条丢弃");
            HandComputedScenarioTest.assertPairs(tf, r, List.of());
        });

        tf.addTest("迟到: ACCEPT 迟到A遇后续B在窗口内可补匹配", () -> {
            PatternConfig cfg = PatternConfig.of("A", "B", "C", 1000, MatchPolicy.EARLIEST_A)
                    .withLate(LatePolicy.ACCEPT, 0);
            PatternEngine e = new PatternEngine(cfg);
            e.ingest("aX", "A", 2000); // wm=2000
            e.ingest("aLate", "A", 1500); // 迟到车道：deadline2500>wm，保留
            e.ingest("b1", "B", 2200);    // 正常：候选按全序 aLate@1500 最早 -> (aLate,b1)
            EngineResult r = e.flush();
            HandComputedScenarioTest.assertPairs(tf, r, List.of("aLate->b1"));
            TestFramework.assertTrue(r.matches().get(0).late(), "标记 late");
            HandComputedScenarioTest.assertTimeoutIds(tf, r, List.of("aX"));
        });

        tf.addTest("迟到: ACCEPT 迟到A窗口已过 -> 立即late超时", () -> {
            PatternConfig cfg = PatternConfig.of("A", "B", "C", 100, MatchPolicy.ALL_PAIRS)
                    .withLate(LatePolicy.ACCEPT, 0);
            PatternEngine e = new PatternEngine(cfg);
            e.ingest("a2", "A", 1000); // wm=1000
            e.ingest("aLate", "A", 800); // 迟到，deadline900 < wm1000 -> 立即超时(late)
            EngineResult r = e.flush();
            TestFramework.assertEquals(1L, r.stats().acceptedLate, "接受1条");
            TestFramework.assertEquals(2L, r.stats().timeouts, "迟到A + 正常a2 共2个超时");
            TestFramework.assertTrue(r.timeouts().get(0).late(), "第1个超时(aLate)标记 late");
            TestFramework.assertFalse(r.timeouts().get(1).late(), "a2 的超时不 late");
        });

        tf.addTest("迟到: ACCEPT 迟到B无法匹配已超时的A", () -> {
            PatternConfig cfg = PatternConfig.of("A", "B", "C", 100, MatchPolicy.ALL_PAIRS)
                    .withLate(LatePolicy.ACCEPT, 0);
            PatternEngine e = new PatternEngine(cfg);
            e.ingest("a1", "A", 0);
            e.ingest("tick", "X", 500); // a1 早已超时
            e.ingest("bLate", "B", 50); // 迟到 B：无活跃 A，无结果
            EngineResult r = e.flush();
            HandComputedScenarioTest.assertPairs(tf, r, List.of());
            HandComputedScenarioTest.assertTimeoutIds(tf, r, List.of("a1"));
            TestFramework.assertEquals(1L, r.stats().acceptedLate, "迟到B计入acceptedLate");
        });

        tf.addTest("迟到: ACCEPT 不撤回已发出的超时（之后再来迟到B）", () -> {
            PatternConfig cfg = PatternConfig.of("A", "B", "C", 1000, MatchPolicy.ALL_PAIRS)
                    .withLate(LatePolicy.ACCEPT, 0);
            PatternEngine e = new PatternEngine(cfg);
            e.ingest("a1", "A", 0);
            e.ingest("tick", "X", 2000); // a1 超时
            e.ingest("bLate", "B", 500); // 迟到 B，a1 已超时 -> 无新匹配，超时不撤回
            EngineResult r = e.flush();
            HandComputedScenarioTest.assertPairs(tf, r, List.of());
            HandComputedScenarioTest.assertTimeoutIds(tf, r, List.of("a1"));
        });

        tf.addTest("迟到: 重复ID在DROP车道也只计duplicate", () -> {
            PatternEngine e = new PatternEngine(PatternConfig.of(
                    "A", "B", "C", 1000, MatchPolicy.ALL_PAIRS));
            e.ingest("a1", "A", 1000);
            e.ingest("a1", "A", 0); // 重复优先于迟到判定
            EngineResult r = e.flush();
            TestFramework.assertEquals(1L, r.stats().duplicates, "duplicate");
            TestFramework.assertEquals(0L, r.stats().droppedLate, "不重复计迟到");
        });
    }
}
