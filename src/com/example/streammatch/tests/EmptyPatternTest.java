package com.example.streammatch.tests;

import com.example.streammatch.AhoCorasick;
import com.example.streammatch.EmptyPatternPolicy;
import com.example.streammatch.Match;
import com.example.streammatch.NaiveMatcher;
import com.example.streammatch.StreamMatcher;

import java.util.ArrayList;
import java.util.List;

import static com.example.streammatch.tests.TestFramework.assertEquals;
import static com.example.streammatch.tests.TestFramework.assertTrue;
import static com.example.streammatch.tests.TestFramework.section;

/**
 * 空模式策略专项：
 * SKIP 不产出；BEFORE/AFTER 在长度 n 的流上产出 n+1 个位置；重复空模式独立计数；
 * 流的最终集合与朴素一致，且逐块喂入时各空命中恰好在语义规定的时机产出。
 */
public final class EmptyPatternTest {

    private EmptyPatternTest() {
    }

    public static void run() {
        section("空模式策略", () -> {
            List<String> patterns = List.of("", "a", ""); // 两个空模式：ID 0、2

            // SKIP
            AhoCorasick skip = new AhoCorasick(patterns, EmptyPatternPolicy.SKIP);
            assertTrue(skip.matchAll("abc").stream().noneMatch(m -> m.end() == m.start()),
                    "SKIP 不产出任何空命中");
            assertTrue(skip.matchAll("").isEmpty(), "SKIP 空流无命中");

            for (EmptyPatternPolicy policy : new EmptyPatternPolicy[]{
                    EmptyPatternPolicy.BEFORE, EmptyPatternPolicy.AFTER}) {

                // n 个码点 → 每个空模式 n+1 命中，两个空模式共 2(n+1)
                for (String text : new String[]{"", "a", "abc", "a😀b"}) {
                    int n = text.codePointCount(0, text.length());
                    List<Match> ref = NaiveMatcher.match(text, patterns, policy);
                    assertEquals(2 * (n + 1) + (text.contains("a") ? 1 : 0),
                            ref.size(), policy + " 朴素命中数（文本<" + text + ">）");

                    // 一次性
                    List<Match> one = new AhoCorasick(patterns, policy).matchAll(text);
                    assertTrue(canonical(one).equals(canonical(ref)),
                            policy + " 一次性匹配与朴素一致（文本<" + text + ">）");

                    // 逐码点分块
                    for (int chunk : new int[]{1, 2, 3}) {
                        AhoCorasick ac = new AhoCorasick(patterns, policy);
                        StreamMatcher sm = new StreamMatcher(ac);
                        List<Match> all = new ArrayList<>();
                        int total = text.codePointCount(0, text.length());
                        int[] cps = AhoCorasick.toCodePoints(text);
                        for (int i = 0; i < total; i += chunk) {
                            int len = Math.min(chunk, total - i);
                            sm.feedCodePoints(cps, i, len);
                            all.addAll(sm.drainMatches());
                        }
                        all.addAll(sm.finish());
                        assertTrue(canonical(all).equals(canonical(ref)),
                                policy + " 分块=" + chunk + " 最终集合与朴素一致（文本<" + text + ">）");
                    }
                }

                // 时机检查：逐码点喂入，每批空命中位置必须符合策略定义
                AhoCorasick ac = new AhoCorasick(patterns, policy);
                StreamMatcher sm = new StreamMatcher(ac);
                sm.feedCodePoint('x'); // 第 0 个码点
                List<Match> batch0 = sm.drainMatches();
                List<Match> empties0 = batch0.stream().filter(m -> m.end() == m.start()).toList();
                if (policy == EmptyPatternPolicy.BEFORE) {
                    // 消费码点前：位置 0 的两个空命中（真实命中 x 不存在）
                    assertEquals(2, empties0.size(), "BEFORE 第一批恰有 2 个位置 0 空命中");
                    assertTrue(empties0.stream().allMatch(m -> m.start() == 0),
                            "BEFORE 第一批空命中都在位置 0");
                } else {
                    // AFTER：先位置 0 两个，再消费码点，再位置 1 两个
                    assertEquals(4, empties0.size(), "AFTER 第一批含位置 0、1 各 2 个空命中");
                    assertEquals(0, empties0.get(0).start(), "AFTER 先产出位置 0");
                    assertEquals(1, empties0.get(empties0.size() - 1).start(),
                            "AFTER 后产出位置 1");
                }

                List<Match> tail = sm.finish();
                List<Match> tailEmpties = tail.stream().filter(m -> m.end() == m.start()).toList();
                if (policy == EmptyPatternPolicy.BEFORE) {
                    // finish 时补齐位置 n=1
                    assertEquals(2, tailEmpties.size(), "BEFORE finish 补位置 1 的两个空命中");
                    assertTrue(tailEmpties.stream().allMatch(m -> m.start() == 1),
                            "BEFORE finish 空命中在位置 1");
                } else {
                    // 位置 1 已随码点产出，finish 无新增
                    assertTrue(tailEmpties.isEmpty(), "AFTER finish 无新增空命中");
                }

                // 空流：BEFORE 与 AFTER 都应在 finish 时给出位置 0 的两个空命中
                StreamMatcher esm = new StreamMatcher(new AhoCorasick(patterns, policy));
                List<Match> er = esm.finish();
                assertEquals(2, er.size(), policy + " 空流 finish 产出 2 个位置 0 空命中");
                assertTrue(er.stream().allMatch(m -> m.start() == 0 && m.end() == 0),
                        policy + " 空流空命中在位置 0");
            }

            // 字节块风格（会切断多字节字符）× 各空策略：最终集合仍与朴素一致
            List<String> uniPatterns = List.of("", "a", "😀", "");
            String uniText = "a😀b 日本😀a";
            byte[] uniBytes = uniText.getBytes(java.nio.charset.StandardCharsets.UTF_8);
            for (EmptyPatternPolicy policy : EmptyPatternPolicy.values()) {
                for (int bChunk : new int[]{1, 2, 3, 4, 7}) {
                    StreamMatcher bsm = new StreamMatcher(new AhoCorasick(uniPatterns, policy));
                    List<Match> got = new ArrayList<>();
                    for (int off = 0; off < uniBytes.length; off += bChunk) {
                        bsm.feedChunkBytes(uniBytes, off,
                                Math.min(bChunk, uniBytes.length - off), true);
                        got.addAll(bsm.drainMatches());
                    }
                    got.addAll(bsm.finish());
                    List<Match> ref = NaiveMatcher.match(uniText, uniPatterns, policy);
                    assertTrue(canonical(got).equals(canonical(ref)),
                            "字节块风格 块=" + bChunk + " 策略=" + policy + " 与朴素一致");
                }
            }

            // 默认策略为 SKIP
            assertEquals(EmptyPatternPolicy.SKIP,
                    new AhoCorasick(patterns).emptyPatternPolicy(),
                    "默认 EmptyPatternPolicy 是 SKIP");
        });
    }

    private static List<Match> canonical(List<Match> ms) {
        List<Match> copy = new ArrayList<>(ms);
        copy.sort(Match.canonicalOrder());
        return copy;
    }
}
