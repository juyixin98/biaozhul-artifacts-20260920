package com.example.ac.test;

import com.example.ac.Compiled;
import com.example.ac.EmptyPatternPolicy;
import com.example.ac.Engine;
import com.example.ac.Match;
import com.example.ac.NaiveMatcher;
import com.example.ac.Pattern;
import com.example.ac.StreamingMatcher;

import java.util.ArrayList;
import java.util.List;

/**
 * 大量共享前缀与重叠模式的压力/正确性测试：
 * a^1..a^K、a^k b 系列全部共享 Trie 前缀；在长 a-run 上产生大量重叠命中。
 */
public class SharedPrefixTest extends TestCase {

    public SharedPrefixTest() {
        super("large-shared-prefix");
    }

    @Override
    protected void run() {
        int K = 400;
        List<Pattern> pats = new ArrayList<>();
        for (int k = 1; k <= K; k++) {
            pats.add(Pattern.of("a" + k, "a".repeat(k)));
        }
        // 再加 200 个 a^k c 共享前缀但不同终止分支
        for (int k = 1; k <= 200; k++) {
            pats.add(Pattern.of("ac" + k, "a".repeat(k) + "c"));
        }
        Compiled c = Engine.compile(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION);

        String text = "a".repeat(300) + "c" + "a".repeat(500) + "c";
        List<Match> oracle = NaiveMatcher.match(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION, text);
        List<Match> got = Engine.match(c, text);
        eq(got, oracle, "large shared-prefix matches naive");

        // a^k 模式（1..300）在前 300 个 a 的每个结束位置 i 上，
        // 命中 k=1..min(i,300)，共 300*301/2 = 45150 条 a* 命中（仅第一个 a-run）。
        long aHits = got.stream().filter(m -> m.matched().matches("a+")).count();
        long expectedA = 0;
        // 第一个 run：长度 300；第二个 run：长度 500，a^k 仅 k<=400 存在
        for (int end = 1; end <= 300; end++) {
            expectedA += Math.min(end, K);
        }
        for (int end = 302; end <= 301 + 500; end++) {
            int runLen = end - 301;
            expectedA += Math.min(runLen, K);
        }
        eq(aHits, expectedA, "a^k overlap count");
        check(aHits > 100_000, "dense overlap output actually produced, got " + aHits);

        // a^k c 命中：在两个 c 位置，分别 k=300（存在 k<=200 时不命中 300c 因 max k=200），
        // 只有 k<=200 且 runLen>=k：第一个 run 长度300 → k=1..200 全命中；
        // 第二个 run 长度500 → k=1..200 全命中 → 共 400。
        long acHits = got.stream().filter(m -> m.matched().endsWith("c")).count();
        eq(acHits, 400L, "a^k c hits at both c positions");

        // 分块（每块 37 个 code point，刻意不整除）结果一致
        StreamingMatcher sm = new StreamingMatcher(c);
        List<Match> chunked = new ArrayList<>();
        int[] cps = text.codePoints().toArray();
        for (int begin = 0; begin < cps.length; begin += 37) {
            sm.feed(new String(cps, begin, Math.min(37, cps.length - begin)), chunked);
        }
        chunked.addAll(sm.finish());
        eq(chunked, oracle, "chunked large shared-prefix equals whole");

        // 顺序检查：规范事件序必须全局有序（end,start,patternIndex）
        Match prev = null;
        for (Match m : chunked) {
            if (prev != null) {
                check(prev.compareTo(m) <= 0, "global event order violated: " + prev + " then " + m);
            }
            prev = m;
        }

        // 2000 个随机共享前缀模式规模冒烟
        List<Pattern> many = new ArrayList<>();
        long seed = 42;
        for (int i = 0; i < 2000; i++) {
            seed = (seed * 6364136223846793005L + 1442695040888963407L) & Long.MAX_VALUE;
            int len = 2 + (int) (seed % 8);
            StringBuilder sb = new StringBuilder();
            for (int j = 0; j < len; j++) {
                seed = (seed * 6364136223846793005L + 1442695040888963407L) & Long.MAX_VALUE;
                sb.append((char) ('a' + seed % 3));
            }
            many.add(Pattern.of("r" + i, sb.toString()));
        }
        Compiled c2 = Engine.compile(many, EmptyPatternPolicy.MATCH_EVERY_POSITION);
        String t2 = "abcabc".repeat(200);
        long t0 = System.nanoTime();
        List<Match> g2 = Engine.match(c2, t2);
        long millis = (System.nanoTime() - t0) / 1_000_000;
        List<Match> o2 = NaiveMatcher.match(many, EmptyPatternPolicy.MATCH_EVERY_POSITION, t2);
        eq(g2, o2, "2000 patterns correctness");
        check(millis < 10_000, "2000 patterns on 1200 chars under 10s, took " + millis + "ms");
    }
}
