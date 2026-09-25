package com.example.streammatch.tests;

import com.example.streammatch.AhoCorasick;
import com.example.streammatch.CorpusGenerator;
import com.example.streammatch.EmptyPatternPolicy;
import com.example.streammatch.Match;
import com.example.streammatch.NaiveMatcher;
import com.example.streammatch.StreamMatcher;

import java.util.ArrayList;
import java.util.List;

import static com.example.streammatch.tests.TestFramework.assertTrue;
import static com.example.streammatch.tests.TestFramework.section;

/**
 * 性能与规模测试。
 *
 * <p>朴素参照是 O(n·m) 的刻意简单实现，在 2000 模式 × 20 万码点规模下本来就应该很慢——
 * 这正是 AC 存在的理由。因此这里：</p>
 * <ul>
 *   <li>全量规模：验证 AC 一次性匹配与分块流式结果一致且足够快；</li>
 *   <li>前缀规模（默认 20000 码点）：三方（一次性 AC / 流式 AC / 朴素）逐条核对，
 *       并断言 AC 不慢于朴素。</li>
 * </ul>
 */
public final class PerfTest {

    /** 朴素核对使用的前缀码点数。 */
    private static final int NAIVE_PREFIX = 20_000;

    private PerfTest() {
    }

    public static void run() {
        section("性能 / 规模（AC 全量 + 前缀朴素对照）", () -> {
            benchmark("sharedPrefix", 7, 16);
            benchmark("randomDna", 3, 16);
        });
    }

    private static void benchmark(String name, long seed, int chunk) {
        CorpusGenerator.Corpus c = CorpusGenerator.generate(name, seed);
        String text = c.text();
        int textCp = text.codePointCount(0, text.length());

        long t0 = System.nanoTime();
        AhoCorasick ac = new AhoCorasick(c.patterns(), EmptyPatternPolicy.SKIP);
        long buildMs = (System.nanoTime() - t0) / 1_000_000;

        long t1 = System.nanoTime();
        List<Match> oneShot = ac.matchAll(text);
        long acMs = (System.nanoTime() - t1) / 1_000_000;

        long t2 = System.nanoTime();
        StreamMatcher sm = new StreamMatcher(ac);
        List<Match> streamed = new ArrayList<>();
        int jStart = 0;
        for (int s = 0; s < textCp; s += chunk) {
            int e = Math.min(textCp, s + chunk);
            int jEnd = text.offsetByCodePoints(jStart, e - s);
            sm.feedChars(text.substring(jStart, jEnd));
            streamed.addAll(sm.drainMatches());
            jStart = jEnd;
        }
        streamed.addAll(sm.finish());
        long streamMs = (System.nanoTime() - t2) / 1_000_000;

        boolean fullCorrect = canonical(oneShot).equals(canonical(streamed));

        // 前缀三方对照
        int prefixEndChar = text.offsetByCodePoints(0, Math.min(NAIVE_PREFIX, textCp));
        String prefix = text.substring(0, prefixEndChar);
        long t3 = System.nanoTime();
        List<Match> preAc = ac.matchAll(prefix);
        long preAcMs = (System.nanoTime() - t3) / 1_000_000;

        long t4 = System.nanoTime();
        List<Match> preNaive = NaiveMatcher.match(prefix, c.patterns(), EmptyPatternPolicy.SKIP);
        long preNaiveMs = (System.nanoTime() - t4) / 1_000_000;

        List<Match> preStream = new ArrayList<>();
        StreamMatcher sm2 = new StreamMatcher(ac);
        int preCp = prefix.codePointCount(0, prefix.length());
        int pj = 0;
        for (int s = 0; s < preCp; s += chunk) {
            int e = Math.min(preCp, s + chunk);
            int pjEnd = prefix.offsetByCodePoints(pj, e - s);
            sm2.feedChars(prefix.substring(pj, pjEnd));
            preStream.addAll(sm2.drainMatches());
            pj = pjEnd;
        }
        preStream.addAll(sm2.finish());

        boolean prefixCorrect = canonical(preAc).equals(canonical(preNaive))
                && canonical(preStream).equals(canonical(preNaive));

        System.out.printf("  %-13s 模式=%-5d 文本=%-7d码点 构造=%-5dms AC全量=%-5dms 流式=%-5dms 命中=%-7d | 前缀%d码点: AC=%-4dms 朴素=%-6dms 前缀命中=%d%n",
                name, c.patterns().size(), textCp, buildMs, acMs, streamMs, oneShot.size(),
                preCp, preAcMs, preNaiveMs, preNaive.size());

        assertTrue(fullCorrect, name + ": 全量 AC 一次性与分块流式一致");
        assertTrue(prefixCorrect, name + ": 前缀上 AC/流式/朴素三方一致");
        assertTrue(buildMs < 5000, name + ": trie 构造在 5s 内，实际 " + buildMs + "ms");
        assertTrue(acMs < 5000, name + ": 全量 AC 匹配在 5s 内，实际 " + acMs + "ms");
        assertTrue(preAcMs <= preNaiveMs,
                name + ": 前缀上 AC 不慢于朴素（AC=" + preAcMs + "ms, 朴素=" + preNaiveMs + "ms）");
    }

    private static List<Match> canonical(List<Match> ms) {
        List<Match> copy = new ArrayList<>(ms);
        copy.sort(Match.canonicalOrder());
        return copy;
    }
}
