package cep.pattern;

import cep.config.LatePolicy;
import cep.config.MatchPolicy;
import cep.config.PatternConfig;
import cep.model.EngineResult;
import cep.model.KeyEvent;
import cep.model.Match;
import cep.model.Timeout;
import cep.test.TestFramework;

import java.util.ArrayList;
import java.util.List;

/**
 * 验收核心：手算短序列对照。每个场景在 README 的"手算对照"一节有逐步推演，
 * 这里断言匹配（含所用事件 ID）、超时、被杀计数与统计数字。
 */
public final class HandComputedScenarioTest {

    private HandComputedScenarioTest() {}

    // 手算事件（时间单位 ms）
    private static final PatternConfig CFG_ALL = PatternConfig.of("A", "B", "C", 1000,
            MatchPolicy.ALL_PAIRS);
    private static final PatternConfig CFG_EARLY = CFG_ALL.withPolicy(MatchPolicy.EARLIEST_A);
    private static final PatternConfig CFG_LATE = CFG_ALL.withPolicy(MatchPolicy.LATEST_A);
    private static final PatternConfig CFG_NON = CFG_ALL.withPolicy(MatchPolicy.NON_OVERLAPPING);

    public static void register(TestFramework tf) {

        // -------- 场景 1：多个 A 候选 + C 打断 --------
        // A1@0, A2@100, C@400, A3@600, B@1000
        // 期望：A1,A2 被 C 杀；A3→B（dt=400）唯一匹配
        tf.addTest("手算1: ALL_PAIRS 多A候选与C打断 -> 仅 A3->B", () -> {
            EngineResult r = run(CFG_ALL, seq(
                    ev("A1", "A", 0), ev("A2", "A", 100), ev("C1", "C", 400),
                    ev("A3", "A", 600), ev("B1", "B", 1000)));
            assertPairs(tf, r, List.of(pair("A3", "B1")));
            TestFramework.assertEquals(0L, r.stats().timeouts, "无超时");
            TestFramework.assertEquals(2L, r.stats().cKilled, "C 杀掉 A1,A2");
            assertTimeoutIds(tf, r, List.of());
        });

        // -------- 场景 2a：三个候选 A，ALL_PAIRS 重叠匹配 --------
        // A1@0,A2@100,A3@200, B@500
        // 期望 3 个匹配：(A1,B)(A2,B)(A3,B)，顺序按 A 全序；无超时
        tf.addTest("手算2a: ALL_PAIRS 一个B匹配三个A（重叠保留）", () -> {
            EngineResult r = run(CFG_ALL, seq(
                    ev("A1", "A", 0), ev("A2", "A", 100), ev("A3", "A", 200),
                    ev("B1", "B", 500)));
            assertPairs(tf, r, List.of(pair("A1", "B1"), pair("A2", "B1"), pair("A3", "B1")));
            TestFramework.assertEquals(0L, r.stats().timeouts, "无超时");
        });

        // -------- 场景 2b：EARLIEST_A，未被选择的 A 等后续 --------
        // A1@0,A2@100,A3@200, B@500 -> 仅 (A1,B)；A2@1000, B2@1050 -> (A2,B2)；
        // A3 在 flush 时超时（deadline1200）
        tf.addTest("手算2b: EARLIEST_A 选最早A，A3超时", () -> {
            EngineResult r = run(CFG_EARLY, seq(
                    ev("A1", "A", 0), ev("A2", "A", 100), ev("A3", "A", 200),
                    ev("B1", "B", 500), ev("A4", "A", 1000), ev("B2", "B", 1050)));
            assertPairs(tf, r, List.of(pair("A1", "B1"), pair("A2", "B2")));
            assertTimeoutIds(tf, r, List.of("A3", "A4"));
        });

        // -------- 场景 2c：LATEST_A --------
        // 同上序列：B1 选 A3；B2 在 1050 时候选是 A1(deadline1000 已超时),A2,A4：
        // A1 在 t=1000 时被超时（B2 前），候选窗口内为 A2(dt950),A4(dt50) -> 选 A4；
        // A2 flush 超时
        tf.addTest("手算2c: LATEST_A 选最晚A，其余按窗口超时", () -> {
            EngineResult r = run(CFG_LATE, seq(
                    ev("A1", "A", 0), ev("A2", "A", 100), ev("A3", "A", 200),
                    ev("B1", "B", 500), ev("A4", "A", 1000), ev("B2", "B", 1050)));
            assertPairs(tf, r, List.of(pair("A3", "B1"), pair("A4", "B2")));
            assertTimeoutIds(tf, r, List.of("A1", "A2"));
        });

        // -------- 场景 2d：NON_OVERLAPPING 贪婪消费 --------
        // A1@0,A2@100,A3@200,B1@500 -> (A1,B1)，消费 A2,A3；无超时
        // A5@600,B2@900 -> (A5,B2)
        tf.addTest("手算2d: NON_OVERLAPPING 消费区间内全部A", () -> {
            EngineResult r = run(CFG_NON, seq(
                    ev("A1", "A", 0), ev("A2", "A", 100), ev("A3", "A", 200),
                    ev("B1", "B", 500), ev("A5", "A", 600), ev("B2", "B", 900)));
            assertPairs(tf, r, List.of(pair("A1", "B1"), pair("A5", "B2")));
            TestFramework.assertEquals(0L, r.stats().timeouts, "被消费A不超时");
        });

        // -------- 场景 3：C 的位置语义（只打断全序在它之前的 A）--------
        // C@50 位于 A1@0 与 A2@100 之间：只杀 A1；A2->B@800
        tf.addTest("手算3: C 只打断它之前的 A，之后的 A 照常匹配", () -> {
            EngineResult r = run(CFG_ALL, seq(
                    ev("A1", "A", 0), ev("C1", "C", 50), ev("A2", "A", 100),
                    ev("B1", "B", 800)));
            assertPairs(tf, r, List.of(pair("A2", "B1")));
            TestFramework.assertEquals(1L, r.stats().cKilled, "只杀 A1");
            assertTimeoutIds(tf, r, List.of());
        });

        // -------- 场景 4：窗口超时（边界两侧各一）--------
        // A1@0, B@999   -> dt 999 <=1000 匹配
        // A2@2000(active), 下一条 B@3000 -> dt 1000，恰好边界 -> 匹配
        // A3@5000, B@6001 -> dt 1001 -> 不匹配，A3 超时
        tf.addTest("手算4: 窗口边界包含 dt=1000，dt=1001 超时", () -> {
            EngineResult r = run(CFG_ALL, seq(
                    ev("A1", "A", 0), ev("B1", "B", 999),
                    ev("A2", "A", 2000), ev("B2", "B", 3000),
                    ev("A3", "A", 5000), ev("B3", "B", 6001)));
            assertPairs(tf, r, List.of(pair("A1", "B1"), pair("A2", "B2")));
            assertTimeoutIds(tf, r, List.of("A3"));
            TestFramework.assertEquals(1L, r.stats().timeouts, "仅 A3 超时");
        });

        // -------- 场景 5：同一时间戳的全序（seq 决胜）--------
        // 同刻输入顺序 A, B, C：
        //   A#0 被 B#1 匹配 -> (A,B)；C#2 在 B 之后，无 A 可杀
        // 同刻输入顺序 A, C, B：
        //   C#1 杀掉 A#0；B#2 无候选
        tf.addTest("手算5: 同刻 A,B,C 顺序 -> 匹配", () -> {
            EngineResult r = run(CFG_ALL, seq(
                    ev("a", "A", 100), ev("b", "B", 100), ev("c", "C", 100)));
            assertPairs(tf, r, List.of(pair("a", "b")));
            TestFramework.assertEquals(0L, r.stats().cKilled, "C 在 B 之后不杀 A");
        });

        tf.addTest("手算5: 同刻 A,C,B 顺序 -> 被打断", () -> {
            EngineResult r = run(CFG_ALL, seq(
                    ev("a", "A", 100), ev("c", "C", 100), ev("b", "B", 100)));
            assertPairs(tf, r, List.of());
            TestFramework.assertEquals(1L, r.stats().cKilled, "C 杀掉同刻在它之前的 A");
            assertTimeoutIds(tf, r, List.of());
        });

        // 同刻 B 早于 A：B 不能匹配"未来"的 A，A 在 flush 超时
        tf.addTest("手算5: 同刻 B,A 顺序 -> B 不匹配后续 A", () -> {
            EngineResult r = run(CFG_ALL, seq(
                    ev("b", "B", 100), ev("a", "A", 100)));
            assertPairs(tf, r, List.of());
            assertTimeoutIds(tf, r, List.of("a"));
        });

        // 显式 seq 与到达顺序不同：到达乱序给 seq，语义以 seq 为准
        tf.addTest("手算5: 乱序到达但显式seq决定全序", () -> {
            PatternEngine e = new PatternEngine(CFG_ALL);
            e.ingest("b", "B", 100, 1);
            e.ingest("c", "C", 100, 2);
            e.ingest("a", "A", 100, 0);
            EngineResult r = e.flush();
            assertPairs(tf, r, List.of(pair("a", "b")));
        });

        // -------- 场景 6：迟到事件 DROP/ACCEPT + 重放确定性 --------
        // A@0, A2@2000（wm=2000）后，A@1000 到达 -> 1000 < 2000 迟到
        tf.addTest("手算6: 迟到事件默认 DROP 并计数", () -> {
            PatternEngine e = new PatternEngine(CFG_ALL);
            e.ingest("A1", "A", 0);
            e.ingest("A2", "A", 2000); // wm -> 2000
            e.ingest("LateA", "A", 1000);
            EngineResult r = e.flush();
            TestFramework.assertEquals(1L, r.stats().droppedLate, "丢弃1条迟到");
            TestFramework.assertEquals(0L, r.stats().acceptedLate, "不接受迟到");
            assertTimeoutIds(tf, r, List.of("A1", "A2"));
        });

        tf.addTest("手算6: ACCEPT 迟到A可与后续正常B匹配（标记late）", () -> {
            PatternConfig cfg = CFG_ALL.withLate(LatePolicy.ACCEPT, 0);
            PatternEngine e = new PatternEngine(cfg);
            e.ingest("A1", "A", 0);
            e.ingest("A2", "A", 2000); // wm=2000
            e.ingest("LateA", "A", 1500); // 1500<2000 迟到，进入尽力车道，deadline2500>wm
            e.ingest("B1", "B", 2200); // 正常事件，窗口内候选：LateA(dt700); A2 也在? dt200 -> A2 也候选
            EngineResult r = e.flush();
            // ALL_PAIRS 下 B1 只匹配一次，候选是当时仍活跃的 LateA 与 A2（两者都与B配对）
            assertPairs(tf, r, List.of(pair("LateA", "B1"), pair("A2", "B1")));
            TestFramework.assertTrue(r.matches().get(0).late()
                            || r.matches().get(1).late(),
                    "迟到A参与的匹配带late标记");
            TestFramework.assertEquals(1L, r.stats().acceptedLate, "接受1条迟到");
            assertTimeoutIds(tf, r, List.of("A1"));
        });

        tf.addTest("手算6: 迟到C杀掉现存活跃A，但不撤回已发匹配", () -> {
            PatternConfig cfg = CFG_ALL.withLate(LatePolicy.ACCEPT, 0);
            PatternEngine e = new PatternEngine(cfg);
            e.ingest("A1", "A", 0);
            e.ingest("B1", "B", 500); // 已匹配 (A1,B1)
            e.ingest("A2", "A", 2000); // wm=2000
            e.ingest("A3", "A", 2100);
            e.ingest("LateC", "C", 1500); // 迟到 C：杀全序在它之前的活跃 A（无，A1已匹配）
            EngineResult r = e.flush();
            assertPairs(tf, r, List.of(pair("A1", "B1"))); // 不撤回
            assertTimeoutIds(tf, r, List.of("A2", "A3")); // 迟到C在它们之前但活跃A都在2000+，不杀
        });

        // 重放：reset 后喂入完全相同的日志，结果一致
        tf.addTest("手算6: 重放产生完全一致的结果", () -> {
            PatternEngine e = new PatternEngine(CFG_ALL);
            List<KeyEvent> input = seq(
                    ev("A1", "A", 0), ev("A2", "A", 100), ev("C1", "C", 400),
                    ev("A3", "A", 600), ev("B1", "B", 1000));
            for (KeyEvent ev : input) {
                e.ingest(ev.id(), ev.key(), ev.timestamp());
            }
            EngineResult first = e.flush();
            e.reset();
            for (KeyEvent ev : input) {
                e.ingest(ev.id(), ev.key(), ev.timestamp());
            }
            EngineResult second = e.flush();
            assertSameResult(tf, first, second, "重放结果一致");
            TestFramework.assertEquals(0L, second.stats().duplicates, "reset 后统计清零");
        });

        // -------- 场景 7：重复 ID 忽略 --------
        tf.addTest("手算7: 重复事件ID被忽略并计数", () -> {
            PatternEngine e = new PatternEngine(CFG_ALL);
            e.ingest("A1", "A", 0);
            e.ingest("A1", "A", 50); // 重复
            e.ingest("B1", "B", 500);
            EngineResult r = e.flush();
            assertPairs(tf, r, List.of(pair("A1", "B1")));
            TestFramework.assertEquals(1L, r.stats().duplicates, "1 条重复");
            TestFramework.assertEquals(2L, r.stats().processed, "只处理2条");
        });
    }

    // ----------------------------------------------------------- 辅助

    static EngineResult run(PatternConfig cfg, List<KeyEvent> events) {
        PatternEngine e = new PatternEngine(cfg);
        for (KeyEvent ev : events) {
            e.ingest(ev.id(), ev.key(), ev.timestamp());
        }
        return e.flush();
    }

    static KeyEvent ev(String id, String key, long ts) {
        return new KeyEvent(id, key, ts, 0);
    }

    /** seq 由列表位置决定（模拟到达顺序补号）。 */
    static List<KeyEvent> seq(KeyEvent... events) {
        List<KeyEvent> out = new ArrayList<>();
        for (int i = 0; i < events.length; i++) {
            KeyEvent e = events[i];
            out.add(new KeyEvent(e.id(), e.key(), e.timestamp(), i));
        }
        return out;
    }

    static String pair(String a, String b) { return a + "->" + b; }

    static void assertPairs(TestFramework tf, EngineResult r, List<String> expected) {
        List<String> actual = new ArrayList<>();
        for (Match m : r.matches()) {
            actual.add(m.aId() + "->" + m.bId());
        }
        TestFramework.assertEquals(expected.toString(), actual.toString(), "匹配对(所用事件ID)");
        TestFramework.assertEquals((long) expected.size(), r.stats().matches, "matches 统计");
    }

    static void assertTimeoutIds(TestFramework tf, EngineResult r, List<String> expected) {
        List<String> actual = new ArrayList<>();
        for (Timeout t : r.timeouts()) {
            actual.add(t.aId());
        }
        TestFramework.assertEquals(expected.toString(), actual.toString(), "超时A的ID(发射顺序)");
    }

    static void assertSameResult(TestFramework tf, EngineResult a, EngineResult b, String msg) {
        List<String> ma = a.matches().stream().map(m -> m.aId() + "->" + m.bId()).toList();
        List<String> mb = b.matches().stream().map(m -> m.aId() + "->" + m.bId()).toList();
        TestFramework.assertEquals(ma.toString(), mb.toString(), msg + ": matches");
        List<String> ta = a.timeouts().stream().map(Timeout::aId).toList();
        List<String> tb = b.timeouts().stream().map(Timeout::aId).toList();
        TestFramework.assertEquals(ta.toString(), tb.toString(), msg + ": timeouts");
    }
}
