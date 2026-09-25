package com.example.edcand;

import java.nio.charset.StandardCharsets;
import java.util.Random;

/** Levenshtein 距离本身的正确性：已知值、空串、Unicode 码点、字节距离对照、阈值版本对拍。 */
public final class LevenshteinTest {

    private LevenshteinTest() {
    }

    public static void register(TestRunner r) {
        knownPairs(r);
        emptyStrings(r);
        unicodeCodepoints(r);
        notByteDistance(r);
        cappedMatchesFull(r);
    }

    private static void knownPairs(TestRunner r) {
        r.add("lev: known ascii pairs", t -> {
            t.eq(Levenshtein.distance("kitten", "sitting"), 3, "kitten/sitting");
            t.eq(Levenshtein.distance("saturday", "sunday"), 3, "saturday/sunday");
            t.eq(Levenshtein.distance("flaw", "lawn"), 2, "flaw/lawn");
            t.eq(Levenshtein.distance("gumbo", "gambol"), 2, "gumbo/gambol");
            t.eq(Levenshtein.distance("book", "back"), 2, "book/back");
            t.eq(Levenshtein.distance("abc", "abc"), 0, "equal");
            t.eq(Levenshtein.distance("a", ""), 1, "single/empty");
            t.eq(Levenshtein.distance("", "abc"), 3, "empty/abc");
            t.eq(Levenshtein.distance("abc", "abd"), 1, "substitute");
            t.eq(Levenshtein.distance("abc", "ac"), 1, "delete");
            t.eq(Levenshtein.distance("ac", "abc"), 1, "insert");
        });
    }

    private static void emptyStrings(TestRunner r) {
        r.add("lev: both empty", t -> t.eq(Levenshtein.distance("", ""), 0, "empty/empty"));
    }

    private static void unicodeCodepoints(TestRunner r) {
        r.add("lev: unicode code points (not UTF-16 units)", t -> {
            // 😀 = U+1F600 增补平面，UTF-16 下是 2 个 char，码点距离必须是 1
            t.eq(Levenshtein.distance("😀", "😁"), 1, "emoji substitute is 1 code point");
            t.eq(Levenshtein.distance("a😀", "a"), 1, "delete emoji = 1");
            t.eq(Levenshtein.distance("日本", "日本語"), 1, "insert one CJK code point");
            t.eq(CodePoints.length("😀"), 1, "emoji code point count");
            t.eq("😀".length(), 2, "sanity: emoji is 2 UTF-16 chars");
            // 组合字符：NFC 一个码点，NFD 两个码点
            t.eq(CodePoints.length("é"), 1, "NFC é is 1 code point");
            t.eq(CodePoints.length("é"), 2, "NFD e+acute is 2 code points");
            // NFC é(1 码点) vs NFD e+U+0301(2 码点)：把 é 替换成 e 再插入
            // 组合重音符，码点 Levenshtein = 2（规范化之后才是 0，见 NormalizationTest）。
            t.eq(Levenshtein.distance("é", "é"), 2,
                    "raw NFC vs NFD forms differ by 2 code point edits");
        });
    }

    private static void notByteDistance(TestRunner r) {
        r.add("lev: codepoint distance must not equal UTF-8 byte distance", t -> {
            // é(U+00E9) 在 UTF-8 中是 2 字节 (C3 A9)，e 是 1 字节。
            // 字节序列上的 Levenshtein 距离 = 2（插入 A9 + 替换 65→C3），
            // 而码点距离 = 1。若实现误用字节，这里会失败。
            String a = "e";
            String b = "é";
            int cpDist = Levenshtein.distance(a, b);
            int byteDist = byteLevenshtein(
                    a.getBytes(StandardCharsets.UTF_8), b.getBytes(StandardCharsets.UTF_8));
            t.eq(cpDist, 1, "code point distance e/é");
            t.eq(byteDist, 2, "byte distance is indeed 2 (proves contrast)");
            t.check(cpDist != byteDist, "codepoint and byte distances must differ");

            // emoji：UTF-8 4 字节，码点距离仍为 1；字节距离为 4
            int cp2 = Levenshtein.distance("a", "😀");
            int by2 = byteLevenshtein(
                    "a".getBytes(StandardCharsets.UTF_8), "😀".getBytes(StandardCharsets.UTF_8));
            t.eq(cp2, 1, "code point distance a/emoji");
            t.eq(by2, 4, "byte distance a/emoji");
        });
    }

    private static void cappedMatchesFull(TestRunner r) {
        r.add("lev: cappedDistance agrees with full DP (random fuzz)", t -> {
            Random rnd = new Random(424242);
            String alpha = "abcde";
            for (int iter = 0; iter < 4000; iter++) {
                String a = randString(rnd, alpha, rnd.nextInt(9));
                String b = randString(rnd, alpha, rnd.nextInt(9));
                int full = Levenshtein.distance(a, b);
                for (int k = 0; k <= 4; k++) {
                    int capped = Levenshtein.cappedDistance(a, b, k);
                    if (full <= k) {
                        t.eq(capped, full, "within-threshold must be exact: '" + a + "'/'" + b + "' k=" + k);
                    } else {
                        t.eq(capped, k + 1, "over-threshold must saturate: '" + a + "'/'" + b + "' k=" + k);
                    }
                }
            }
        });
        r.add("lev: cappedDistance fuzz with emoji code points", t -> {
            Random rnd = new Random(99);
            int[] pool = {'a', 'b', 0x1F600, 0x1F601, '日', '本'};
            for (int iter = 0; iter < 2000; iter++) {
                int[] a = randCp(rnd, pool, rnd.nextInt(7));
                int[] b = randCp(rnd, pool, rnd.nextInt(7));
                int full = Levenshtein.distance(a, b);
                int k = rnd.nextInt(3);
                int capped = Levenshtein.cappedDistance(a, b, k);
                int expect = full <= k ? full : k + 1;
                t.eq(capped, expect, "cp fuzz");
            }
        });
    }

    // ---- helpers ----

    private static String randString(Random rnd, String alpha, int len) {
        StringBuilder sb = new StringBuilder(len);
        for (int i = 0; i < len; i++) {
            sb.append(alpha.charAt(rnd.nextInt(alpha.length())));
        }
        return sb.toString();
    }

    private static int[] randCp(Random rnd, int[] pool, int len) {
        int[] out = new int[len];
        for (int i = 0; i < len; i++) {
            out[i] = pool[rnd.nextInt(pool.length)];
        }
        return out;
    }

    /** 字节序列上的 Levenshtein —— 仅用于在测试中与码点距离形成对照。 */
    static int byteLevenshtein(byte[] a, byte[] b) {
        int[] prev = new int[b.length + 1];
        int[] cur = new int[b.length + 1];
        for (int j = 0; j <= b.length; j++) {
            prev[j] = j;
        }
        for (int i = 1; i <= a.length; i++) {
            cur[0] = i;
            for (int j = 1; j <= b.length; j++) {
                int cost = (a[i - 1] == b[j - 1]) ? 0 : 1;
                cur[j] = Math.min(Math.min(cur[j - 1] + 1, prev[j] + 1), prev[j - 1] + cost);
            }
            int[] tmp = prev;
            prev = cur;
            cur = tmp;
        }
        return prev[b.length];
    }
}
