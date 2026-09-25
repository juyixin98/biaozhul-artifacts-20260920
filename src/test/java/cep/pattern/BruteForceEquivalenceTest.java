package cep.pattern;

import cep.config.MatchPolicy;
import cep.config.PatternConfig;
import cep.model.EngineResult;
import cep.model.KeyEvent;
import cep.model.Match;
import cep.model.Timeout;
import cep.test.TestFramework;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;

/**
 * 流式引擎 vs 独立小数据精确参考实现的等价性测试（含随机差分测试）。
 *
 * <p>随机生成短序列（固定种子、可复现），以"到达顺序"喂给流式引擎（bound=0、无迟到，
 * seq 由到达顺序补齐），同时排序后交给 {@link BruteForceMatcher}，逐轮比较匹配对与超时。
 * 另用完全独立的 O(n²) 谓词枚举校验 ALL_PAIRS 匹配集合的正确性。
 */
public final class BruteForceEquivalenceTest {

    private BruteForceEquivalenceTest() {}

    private static final String[] KEYS = {"A", "B", "C", "X"};

    public static void register(TestFramework tf) {

        tf.addTest("等价性: 手算场景与参考实现一致", () -> {
            for (MatchPolicy policy : MatchPolicy.values()) {
                PatternConfig cfg = PatternConfig.of("A", "B", "C", 1000, policy);
                List<KeyEvent> input = HandComputedScenarioTest.seq(
                        HandComputedScenarioTest.ev("A1", "A", 0),
                        HandComputedScenarioTest.ev("A2", "A", 100),
                        HandComputedScenarioTest.ev("C1", "C", 400),
                        HandComputedScenarioTest.ev("A3", "A", 600),
                        HandComputedScenarioTest.ev("B1", "B", 1000),
                        HandComputedScenarioTest.ev("A4", "A", 1050),
                        HandComputedScenarioTest.ev("B2", "B", 1800),
                        HandComputedScenarioTest.ev("A5", "A", 5000),
                        HandComputedScenarioTest.ev("B3", "B", 5001));
                EngineResult stream = HandComputedScenarioTest.run(cfg, input);
                ReferenceResult ref = BruteForceMatcher.evaluate(cfg, input);
                assertAgrees(tf, stream, ref, policy + " 手算序列");
            }
        });

        tf.addTest("等价性: 随机差分（2000轮 x 4策略，固定种子）", () -> {
            Random rnd = new Random(20260923L);
            int rounds = 2000;
            for (int round = 0; round < rounds; round++) {
                MatchPolicy policy = MatchPolicy.values()[rnd.nextInt(4)];
                long window = 1 + rnd.nextInt(400);
                boolean emitTimeouts = rnd.nextBoolean();
                PatternConfig cfg = new PatternConfig("A", "B", "C", window, policy,
                        cep.config.LatePolicy.DROP, 0L, 0L, emitTimeouts);

                int n = rnd.nextInt(10); // 0..9 个事件
                List<KeyEvent> events = new ArrayList<>();
                for (int i = 0; i < n; i++) {
                    long ts = rnd.nextInt(800); // 允许同刻
                    String key = KEYS[rnd.nextInt(KEYS.length)];
                    events.add(new KeyEvent("e" + i, key, ts, i));
                }

                PatternEngine engine = new PatternEngine(cfg);
                // 两边都以全序 (ts,seq) 喂入：引擎走 bound=0 的无迟到基线，
                // 参考实现内部也排序——由此比较"事件时间处理逻辑"本身。
                List<KeyEvent> sorted = new ArrayList<>(events);
                sorted.sort(null);
                for (KeyEvent e : sorted) {
                    engine.ingest(e.id(), e.key(), e.timestamp(), e.seq());
                }
                EngineResult stream = engine.flush();
                ReferenceResult ref = BruteForceMatcher.evaluate(cfg, events);
                assertAgrees(tf, stream, ref,
                        "round=" + round + " policy=" + policy + " window=" + window
                                + " emitTimeouts=" + emitTimeouts + " events=" + events);
            }
        });

        tf.addTest("等价性: 独立O(n²)谓词校验 ALL_PAIRS 匹配集（1000轮）", () -> {
            Random rnd = new Random(42L);
            for (int round = 0; round < 1000; round++) {
                long window = 1 + rnd.nextInt(400);
                PatternConfig cfg = PatternConfig.of("A", "B", "C", window,
                        MatchPolicy.ALL_PAIRS);
                int n = rnd.nextInt(12);
                List<KeyEvent> events = new ArrayList<>();
                for (int i = 0; i < n; i++) {
                    events.add(new KeyEvent("e" + i, KEYS[rnd.nextInt(3)],
                            rnd.nextInt(500), i));
                }
                // 引擎按全序喂入（无迟到基线），事件本身保持随机时间戳用于枚举
                List<KeyEvent> sorted = new ArrayList<>(events);
                sorted.sort(null);
                PatternEngine engine = new PatternEngine(cfg);
                for (KeyEvent e : sorted) {
                    engine.ingest(e.id(), e.key(), e.timestamp(), e.seq());
                }
                EngineResult r = engine.flush();
                Set<String> expected = independentPairs(cfg, events);
                Set<String> actual = new HashSet<>();
                for (Match m : r.matches()) {
                    actual.add(m.aId() + "->" + m.bId());
                }
                if (!expected.equals(actual)) {
                    TestFramework.fail("轮次 " + round + " 匹配集不一致\n期望: "
                            + expected + "\n实际: " + actual + "\n事件: " + events);
                }
            }
        });
    }

    // ----------------------------------------------------------- 校验

    static void assertAgrees(TestFramework tf, EngineResult stream, ReferenceResult ref,
                             String context) {
        List<String> sm = new ArrayList<>();
        for (Match m : stream.matches()) {
            sm.add(m.aId() + "->" + m.bId());
        }
        List<String> rm = new ArrayList<>();
        for (Match m : ref.matches()) {
            rm.add(m.aId() + "->" + m.bId());
        }
        TestFramework.assertEquals(rm.toString(), sm.toString(), "匹配不一致 [" + context + "]");

        List<String> st = new ArrayList<>();
        for (Timeout t : stream.timeouts()) {
            st.add(t.aId());
        }
        List<String> rt = new ArrayList<>();
        for (Timeout t : ref.timeouts()) {
            rt.add(t.aId());
        }
        TestFramework.assertEquals(rt.toString(), st.toString(), "超时不一致 [" + context + "]");
    }

    /**
     * 完全独立的正确性定义（按全序扫描，模拟标准 CEP 语义）：
     * 对每个 B，在窗口内、全序位于其后、且未被 C 杀掉/未被先前 B 消耗的 A 候选，
     * 按 ALL_PAIRS 全部配对；匹配过的 A 立即消耗，不再参与后续 B。
     * 一个 C 杀掉全序严格位于它之前、仍存活的全部 A。
     */
    private static Set<String> independentPairs(PatternConfig cfg, List<KeyEvent> input) {
        List<KeyEvent> sorted = new ArrayList<>(input);
        sorted.sort(null);
        Set<String> pairs = new HashSet<>();
        List<KeyEvent> liveAs = new ArrayList<>();
        for (KeyEvent e : sorted) {
            switch (e.key()) {
                case "A" -> liveAs.add(e);
                case "C" -> liveAs.removeIf(a -> a.compareTo(e) < 0);
                case "B" -> {
                    List<KeyEvent> matched = new ArrayList<>();
                    for (KeyEvent a : new ArrayList<>(liveAs)) {
                        long dt = e.timestamp() - a.timestamp();
                        if (a.compareTo(e) < 0 && dt >= 0 && dt <= cfg.windowMs()) {
                            matched.add(a);
                        }
                    }
                    for (KeyEvent a : matched) {
                        pairs.add(a.id() + "->" + e.id());
                        liveAs.remove(a);
                    }
                }
                default -> { }
            }
        }
        return pairs;
    }
}
