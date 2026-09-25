package com.example.streammatch.tests;

import com.example.streammatch.AhoCorasick;
import com.example.streammatch.CorpusGenerator;
import com.example.streammatch.Match;
import com.example.streammatch.NaiveMatcher;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static com.example.streammatch.tests.TestFramework.assertTrue;
import static com.example.streammatch.tests.TestFramework.section;

/**
 * 大量共享前缀压力测试：
 * 2000+ 模式共享长前缀，验证 trie 构造、失败链聚合与朴素结果一致；
 * 并刻意覆盖“每个具体编号模式确实能独立命中”。
 */
public final class SharedPrefixTest {

    private SharedPrefixTest() {
    }

    public static void run() {
        section("大量共享前缀", () -> {
            CorpusGenerator.Corpus c = CorpusGenerator.generate("sharedPrefix", 1);
            assertTrue(c.patterns().size() >= 2000,
                    "共享前缀语料含 2000+ 模式，实际 " + c.patterns().size());

            AhoCorasick ac = new AhoCorasick(c.patterns());
            List<Match> got = ac.matchAll(c.text());
            List<Match> ref = NaiveMatcher.match(c.text(), c.patterns(),
                    com.example.streammatch.EmptyPatternPolicy.SKIP);
            assertTrue(canonical(got).equals(canonical(ref)),
                    "2000+ 共享前缀模式：AC 与朴素完全一致（AC=" + got.size()
                            + ", 朴素=" + ref.size() + "）");
            assertTrue(got.size() > 500, "共享前缀语料命中数 >500，实际 " + got.size());

            // 每个具体模式单独构造文本，确认身份不串
            Set<Integer> hitIds = new HashSet<>();
            for (int id = 4; id < c.patterns().size(); id += 37) {
                String p = c.patterns().get(id);
                final int fid = id;
                List<Match> one = ac.matchAll(p);
                boolean self = one.stream().anyMatch(m ->
                        m.patternId() == fid && m.start() == 0
                                && m.end() == p.codePointCount(0, p.length()));
                assertTrue(self, "模式 ID=" + fid + " 对自身文本独立命中");
                one.forEach(m -> hitIds.add(m.patternId()));
            }
            assertTrue(hitIds.size() > 50, "抽检覆盖 " + hitIds.size() + " 个不同模式身份");

            // 极短前缀模式 "pr3" 与 "pr3fix" 在每处长词上同时命中（重叠）
            List<Match> local = ac.matchAll("pr3fix/0000/0000000");
            boolean pr3 = local.stream().anyMatch(m -> m.pattern().equals("pr3"));
            boolean stem = local.stream().anyMatch(m -> m.pattern().equals("pr3fix"));
            assertTrue(pr3 && stem, "长词位置同时命中短前缀模式（重叠输出链）");

            // 流式小块同样一致
            List<Match> streamed = new ArrayList<>();
            com.example.streammatch.StreamMatcher sm =
                    new com.example.streammatch.StreamMatcher(ac);
            String text = c.text();
            for (int i = 0; i < text.length(); i += 13) {
                sm.feedChars(text.substring(i, Math.min(text.length(), i + 13)));
                streamed.addAll(sm.drainMatches());
            }
            streamed.addAll(sm.finish());
            assertTrue(canonical(streamed).equals(canonical(ref)),
                    "共享前缀语料 char 块=13 流式结果与朴素一致");
        });
    }

    private static List<Match> canonical(List<Match> ms) {
        List<Match> copy = new ArrayList<>(ms);
        copy.sort(Match.canonicalOrder());
        return copy;
    }
}
