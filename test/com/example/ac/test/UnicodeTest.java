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
 * Unicode 验收：code point 位置与 UTF-16 char 位置都正确；
 * emoji（补充平面，UTF-16 代理对、UTF-8 四字节）跨块时不错位。
 */
public class UnicodeTest extends TestCase {

    private static final String GRIN = new String(Character.toChars(0x1F600)); // 😀
    private static final String ROCKET = new String(Character.toChars(0x1F680)); // 🚀
    private static final String POOP = new String(Character.toChars(0x1F4A9)); // 💩

    public UnicodeTest() {
        super("unicode-boundaries");
    }

    @Override
    protected void run() {
        // 文本：a 😀 b 你好 😀 c（code point 与 UTF-16 偏移不同）
        String text = "a" + GRIN + "b你好" + GRIN + "c";
        List<Pattern> pats = TestUtils.patterns(GRIN, "你好", "b", GRIN + "c", "a" + GRIN);
        Compiled c = Engine.compile(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION);
        List<Match> whole = Engine.match(c, text);
        eq(whole, NaiveMatcher.match(pats, EmptyPatternPolicy.MATCH_EVERY_POSITION, text),
                "unicode whole text equals naive");

        // 找到 😀 的命中并核对双坐标
        Match emoji = whole.stream().filter(m -> m.matched().equals(GRIN)).findFirst().orElseThrow();
        eq(emoji.start(), 1, "emoji cp start");
        eq(emoji.end(), 2, "emoji cp end");
        eq(emoji.charStart(), 1, "emoji UTF-16 start (one prior char)");
        eq(emoji.charEnd(), 3, "emoji UTF-16 end (surrogate pair spans 2 units)");

        Match second = whole.stream().filter(m -> m.matched().equals(GRIN) && m.start() > 1)
                .findFirst().orElseThrow();
        eq(second.start(), 5, "second emoji cp start");
        eq(second.charStart(), 6, "second emoji char start (a(1)+pair(2)+b(1)+2 BMP = 6)");

        // 跨代理对切：按 UTF-16 单 char 喂会割裂代理对——
        // 因此对外保证的切块单位是 code point（见 feed(String) 内 codePointAt 处理），
        // 以及任意 UTF-8 字节切分。String 路径若调用方传入孤立代理，属于调用方契约违反；
        // 这里验证 code point 级逐点喂入等价。
        StreamingMatcher sm = new StreamingMatcher(c);
        List<Match> perCp = new ArrayList<>();
        int[] cps = text.codePoints().toArray();
        for (int cp : cps) {
            sm.feed(new String(Character.toChars(cp)), perCp);
        }
        perCp.addAll(sm.finish());
        eq(perCp, whole, "per-code-point feeding equals whole");

        // 任意 code point 块大小
        for (int size = 1; size <= cps.length; size++) {
            StreamingMatcher s2 = new StreamingMatcher(c);
            List<Match> chunked = new ArrayList<>();
            for (int begin = 0; begin < cps.length; begin += size) {
                int end = Math.min(begin + size, cps.length);
                s2.feed(new String(cps, begin, end - begin), chunked);
            }
            chunked.addAll(s2.finish());
            eq(chunked, whole, "cp chunk size " + size + " equals whole");
        }

        // UTF-8 字节任意切分（包含切在 4 字节 emoji 中间）
        byte[] utf8 = text.getBytes(java.nio.charset.StandardCharsets.UTF_8);
        for (int size = 1; size <= utf8.length; size++) {
            StreamingMatcher s3 = new StreamingMatcher(c);
            List<Match> chunked = new ArrayList<>();
            for (int begin = 0; begin < utf8.length; begin += size) {
                int end = Math.min(begin + size, utf8.length);
                s3.feedBytes(utf8, begin, end - begin, chunked);
            }
            chunked.addAll(s3.finish());
            eq(chunked, whole, "utf8 byte chunk size " + size + " equals whole");
        }

        // 组合字符序列：模式可以包含 combining mark（e + 0x0301）
        String composed = "e" + new String(Character.toChars(0x0301)); // é（分解形式，2 个 code point）
        String t2 = "x" + composed + "y";
        List<Pattern> p2 = TestUtils.patterns(composed);
        List<Match> m2 = Engine.match(Engine.compile(p2, EmptyPatternPolicy.MATCH_EVERY_POSITION), t2);
        eq(m2.size(), 1, "combining sequence matched once");
        eq(m2.get(0).start(), 1, "combining seq start cp");
        eq(m2.get(0).end(), 3, "combining seq end cp");

        // 混合 ASCII/emoji 模式跨多块命中
        List<Pattern> p3 = TestUtils.patterns(POOP + "x", ROCKET + GRIN);
        String t3 = "00" + POOP + "x00" + ROCKET + GRIN + "00";
        Compiled c3 = Engine.compile(p3, EmptyPatternPolicy.MATCH_EVERY_POSITION);
        List<Match> w3 = Engine.match(c3, t3);
        eq(w3.size(), 2, "two astral-spanning patterns");
        byte[] b3 = t3.getBytes(java.nio.charset.StandardCharsets.UTF_8);
        for (int size = 1; size <= 7; size++) {
            StreamingMatcher s4 = new StreamingMatcher(c3);
            List<Match> chunked = new ArrayList<>();
            for (int begin = 0; begin < b3.length; begin += size) {
                s4.feedBytes(b3, begin, Math.min(size, b3.length - begin), chunked);
            }
            chunked.addAll(s4.finish());
            eq(chunked, w3, "astral utf8 chunks size=" + size);
        }
    }
}
