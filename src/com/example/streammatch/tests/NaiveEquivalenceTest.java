package com.example.streammatch.tests;

import com.example.streammatch.AhoCorasick;
import com.example.streammatch.EmptyPatternPolicy;
import com.example.streammatch.Match;
import com.example.streammatch.NaiveMatcher;
import com.example.streammatch.StreamMatcher;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;

import static com.example.streammatch.tests.TestFramework.assertTrue;
import static com.example.streammatch.tests.TestFramework.section;

/**
 * 验收核心：随机化差分测试（differential/fuzz testing）。
 *
 * <p>数千组随机“文本 × 模式表 × 分块大小 × 喂入方式（char/码点/UTF-8 字节）× 空策略”，
 * 要求流式 AC 累积的<b>全部块位置</b>与朴素匹配逐条相等（规范化后）。</p>
 */
public final class NaiveEquivalenceTest {

    private NaiveEquivalenceTest() {
    }

    public static void run() {
        section("差分测试：全部块位置 == 朴素匹配", () -> {
            Random rnd = new Random(20260924L);

            // 字母表：ASCII + BMP + 星面层，刻意包含会形成共享前缀与空串的模式
            int[] alphabet = {'a', 'b', 'c', 'h', 'e', 's', 'r', 'i', 'x',
                    '中', '日', 0x1F600, 0x1F1EF, 0x1F1F5};

            int cases = 0;
            for (int iter = 0; iter < 4000; iter++) {
                int textCpLen = 1 + rnd.nextInt(60);
                StringBuilder text = new StringBuilder();
                for (int i = 0; i < textCpLen; i++) {
                    text.appendCodePoint(alphabet[rnd.nextInt(alphabet.length)]);
                }
                // 每 50 组构造一个空文本边界
                String t = (iter % 50 == 49) ? "" : text.toString();

                int pn = 1 + rnd.nextInt(8);
                List<String> patterns = new ArrayList<>(pn);
                for (int pi = 0; pi < pn; pi++) {
                    int roll = rnd.nextInt(20);
                    if (roll == 0) {
                        patterns.add(""); // 5% 空模式
                    } else if (roll == 1 && !t.isEmpty()) {
                        // 5% 从文本随机截一段做模式，保证真实命中且含 Unicode
                        int total = t.codePointCount(0, t.length());
                        int s = rnd.nextInt(total);
                        int e = Math.min(total, s + 1 + rnd.nextInt(6));
                        int jS = t.offsetByCodePoints(0, s);
                        int jE = t.offsetByCodePoints(0, e);
                        patterns.add(t.substring(jS, jE));
                    } else {
                        int len = 1 + rnd.nextInt(6);
                        StringBuilder pb = new StringBuilder();
                        for (int k = 0; k < len; k++) {
                            pb.appendCodePoint(alphabet[rnd.nextInt(alphabet.length)]);
                        }
                        patterns.add(pb.toString());
                    }
                    // 10% 概率复制上一个模式（重复模式独立身份）
                    if (patterns.size() >= 2 && rnd.nextInt(10) == 0) {
                        patterns.add(patterns.get(patterns.size() - 1));
                    }
                }
                EmptyPatternPolicy policy = patterns.contains("")
                        ? EmptyPatternPolicy.values()[rnd.nextInt(3)]
                        : EmptyPatternPolicy.SKIP;

                List<Match> ref = NaiveMatcher.match(t, patterns, policy);

                // 随机选喂入方式与块大小
                int mode = rnd.nextInt(3);
                int chunk = 1 + rnd.nextInt(7);
                List<Match> got;
                AhoCorasick ac = new AhoCorasick(patterns, policy);
                if (mode == 0) {
                    got = feedCodePoints(ac, t, chunk);
                } else if (mode == 1) {
                    got = feedChars(ac, t, chunk);
                } else {
                    got = feedBytes(ac, t, chunk);
                }

                if (!canonical(got).equals(canonical(ref))) {
                    assertTrue(false, "差分不一致 iter=" + iter + " mode=" + mode
                            + " chunk=" + chunk + " policy=" + policy
                            + " textCp=" + t.codePointCount(0, t.length())
                            + " patterns=" + patterns
                            + " got=" + got.size() + " ref=" + ref.size());
                    return;
                }
                cases++;
            }
            assertTrue(cases > 0, "差分测试实际执行 " + cases + " 组");
            System.out.println("  （" + cases + " 组随机用例全部与朴素匹配一致）");

            // 极端：超大分块（一次喂完）与块大小 1 必须一致
            List<String> ps = List.of("ab", "ba", "aba", "");
            String t = "abababa".repeat(100);
            AhoCorasick ac1 = new AhoCorasick(ps, EmptyPatternPolicy.BEFORE);
            AhoCorasick ac2 = new AhoCorasick(ps, EmptyPatternPolicy.BEFORE);
            List<Match> big = feedChars(ac1, t, 1_000_000);
            List<Match> one = feedChars(ac2, t, 1);
            assertTrue(canonical(big).equals(canonical(one)),
                    "超大块（一次喂完）与逐字符喂入结果一致");
            assertTrue(canonical(one).equals(
                    canonical(NaiveMatcher.match(t, ps, EmptyPatternPolicy.BEFORE))),
                    "BEFORE 策略长文本与朴素一致");
        });
    }

    private static List<Match> feedCodePoints(AhoCorasick ac, String t, int chunk) {
        StreamMatcher sm = new StreamMatcher(ac);
        int[] cps = AhoCorasick.toCodePoints(t);
        List<Match> all = new ArrayList<>();
        for (int i = 0; i < cps.length; i += chunk) {
            int len = Math.min(chunk, cps.length - i);
            sm.feedCodePoints(cps, i, len);
            all.addAll(sm.drainMatches());
        }
        all.addAll(sm.finish());
        return all;
    }

    private static List<Match> feedChars(AhoCorasick ac, String t, int chunk) {
        StreamMatcher sm = new StreamMatcher(ac);
        List<Match> all = new ArrayList<>();
        for (int i = 0; i < t.length(); i += chunk) {
            sm.feedChars(t.substring(i, Math.min(t.length(), i + chunk)));
            all.addAll(sm.drainMatches());
        }
        all.addAll(sm.finish());
        return all;
    }

    private static List<Match> feedBytes(AhoCorasick ac, String t, int chunk) {
        StreamMatcher sm = new StreamMatcher(ac);
        byte[] b = t.getBytes(StandardCharsets.UTF_8);
        List<Match> all = new ArrayList<>();
        for (int i = 0; i < b.length; i += chunk) {
            sm.feedBytes(b, i, Math.min(chunk, b.length - i), true);
            all.addAll(sm.drainMatches());
        }
        all.addAll(sm.finish());
        return all;
    }

    private static List<Match> canonical(List<Match> ms) {
        List<Match> copy = new ArrayList<>(ms);
        copy.sort(Match.canonicalOrder());
        return copy;
    }
}
