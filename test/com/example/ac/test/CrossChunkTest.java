package com.example.ac.test;

import com.example.ac.Compiled;
import com.example.ac.EmptyPatternPolicy;
import com.example.ac.Engine;
import com.example.ac.Match;
import com.example.ac.NaiveMatcher;
import com.example.ac.Pattern;
import com.example.ac.StreamingMatcher;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * 跨块命中验收：
 * 对每个候选切点位置（含模式必须跨越切点才能命中的情形），
 * 流式结果必须与朴素全量匹配逐条相等。
 */
public class CrossChunkTest extends TestCase {

    public CrossChunkTest() {
        super("cross-chunk-hits");
    }

    @Override
    protected void run() {
        // 1) 切点贯穿唯一命中的模式：模式长度 5，文本长度 7，切点从 1..6 都把模式切开
        List<Pattern> pats = TestUtils.patterns("abcde");
        String text = "00abcde";
        crossCutEveryCpPosition(pats, text, EmptyPatternPolicy.MATCH_EVERY_POSITION);

        // 2) 两块拼接，命中恰好开始于第一块末尾的字符
        pats = TestUtils.patterns("xyz");
        text = "abxyzcd";
        crossCutEveryCpPosition(pats, text, EmptyPatternPolicy.MATCH_EVERY_POSITION);

        // 3) 多模式 + 重叠，切点扫过全部位置、所有块大小
        pats = TestUtils.patterns("ab", "b", "abc", "bc", "c", "zab");
        text = "zzabcabc";
        crossCutEveryCpPosition(pats, text, EmptyPatternPolicy.MATCH_EVERY_POSITION);

        // 4) 空模式 + 跨块非空模式混合
        pats = TestUtils.patterns("abc", "", "bc");
        text = "xabcx";
        crossCutEveryCpPosition(pats, text, EmptyPatternPolicy.MATCH_EVERY_POSITION);

        // 5) UTF-8 字节流：切点扫过每个字节（含 3 字节中文、4 字节 emoji 内部）
        pats = TestUtils.patterns("你好", "😀🚀", "a😀");
        text = "xx你好xx😀🚀zza😀q";
        crossCutEveryByte(pats, text);

        // 6) 多块（>2）一致性：块大小 1 的逐字符流式
        pats = TestUtils.patterns("he", "hel", "hello", "ell", "lo");
        text = "hello hello hello";
        Compiled c = Engine.compile(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION);
        StreamingMatcher sm = new StreamingMatcher(c);
        List<Match> got = new ArrayList<>();
        for (String ch : text.split("")) {
            sm.feed(ch, got);
        }
        got.addAll(sm.finish());
        eq(got, NaiveMatcher.match(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION, text),
                "char-at-a-time streaming equals naive");

        // 7) 跨块命中数统计（CorpusRunner 逻辑的微型验证内联版）
        // 文本 "aaab"、模式 "aab"；切成 ["aa","ab"] 时该命中跨块完成
        pats = TestUtils.patterns("aab");
        c = Engine.compile(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION);
        sm = new StreamingMatcher(c);
        List<Match> first = new ArrayList<>();
        sm.feed("aa", first);
        check(first.isEmpty(), "no emission until pattern completes");
        List<Match> second = new ArrayList<>();
        sm.feed("ab", second);
        second.addAll(sm.finish());
        long cross = second.stream().filter(m -> m.length() > 0 && m.start() < 2).count();
        eq(cross, 1L, "the aab hit spans the cut at 2");
    }

    private void crossCutEveryCpPosition(List<Pattern> pats, String text, EmptyPatternPolicy policy) {
        Compiled c = Engine.compile(pats, policy);
        List<Match> oracle = NaiveMatcher.match(pats, policy, text);
        int[] cps = text.codePoints().toArray();
        for (int cut = 1; cut < cps.length; cut++) {
            String left = new String(cps, 0, cut);
            String right = new String(cps, cut, cps.length - cut);
            StreamingMatcher sm = new StreamingMatcher(c);
            List<Match> got = new ArrayList<>();
            sm.feed(left, got);
            sm.feed(right, got);
            got.addAll(sm.finish());
            eq(got, oracle, "cp cut after " + cut + " code points (\"" + left + "|\")");
        }
        // 再扫一遍各种多块大小
        for (int size = 1; size <= cps.length; size++) {
            StreamingMatcher sm = new StreamingMatcher(c);
            List<Match> got = new ArrayList<>();
            for (int begin = 0; begin < cps.length; begin += size) {
                sm.feed(new String(cps, begin, Math.min(size, cps.length - begin)), got);
            }
            got.addAll(sm.finish());
            eq(got, oracle, "cp chunk size " + size);
        }
    }

    private void crossCutEveryByte(List<Pattern> pats, String text) {
        Compiled c = Engine.compile(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION);
        List<Match> oracle = NaiveMatcher.match(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION, text);
        byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
        for (int cut = 1; cut < bytes.length; cut++) {
            StreamingMatcher sm = new StreamingMatcher(c);
            List<Match> got = new ArrayList<>();
            sm.feedBytes(bytes, 0, cut, got);
            sm.feedBytes(bytes, cut, bytes.length - cut, got);
            got.addAll(sm.finish());
            eq(got, oracle, "utf8 cut after byte " + cut + " / " + bytes.length);
        }
        for (int size = 1; size <= bytes.length; size++) {
            StreamingMatcher sm = new StreamingMatcher(c);
            List<Match> got = new ArrayList<>();
            for (int begin = 0; begin < bytes.length; begin += size) {
                sm.feedBytes(bytes, begin, Math.min(size, bytes.length - begin), got);
            }
            got.addAll(sm.finish());
            eq(got, oracle, "utf8 chunk size " + size);
        }
    }
}
