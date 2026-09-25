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
 * 空模式的三种策略，重点是 MATCH_EVERY_POSITION 下“每一边界一次”的语义：
 * 长度 n 的文本产生 n+1 个位置；任意分块方式拼起来都应与整体匹配完全一致。
 */
public class EmptyPatternTest extends TestCase {

    public EmptyPatternTest() {
        super("empty-pattern-policy");
    }

    @Override
    protected void run() {
        List<Pattern> pats = TestUtils.patterns("ab", "", "a");

        // 1) 整体匹配，空文本 → 仅位置 0 一条
        Compiled c = Engine.compile(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION);
        List<Match> whole = Engine.match(c, "");
        eq(whole.size(), 1, "empty text single boundary");
        eq(whole.get(0).start(), 0, "boundary 0 start");
        eq(whole.get(0).end(), 0, "boundary 0 end");
        eq(whole.get(0).patternId(), "p1", "it is the empty pattern");

        // 2) 非空文本 ab：位置 0,1,2 各一条空命中，加上 a@0-1、ab@0-2
        whole = Engine.match(c, "ab");
        long empties = whole.stream().filter(m -> m.length() == 0).count();
        eq(empties, 3L, "n+1 = 3 zero-length hits");
        // 空命中位置集合
        List<Integer> emptyPos = whole.stream().filter(m -> m.length() == 0).map(Match::start).toList();
        eq(emptyPos, List.of(0, 1, 2), "boundary positions 0..n");

        // 3) 与朴素 oracle 一致
        eq(whole, NaiveMatcher.match(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION, "ab"),
                "AC equals naive for empty patterns");

        // 4) 任意分块：按各种块大小喂，结果必须等于整体
        String text = "abracadabra-ab";
        List<Match> wholeText = Engine.match(c, text);
        for (int chunk = 1; chunk <= text.length() + 2; chunk++) {
            StreamingMatcher sm = new StreamingMatcher(c);
            List<Match> chunked = new ArrayList<>();
            for (int begin = 0; begin < text.length(); begin += chunk) {
                sm.feed(text.substring(begin, Math.min(begin + chunk, text.length())), chunked);
            }
            chunked.addAll(sm.finish());
            eq(chunked, wholeText, "chunk size " + chunk + " equals whole");
        }

        // 5) 两个空模式（重复空模式）在每个边界各产生一条，且 patternIndex 稳定
        List<Pattern> twoEmpty = TestUtils.patterns("", "x", "");
        Compiled c2 = Engine.compile(twoEmpty, EmptyPatternPolicy.MATCH_EVERY_POSITION);
        List<Match> m2 = Engine.match(c2, "xx");
        long e0 = m2.stream().filter(m -> m.patternIndex() == 0).count();
        long e2 = m2.stream().filter(m -> m.patternIndex() == 2).count();
        eq(e0, 3L, "first empty pattern at 3 boundaries");
        eq(e2, 3L, "second empty pattern at 3 boundaries");

        // 6) 先 feed 空字符串再 finish（started 语义不重复）
        StreamingMatcher sm = new StreamingMatcher(c);
        List<Match> out = new ArrayList<>();
        sm.feed("", out);
        sm.feed("", out);
        out.addAll(sm.finish());
        eq(out.size(), 1, "only one boundary event when no content");

        // 7) SKIP 策略与 oracle 一致
        Compiled skip = Engine.compile(pats, EmptyPatternPolicy.SKIP);
        eq(Engine.match(skip, text),
                NaiveMatcher.match(pats, EmptyPatternPolicy.SKIP, text),
                "SKIP matches oracle");
    }
}
