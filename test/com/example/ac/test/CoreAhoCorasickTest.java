package com.example.ac.test;

import com.example.ac.Compiled;
import com.example.ac.EmptyPatternPolicy;
import com.example.ac.Engine;
import com.example.ac.Match;
import com.example.ac.Pattern;

import java.util.List;

/** 手工可验证的 AC 核心语义：重叠、后缀链接、重复模式独立身份。 */
public class CoreAhoCorasickTest extends TestCase {

    public CoreAhoCorasickTest() {
        super("core-aho-corasick");
    }

    public static List<Match> run(String text, EmptyPatternPolicy policy, String... pats) {
        return Engine.match(Engine.compile(TestUtils.patterns(pats), policy), text);
    }

    @Override
    protected void run() {
        // 1) 经典 "ushers"：he / she / his / hers
        List<Match> r = run("ushers", EmptyPatternPolicy.MATCH_EVERY_POSITION, "he", "she", "his", "hers");
        // 结束于4：she@[1,4]、he@[2,4]（重叠，同一结束位置两个不同长度）；
        // 结束于6：hers@[2,6]（后缀输出 he 已在位置 4 报过）；his 不命中。
        check(r.size() == 3, "ushers should have 3 hits, got " + r);
        eq(r.get(0).patternId(), "p1", "first hit is she");
        eq(r.get(0).start(), 1, "she start");
        eq(r.get(1).patternId(), "p0", "second hit is he");
        eq(r.get(1).start(), 2, "he start");
        eq(r.get(1).end(), 4, "shared end");
        eq(r.get(2).patternId(), "p3", "third hit is hers");
        eq(r.get(2).start(), 2, "hers start");
        eq(r.get(2).end(), 6, "hers end");

        // 2) 完全包含与自重叠：aaa 中 a / aa / aaa
        r = run("aaa", EmptyPatternPolicy.MATCH_EVERY_POSITION, "a", "aa", "aaa");
        // a@0,  aa@0 a@1, aa@1 a@2, aaa@0 (结束于3的顺序：aaa start0, a start2)
        List<String> got = r.stream().map(m -> m.patternId() + "@" + m.start() + "-" + m.end()).toList();
        eq(got, List.of(
                "p0@0-1",
                "p1@0-2", "p0@1-2",
                "p2@0-3", "p1@1-3", "p0@2-3"), "aaa overlap event order: " + got);

        // 3) 重复模式保留独立身份：两个字面相同的模式都要报
        Compiled c = Engine.compile(List.of(
                Pattern.of("alpha", "ab"), Pattern.of("beta", "ab")),
                EmptyPatternPolicy.MATCH_EVERY_POSITION);
        r = Engine.match(c, "abab");
        check(r.size() == 4, "duplicate patterns each fire, got " + r);
        eq(r.get(0).patternId(), "alpha", "dup 1 first occurrence");
        eq(r.get(1).patternId(), "beta", "dup 2 first occurrence");
        eq(r.get(0).patternIndex(), 0, "dup patternIndex 0");
        eq(r.get(1).patternIndex(), 1, "dup patternIndex 1");
        eq(r.get(2).start(), 2, "dup second occurrence start");
        eq(r.get(3).start(), 2, "dup second occurrence start");

        // 4) 无命中
        r = run("xyz", EmptyPatternPolicy.MATCH_EVERY_POSITION, "ab", "cd");
        check(r.isEmpty(), "no hit expected");

        // 5) 模式长于文本
        r = run("ab", EmptyPatternPolicy.MATCH_EVERY_POSITION, "abc");
        check(r.isEmpty(), "longer pattern no hit");

        // 6) 模式恰等于文本
        r = run("abc", EmptyPatternPolicy.MATCH_EVERY_POSITION, "abc");
        eq(r.size(), 1, "exact whole-text match");
        eq(r.get(0).start(), 0, "whole start");
        eq(r.get(0).end(), 3, "whole end");

        // 7) 后链接多输出：匹配 "she" 的同时输出 "he"，且顺序为长在前
        r = run("sheshe", EmptyPatternPolicy.MATCH_EVERY_POSITION, "he", "she");
        eq(r.size(), 4, "sheshe hits 4");
        eq(r.get(0).patternId(), "p1", "she before he at same end");
        eq(r.get(1).patternId(), "p0", "he suffix output");

        // 8) 空模式策略 ERROR
        boolean threw = false;
        try {
            Engine.compile(TestUtils.patterns("a", ""), EmptyPatternPolicy.ERROR);
        } catch (IllegalArgumentException e) {
            threw = true;
        }
        check(threw, "ERROR policy rejects empty pattern at compile");

        // 9) SKIP：空模式被忽略，非空照常
        c = Engine.compile(TestUtils.patterns("a", "", "b"), EmptyPatternPolicy.SKIP);
        eq(c.skippedIndices().length, 1, "one skipped index");
        eq(c.skippedIndices()[0], 1, "skipped index is 1");
        r = Engine.match(c, "ab");
        check(r.stream().noneMatch(m -> m.length() == 0), "no zero-length hits under SKIP");
        eq(r.size(), 2, "non-empty still match under SKIP");

        // 10) 单字符模式在全字符文本上每位置命中
        r = run("aaaa", EmptyPatternPolicy.MATCH_EVERY_POSITION, "a");
        eq(r.size(), 4, "a x4");

        // 11) 一个模式是另一个的后缀（dictionary link 多级）
        r = run("abcd", EmptyPatternPolicy.MATCH_EVERY_POSITION, "d", "cd", "bcd", "abcd");
        got = r.stream().map(m -> m.matched() + "@" + m.start()).toList();
        // 结束于1: d? 无；结束于4: abcd@0, bcd@1, cd@2, d@3（长度降序）
        eq(got, List.of("abcd@0", "bcd@1", "cd@2", "d@3"), "multi-level dict link: " + got);
    }
}
