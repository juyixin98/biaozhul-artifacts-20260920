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
import java.util.Random;

/**
 * 随机差分测试（fuzz）：
 * 多轮随机文本 + 随机模式（含大量共享前缀、含重复、含空串、含 Unicode），
 * 对随机分块（String/code point 路径与 UTF-8 字节路径）的流式结果
 * 与朴素全量匹配做全量逐条比较。固定种子，可复现。
 */
public class FuzzTest extends TestCase {

    private static final long SEED = 20260924L;

    public FuzzTest() {
        super("fuzz-vs-naive");
    }

    @Override
    protected void run() {
        Random rnd = new Random(SEED);

        // 字母表池：ASCII、重复倾向高的小字母表、BMP、补充平面
        int[][] alphabets = {
                {'a', 'b', 'c'},
                {'a', 'b'},
                {'a', 'a', 'a', 'b'},                 // 高重复 → 共享前缀/重叠
                {'x', 'y', '你', '好', 0x1F600, 0x1F680, 0x0301},
                {'A', 'C', 'G', 'T'},
        };

        EmptyPatternPolicy[] policies = EmptyPatternPolicy.values();
        int iterations = 400;
        for (int it = 0; it < iterations; it++) {
            int[] alpha = alphabets[rnd.nextInt(alphabets.length)];
            EmptyPatternPolicy policy = policies[rnd.nextInt(policies.length)];

            int textLen = rnd.nextInt(60); // 0..59，含空文本
            StringBuilder text = new StringBuilder();
            for (int i = 0; i < textLen; i++) {
                text.appendCodePoint(alpha[rnd.nextInt(alpha.length)]);
            }

            int patCount = 1 + rnd.nextInt(12);
            List<Pattern> pats = new ArrayList<>();
            for (int pi = 0; pi < patCount; pi++) {
                int choice = rnd.nextInt(10);
                String lit;
                if (choice == 0 && policy != EmptyPatternPolicy.ERROR) {
                    lit = ""; // 空模式
                } else if (choice == 1) {
                    lit = randomLiteral(alpha, rnd, 1 + rnd.nextInt(4));
                    lit = lit + lit.substring(0, Math.min(lit.length(), 1 + rnd.nextInt(lit.length())));
                } else {
                    lit = randomLiteral(alpha, rnd, 1 + rnd.nextInt(6));
                }
                // 约 1/5 概率插入与上一个相同的字面 → 重复模式独立身份
                String id = "f" + pi;
                pats.add(Pattern.of(id, lit));
            }
            // 再随机复制一个已有模式，确保重复模式被覆盖
            if (patCount > 1 && rnd.nextBoolean()) {
                Pattern src = pats.get(rnd.nextInt(pats.size()));
                pats.add(Pattern.of("dup-of-" + src.id(), src.literal()));
            }

            List<Pattern> finalPats = pats;
            List<Match> oracle;
            Compiled compiled;
            try {
                compiled = Engine.compile(finalPats, policy);
                oracle = NaiveMatcher.match(finalPats, policy, text.toString());
            } catch (IllegalArgumentException ex) {
                // ERROR 策略遇到空模式：编译应一致拒绝
                boolean engineAlsoRejects = false;
                try {
                    Engine.compile(finalPats, policy);
                } catch (IllegalArgumentException expected) {
                    engineAlsoRejects = true;
                }
                check(engineAlsoRejects, "ERROR policy rejection consistent on iteration " + it);
                continue;
            }

            String t = text.toString();

            // a) 一次性
            List<Match> oneShot = Engine.match(compiled, t);
            eq(oneShot, oracle, "one-shot iteration " + it + " (pats=" + finalPats + ", text=" + t + ")");

            // b) 随机 String 分块（1..3 次喂入，切点随机；切点按 char，可能落在代理对中间——
            //    这种喂法本身违反契约，所以 fuzz 里按 code point 切）
            int feeds = 1 + rnd.nextInt(4);
            int[] cps = t.codePoints().toArray();
            int[] cuts = randomCuts(cps.length, feeds, rnd);
            StreamingMatcher sm = new StreamingMatcher(compiled);
            List<Match> got = new ArrayList<>();
            int prev = 0;
            for (int cut : cuts) {
                sm.feed(new String(cps, prev, cut - prev), got);
                prev = cut;
            }
            sm.feed(new String(cps, prev, cps.length - prev), got);
            got.addAll(sm.finish());
            eq(got, oracle, "cp chunked iteration " + it);

            // c) 随机 UTF-8 字节分块（切点可以在多字节字符内部）
            byte[] bytes = t.getBytes(StandardCharsets.UTF_8);
            int bfeeds = 1 + rnd.nextInt(5);
            int[] bcuts = randomCuts(bytes.length, bfeeds, rnd);
            StreamingMatcher smb = new StreamingMatcher(compiled);
            List<Match> gotB = new ArrayList<>();
            int bp = 0;
            for (int cut : bcuts) {
                smb.feedBytes(bytes, bp, cut - bp, gotB);
                bp = cut;
            }
            smb.feedBytes(bytes, bp, bytes.length - bp, gotB);
            gotB.addAll(smb.finish());
            eq(gotB, oracle, "utf8 chunked iteration " + it);
        }
    }

    private static String randomLiteral(int[] alpha, Random rnd, int len) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < len; i++) {
            sb.appendCodePoint(alpha[rnd.nextInt(alpha.length)]);
        }
        return sb.toString();
    }

    /** 在 [1,len) 内生成 count 个不重复升序切点；若位置不足则全部返回。 */
    private static int[] randomCuts(int len, int count, Random rnd) {
        if (len <= 1) {
            return new int[0];
        }
        List<Integer> positions = new ArrayList<>();
        for (int i = 1; i < len; i++) {
            positions.add(i);
        }
        java.util.Collections.shuffle(positions, rnd);
        int take = Math.min(count, positions.size());
        int[] cuts = new int[take];
        for (int i = 0; i < take; i++) {
            cuts[i] = positions.get(i);
        }
        java.util.Arrays.sort(cuts);
        return cuts;
    }
}
