package cep.pattern;

import cep.config.MatchPolicy;
import cep.config.PatternConfig;
import cep.model.EngineResult;
import cep.model.KeyEvent;
import cep.test.TestFramework;

import java.util.ArrayList;
import java.util.List;

/** 四种匹配策略在共享事件流上的横向对比，以及其它 key、空输入等边界。 */
public final class EnginePolicyTest {

    private EnginePolicyTest() {}

    public static void register(TestFramework tf) {

        // 两个 A、两个 B：A 一旦匹配即消耗，不重复匹配后续 B（标准 CEP 语义）；
        // "重叠"指同一 B 可同时匹配多个仍有效的 A（见下方重叠用例）。
        // 按事件时间顺序喂入（A1@0,A2@100,B1@300,B2@400）：
        tf.addTest("策略: ALL_PAIRS B1匹配两个A，A被消耗后B2无候选", () -> {
            EngineResult r = run(MatchPolicy.ALL_PAIRS,
                    "A1:A:0", "A2:A:100", "B1:B:300", "B2:B:400");
            HandComputedScenarioTest.assertPairs(tf, r, List.of(
                    "A1->B1", "A2->B1"));
            TestFramework.assertEquals(0L, r.stats().timeouts, "A都已匹配，无超时");
        });

        tf.addTest("策略: EARLIEST_A 每次选最早，两A都用上", () -> {
            EngineResult r = run(MatchPolicy.EARLIEST_A,
                    "A1:A:0", "A2:A:100", "B1:B:300", "B2:B:400");
            HandComputedScenarioTest.assertPairs(tf, r, List.of(
                    "A1->B1", "A2->B2"));
            TestFramework.assertEquals(0L, r.stats().timeouts, "无超时");
        });

        tf.addTest("策略: LATEST_A 每次选最晚，A1超时", () -> {
            EngineResult r = run(MatchPolicy.LATEST_A,
                    "A1:A:0", "A2:A:100", "B1:B:300", "B2:B:400");
            HandComputedScenarioTest.assertPairs(tf, r, List.of(
                    "A2->B1", "A1->B2"));
            // 注意 A1@0 在 B1 时还在窗口内(dt300)，选 A2；A1 在 B2(dt400) 仍在窗口 -> 匹配。无超时。
            TestFramework.assertEquals(0L, r.stats().timeouts, "窗口1000足够大，无超时");
        });

        tf.addTest("策略: LATEST_A 窗口较小时最早A超时", () -> {
            PatternConfig cfg = PatternConfig.of("A", "B", "C", 250, MatchPolicy.LATEST_A);
            EngineResult r = run(cfg, "A1:A:0", "A2:A:100", "B1:B:300", "B2:B:400");
            // A1@0: deadline250；B1@300 到来前（wm 到达300）A1 超时。B1 候选只有 A2(dt200) -> (A2,B1)
            // B2@400：A2 已匹配；无候选
            HandComputedScenarioTest.assertPairs(tf, r, List.of("A2->B1"));
            HandComputedScenarioTest.assertTimeoutIds(tf, r, List.of("A1"));
        });

        tf.addTest("策略: NON_OVERLAPPING B2落在已消费区间后 -> 无更多匹配", () -> {
            EngineResult r = run(MatchPolicy.NON_OVERLAPPING,
                    "A1:A:0", "A2:A:100", "B1:B:300", "B2:B:400");
            HandComputedScenarioTest.assertPairs(tf, r, List.of("A1->B1"));
            // A2 被消费；B2 时没有活跃 A（下一个 A 才会开启新区间）
            TestFramework.assertEquals(0L, r.stats().timeouts, "消费不超时");
        });

        tf.addTest("策略: NON_OVERLAPPING 区间外的A开启下一段匹配", () -> {
            EngineResult r = run(MatchPolicy.NON_OVERLAPPING,
                    "A1:A:0", "A2:A:100", "B1:B:300", "A3:A:500", "B2:B:700");
            HandComputedScenarioTest.assertPairs(tf, r, List.of("A1->B1", "A3->B2"));
        });

        tf.addTest("边界: 空输入 flush 无匹配无超时", () -> {
            PatternEngine e = new PatternEngine(PatternConfig.of("A", "B", "C", 1000,
                    MatchPolicy.ALL_PAIRS));
            EngineResult r = e.flush();
            TestFramework.assertEquals(0L, r.stats().matches, "0匹配");
            TestFramework.assertEquals(0L, r.stats().timeouts, "0超时");
        });

        tf.addTest("边界: 与模式无关的key被忽略但计数", () -> {
            EngineResult r = run(MatchPolicy.ALL_PAIRS,
                    "X1:X:0", "A1:A:10", "Y1:Y:20", "B1:B:30", "Z1:Z:40");
            HandComputedScenarioTest.assertPairs(tf, r, List.of("A1->B1"));
            TestFramework.assertEquals(5L, r.stats().processed, "5条全部处理");
        });

        tf.addTest("边界: 窗口外的B不匹配，A后续超时", () -> {
            PatternConfig cfg = PatternConfig.of("A", "B", "C", 100, MatchPolicy.ALL_PAIRS);
            EngineResult r = run(cfg, "A1:A:0", "B1:B:101");
            HandComputedScenarioTest.assertPairs(tf, r, List.of());
            HandComputedScenarioTest.assertTimeoutIds(tf, r, List.of("A1"));
        });

        tf.addTest("边界: 同一B的多匹配按A全序输出（重叠匹配）", () -> {
            EngineResult r = run(MatchPolicy.ALL_PAIRS,
                    "A1:A:100", "A2:A:200", "A3:A:300", "B1:B:400");
            HandComputedScenarioTest.assertPairs(tf, r,
                    List.of("A1->B1", "A2->B1", "A3->B1"));
        });

        tf.addTest("边界: B出现时没有活跃A -> 忽略", () -> {
            EngineResult r = run(MatchPolicy.ALL_PAIRS, "B1:B:0", "B2:B:10");
            TestFramework.assertEquals(0L, r.stats().matches, "无匹配");
        });

        tf.addTest("边界: 配置非法时抛异常", () -> {
            try {
                PatternConfig.of("A", "A", "C", 1000, MatchPolicy.ALL_PAIRS);
                TestFramework.fail("a/b 相同应拒绝");
            } catch (IllegalArgumentException expected) {
                // 预期
            }
            try {
                PatternConfig.of("A", "B", "C", 0, MatchPolicy.ALL_PAIRS);
                TestFramework.fail("window=0 应拒绝");
            } catch (IllegalArgumentException expected) {
                // 预期
            }
        });
    }

    private static EngineResult run(MatchPolicy policy, String... specs) {
        return run(PatternConfig.of("A", "B", "C", 1000, policy), specs);
    }

    private static EngineResult run(PatternConfig cfg, String... specs) {
        PatternEngine e = new PatternEngine(cfg);
        List<KeyEvent> events = parse(specs);
        for (KeyEvent ev : events) {
            e.ingest(ev.id(), ev.key(), ev.timestamp());
        }
        return e.flush();
    }

    private static List<KeyEvent> parse(String... specs) {
        List<KeyEvent> out = new ArrayList<>();
        long seq = 0;
        for (String s : specs) {
            String[] p = s.split(":");
            out.add(new KeyEvent(p[0], p[1], Long.parseLong(p[2]), seq++));
        }
        return out;
    }
}
