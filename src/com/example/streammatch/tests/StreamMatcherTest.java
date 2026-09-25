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

/** 流式增量喂入：任意分块大小、char 边界、drain 增量与全量一致。 */
public final class StreamMatcherTest {

    private StreamMatcherTest() {
    }

    public static void run() {
        section("StreamMatcher 跨块（码点/char 边界）", () -> {
            List<String> patterns = List.of("he", "she", "his", "hers", "abc", "bc");

            for (String text : List.of("ushers", "abc", "abcabc", "h", "s", "xxhisher",
                    "shers", "hishers", "aabbccabc", "")) {
                List<Match> naive = NaiveMatcher.match(text, patterns, EmptyPatternPolicy.SKIP);
                for (int chunk = 1; chunk <= 9; chunk++) {
                    AhoCorasick ac = new AhoCorasick(patterns);
                    List<Match> got = feedByChars(ac, text, chunk);
                    assertTrue(NaiveMatcher.match(text, patterns, EmptyPatternPolicy.SKIP).size() == got.size()
                                    && canonical(got).equals(canonical(naive)),
                            "文本 <" + text + "> char 块大小 " + chunk + " 与朴素一致");
                }
            }

            // drainMatches 增量语义：每批命中位置都正确，且不重复
            AhoCorasick ac = new AhoCorasick(List.of("a"));
            StreamMatcher sm = new StreamMatcher(ac);
            sm.feedChars("a");
            List<Match> b1 = sm.drainMatches();
            sm.feedChars("xa");
            List<Match> b2 = sm.drainMatches();
            sm.finish();
            assertEquals(1, b1.size(), "第一批 1 个命中");
            assertEquals(1, b2.size(), "第二批 1 个命中");
            assertEquals(0, b1.get(0).start(), "第一批命中位置 0");
            assertEquals(2, b2.get(0).start(), "第二批命中全局位置 2（不受分块影响）");
            assertTrue(sm.drainMatches().isEmpty(), "finish 后无新增命中时 drain 为空");

            // finish 幂等
            sm.finish();
            assertTrue(sm.drainMatches().isEmpty(), "重复 finish 不重复产出");

            // finish 后继续喂入应报错
            boolean threw = false;
            try {
                sm.feedChars("a");
            } catch (IllegalStateException e) {
                threw = true;
            }
            assertTrue(threw, "finish 后再喂入抛 IllegalStateException");

            // 空流 finish
            StreamMatcher empty = new StreamMatcher(new AhoCorasick(List.of("a")));
            List<Match> er = empty.finish();
            assertTrue(er.isEmpty(), "无空模式时空流 finish 无命中");

            // 跨块状态恢复：模式被切成三段喂入
            AhoCorasick split = new AhoCorasick(List.of("abcdef"));
            StreamMatcher sm2 = new StreamMatcher(split);
            sm2.feedChars("ab");
            sm2.feedChars("cd");
            sm2.feedChars("ef");
            List<Match> r = sm2.finish();
            assertEquals(1, r.size(), "模式 abcdef 跨三块喂入后命中一次");
            assertEquals(0, r.get(0).start(), "跨块命中起点为 0");
            assertEquals(6, r.get(0).end(), "跨块命中终点为 6");

            // UTF-8 字节流，按 1 字节切（每个多字节字符必被切断）
            List<String> up = List.of("ab", "中", "😀");
            String ut = "ab中😀ab";
            byte[] bytes = ut.getBytes(StandardCharsets.UTF_8);
            for (int bs : new int[]{1, 2, 3, 4, 7, 100}) {
                AhoCorasick uac = new AhoCorasick(up);
                StreamMatcher usm = new StreamMatcher(uac);
                List<Match> all = new ArrayList<>();
                for (int off = 0; off < bytes.length; off += bs) {
                    usm.feedBytes(bytes, off, Math.min(bs, bytes.length - off), true);
                    all.addAll(usm.drainMatches());
                }
                all.addAll(usm.finish());
                List<Match> ref = NaiveMatcher.match(ut, up, EmptyPatternPolicy.SKIP);
                assertTrue(canonical(all).equals(canonical(ref)),
                        "UTF-8 字节块大小 " + bs + " 与朴素一致（共 " + ref.size() + " 命中）");
            }

            // 流结束时残留不完整 UTF-8 序列应报错
            byte[] broken = "中".getBytes(StandardCharsets.UTF_8); // E4 B8 AD
            StreamMatcher bsm = new StreamMatcher(new AhoCorasick(List.of("a")));
            bsm.feedBytes(new byte[]{broken[0]}, 0, 1, true);
            boolean badFinish = false;
            try {
                bsm.finish();
            } catch (IllegalStateException e) {
                badFinish = true;
            }
            assertTrue(badFinish, "流尾残留不完整 UTF-8 字节时 finish 抛异常");

            // 畸形 UTF-8 立即报错（strict）
            StreamMatcher msm = new StreamMatcher(new AhoCorasick(List.of("a")));
            boolean badInput = false;
            try {
                msm.feedBytes(new byte[]{(byte) 0xFF, (byte) 0xFE}, 0, 2, true);
            } catch (IllegalArgumentException e) {
                badInput = true;
            }
            assertTrue(badInput, "非法 UTF-8 前导字节抛 IllegalArgumentException");
        });
    }

    static List<Match> feedByChars(AhoCorasick ac, String text, int chunk) {
        StreamMatcher sm = new StreamMatcher(ac);
        List<Match> all = new ArrayList<>();
        for (int i = 0; i < text.length(); i += chunk) {
            String piece = text.substring(i, Math.min(text.length(), i + chunk));
            sm.feedChars(piece);
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
