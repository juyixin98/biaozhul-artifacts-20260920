package com.example.streammatch.tests;

import com.example.streammatch.AhoCorasick;
import com.example.streammatch.EmptyPatternPolicy;
import com.example.streammatch.Match;
import com.example.streammatch.NaiveMatcher;
import com.example.streammatch.StreamMatcher;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

import static com.example.streammatch.tests.TestFramework.assertEquals;
import static com.example.streammatch.tests.TestFramework.assertTrue;
import static com.example.streammatch.tests.TestFramework.section;

/**
 * Unicode 边界专项：
 * 星面层字符（代理对）、区域指示符、ZWJ 序列、组合字符、
 * 在任意 char 边界与任意 UTF-8 字节边界切开，位置以码点计。
 */
public final class UnicodeTest {

    private UnicodeTest() {
    }

    private static final List<String> PATTERNS = List.of(
            "😀", "日本", "👨‍👩‍👧", "é", "a", "ab"
    );

    private static final List<String> TEXTS = List.of(
            "😀日本a",
            "a😀b",                 // emoji 在中间，验证位置按码点而非 char
            "👨‍👩‍👧",            // ZWJ 家庭序列
            "café é x",        // 预组合 é 与分解 e+◌́
            "🇯🇵😀日本語",
            "a" + "\uD83D" + "\uDE00" + "ab", // 显式代理对拼接
            "\uD83D",               // 孤立高代理
            "x\uDE00y"              // 孤立低代理
    );

    public static void run() {
        section("Unicode 边界（代理对/组合字符/字节切断）", () -> {
            for (String text : TEXTS) {
                int cpCount = text.codePointCount(0, text.length());
                List<Match> ref = NaiveMatcher.match(text, PATTERNS, EmptyPatternPolicy.SKIP);

                // 码点位置在所有切分方式下一致
                for (int cpChunk = 1; cpChunk <= 5; cpChunk++) {
                    List<Match> got = feedByCodePoints(text, cpChunk);
                    assertTrue(canonical(got).equals(canonical(ref)),
                            "码点块=" + cpChunk + " 文本码点数=" + cpCount
                                    + " char数=" + text.length() + " 一致");
                }
                // 按 Java char 切（可切代理对）
                for (int chChunk = 1; chChunk <= 5; chChunk++) {
                    AhoCorasick ac = new AhoCorasick(PATTERNS);
                    StreamMatcher sm = new StreamMatcher(ac);
                    List<Match> all = new ArrayList<>();
                    for (int i = 0; i < text.length(); i += chChunk) {
                        sm.feedChars(text.substring(i, Math.min(text.length(), i + chChunk)));
                        all.addAll(sm.drainMatches());
                    }
                    all.addAll(sm.finish());
                    assertTrue(canonical(all).equals(canonical(ref)),
                            "char块=" + chChunk + "（可切代理对）文本码点数=" + cpCount + " 一致");
                }
                // 按 UTF-8 字节切（可切多字节序列）
                byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
                for (int bChunk : new int[]{1, 2, 3, 4, 5, 13}) {
                    AhoCorasick ac = new AhoCorasick(PATTERNS);
                    StreamMatcher sm = new StreamMatcher(ac);
                    List<Match> all = new ArrayList<>();
                    for (int off = 0; off < bytes.length; off += bChunk) {
                        sm.feedBytes(bytes, off, Math.min(bChunk, bytes.length - off), true);
                        all.addAll(sm.drainMatches());
                    }
                    all.addAll(sm.finish());
                    assertTrue(canonical(all).equals(canonical(ref)),
                            "字节块=" + bChunk + "（共 " + bytes.length + " 字节）一致");
                }
            }

            // 位置是码点偏移而非 char 偏移："a😀b" 中 'b' 的码点位置是 2，char 位置是 3
            AhoCorasick ac = new AhoCorasick(List.of("b"));
            List<Match> ms = ac.matchAll("a😀b");
            assertEquals(1, ms.size(), "a😀b 中 b 命中一次");
            assertEquals(2, ms.get(0).start(), "b 的位置按码点计为 2（非 char 的 3）");

            // emoji 自身命中位置："x😀" -> 😀 在码点 1
            List<Match> e = new AhoCorasick(List.of("😀")).matchAll("x😀");
            assertEquals(1, e.get(0).start(), "😀 在 x😀 中的码点位置为 1");
            assertEquals(2, e.get(0).end(), "😀 占 1 个码点，end=2");

            // 各块“片段命中”汇总后位置全部不越界
            String longText = "日本語😀".repeat(50);
            int totalCp = longText.codePointCount(0, longText.length());
            List<Match> all = feedByBytes(longText, 3);
            assertTrue(all.stream().allMatch(m ->
                    m.start() >= 0 && m.end() <= totalCp), "所有命中位置在 [0," + totalCp + "] 内");
        });
    }

    private static List<Match> feedByCodePoints(String text, int chunk) {
        AhoCorasick ac = new AhoCorasick(PATTERNS);
        StreamMatcher sm = new StreamMatcher(ac);
        List<Match> all = new ArrayList<>();
        int total = text.codePointCount(0, text.length());
        int[] cps = com.example.streammatch.AhoCorasick.toCodePoints(text);
        for (int i = 0; i < total; i += chunk) {
            int len = Math.min(chunk, total - i);
            sm.feedCodePoints(cps, i, len);
            all.addAll(sm.drainMatches());
        }
        all.addAll(sm.finish());
        return all;
    }

    private static List<Match> feedByBytes(String text, int chunk) {
        AhoCorasick ac = new AhoCorasick(PATTERNS);
        StreamMatcher sm = new StreamMatcher(ac);
        byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
        List<Match> all = new ArrayList<>();
        for (int off = 0; off < bytes.length; off += chunk) {
            sm.feedBytes(bytes, off, Math.min(chunk, bytes.length - off), true);
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
