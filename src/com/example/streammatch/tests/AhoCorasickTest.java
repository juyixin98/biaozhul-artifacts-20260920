package com.example.streammatch.tests;

import com.example.streammatch.AhoCorasick;
import com.example.streammatch.EmptyPatternPolicy;
import com.example.streammatch.Match;

import java.util.List;

import static com.example.streammatch.tests.TestFramework.assertEquals;
import static com.example.streammatch.tests.TestFramework.assertTrue;
import static com.example.streammatch.tests.TestFramework.section;

/** AC 自动机基础行为：经典重叠、重复模式独立 ID、无命中、单字符模式。 */
public final class AhoCorasickTest {

    private AhoCorasickTest() {
    }

    public static void run() {
        section("AhoCorasick 基础匹配", () -> {
            AhoCorasick ac = new AhoCorasick(List.of("he", "she", "his", "hers", "he"));
            List<Match> ms = ac.matchAll("ushers");
            // she@1-3, he(id0)@2-4, he(id4)@2-4, hers@2-6
            assertTrue(ms.size() == 4, "ushers 应有 4 个命中（含重复 he），实际 " + ms.size());

            // ushers: u(0)s(1)h(2)e(3)r(4)s(5)
            // she@1-3码点 -> [1,4)；he@2-3 -> [2,4)（ID 0 与 4 各一）；hers@2..5 -> [2,6)
            boolean she = ms.stream().anyMatch(m -> m.patternId() == 1 && m.start() == 1 && m.end() == 4);
            boolean he0 = ms.stream().anyMatch(m -> m.patternId() == 0 && m.start() == 2 && m.end() == 4);
            boolean he4 = ms.stream().anyMatch(m -> m.patternId() == 4 && m.start() == 2 && m.end() == 4);
            boolean hers = ms.stream().anyMatch(m -> m.patternId() == 3 && m.start() == 2 && m.end() == 6);
            assertTrue(she, "she@1 命中");
            assertTrue(he0, "he 副本 #0（ID=0）独立命中");
            assertTrue(he4, "he 副本 #1（ID=4）独立命中");
            assertTrue(hers, "hers@2 命中（与 he 重叠）");

            // 无命中
            assertTrue(new AhoCorasick(List.of("zzz")).matchAll("abc").isEmpty(),
                    "无命中时返回空列表");

            // null / 空文本
            assertTrue(new AhoCorasick(List.of("a")).matchAll("").isEmpty(), "空文本无命中");
            assertTrue(new AhoCorasick(List.of("a")).matchAll(null).isEmpty(), "null 文本无命中");

            // 单字符模式 + 自重叠
            List<Match> aaa = new AhoCorasick(List.of("aa", "aaa")).matchAll("aaaa");
            // aa@0, aa@1, aa@2, aaa@0, aaa@1 => 5
            assertEquals(5, aaa.size(), "aaaa 对 aa/aaa 应有 5 个重叠命中");

            // 模式比文本长
            assertTrue(new AhoCorasick(List.of("abcdef")).matchAll("abc").isEmpty(),
                    "模式长于文本时无命中");

            // 模式文本回填
            assertTrue(ms.stream().allMatch(m -> m.pattern().equals(
                    List.of("he", "she", "his", "hers", "he").get(m.patternId()))),
                    "命中记录回填正确的模式原文");
        });
    }
}
