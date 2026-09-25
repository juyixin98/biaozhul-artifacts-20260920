package com.example.ac.corpus;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 自建确定性合成语料（固定种子的 LCG 伪随机，保证可复现，不访问任何外部资源）。
 *
 * <p>三个内置 profile：
 * <ul>
 *   <li>{@code dna}：4 字母 DNA 风格文本，大量短模式频繁命中；</li>
 *   <li>{@code sharedPrefix}：{@code aaa…ab} 系列大量共享前缀模式，长 a-run 文本产生重叠；</li>
 *   <li>{@code unicode}：BMP + 补充平面（emoji，代理对）+ 组合字符混合，检验 Unicode 边界。</li>
 * </ul>
 * 生成时会把部分模式显式“种植”到文本中（包括跨块友好的位置），
 * 保证每种语料都确有命中。
 */
public final class SyntheticCorpus {

    private SyntheticCorpus() {
    }

    public static final List<String> PROFILES = List.of("dna", "sharedPrefix", "unicode");

    public static CorpusProfile generate(String name, int textLength, int patternCount) {
        return switch (name) {
            case "dna" -> dna(textLength, patternCount);
            case "sharedPrefix" -> sharedPrefix(textLength, patternCount);
            case "unicode" -> unicode(textLength, patternCount);
            default -> throw new IllegalArgumentException(
                    "unknown corpus profile: " + name + " (expected one of " + PROFILES + ")");
        };
    }

    /** 确定性 LCG（Numerical Recipes 参数）。 */
    private static long next(long[] state) {
        state[0] = (state[0] * 6364136223846793005L + 1442695040888963407L) & Long.MAX_VALUE;
        return state[0];
    }

    private static int bounded(long[] state, int bound) {
        return (int) (next(state) % bound);
    }

    private static CorpusProfile dna(int length, int patCount) {
        length = Math.max(length, 64);
        patCount = Math.max(patCount, 4);
        long[] rng = {0xD1AAC001L};
        char[] alphabet = {'A', 'C', 'G', 'T'};
        StringBuilder text = new StringBuilder(length);
        for (int i = 0; i < length; i++) {
            text.append(alphabet[bounded(rng, 4)]);
        }
        // 种植若干确定片段，保证跨块命中真实存在。
        String[] planted = {"ACGTACGT", "TTTTAAAA", "CGTCGTCGT", "GGGG", "ACACAC"};
        for (int k = 0; k < planted.length; k++) {
            int pos = 7 + k * 13;
            text.replace(pos, pos + planted[k].length(), planted[k]);
        }

        Map<String, String> pats = new LinkedHashMap<>();
        for (String p : planted) {
            pats.putIfAbsent("dna-seed-" + p, p);
        }
        int target = Math.min(patCount, 60);
        while (pats.size() < target) {
            int plen = 2 + bounded(rng, 7);
            StringBuilder p = new StringBuilder(plen);
            for (int i = 0; i < plen; i++) {
                p.append(alphabet[bounded(rng, 4)]);
            }
            pats.putIfAbsent("dna-p" + pats.size(), p.toString());
        }
        return toProfile("dna", text.toString(), pats);
    }

    private static CorpusProfile sharedPrefix(int length, int patCount) {
        length = Math.max(length, 64);
        patCount = Math.max(patCount, 3);
        long[] rng = {0x54A2ED02L};
        StringBuilder text = new StringBuilder(length);
        // 大部分为 'a'，偶发 'b'/'c'，制造 a^k 重叠与共享前缀终止。
        for (int i = 0; i < length; i++) {
            int r = bounded(rng, 20);
            text.append(r == 0 ? 'b' : r == 1 ? 'c' : 'a');
        }
        // 种植超长 a-run 与各长度终止。
        int pos = 11;
        text.replace(pos, pos + 32, "a".repeat(32));
        text.setCharAt(pos + 15, 'b'); // a^15 b 在此处
        text.setCharAt(3, 'c');

        int maxK = Math.min(patCount, 18);
        Map<String, String> pats = new LinkedHashMap<>();
        for (int k = 1; k <= maxK; k++) {
            String a = "a".repeat(k);
            pats.put("pref-a" + k, a);
            if (k <= 16) {
                pats.put("pref-a" + k + "b", a + "b");
            }
        }
        pats.put("pref-abc", "abc");
        pats.put("pref-long", "a".repeat(20) + "b");
        return toProfile("sharedPrefix", text.toString(), pats);
    }

    private static CorpusProfile unicode(int length, int patCount) {
        length = Math.max(length, 48);
        patCount = Math.max(patCount, 4);
        long[] rng = {0x001C0DE3L};
        // BMP 中文、BMP 重音字符、补充平面 emoji（代理对）、组合用 combining accent。
        int[] bmp = {'你', '好', '世', '界', 'é', 'λ', '中', '文'};
        int[] astral = {0x1F600, 0x1F680, 0x1F4A9, 0x1F44D, 0x1FAA8};
        int[] combiners = {0x0301, 0x0302};
        int alphabetSize = bmp.length + astral.length + 3; // +3 ASCII 混杂
        StringBuilder text = new StringBuilder();
        int cps = 0;
        while (cps < length) {
            int r = bounded(rng, alphabetSize + combiners.length);
            if (r < bmp.length) {
                text.appendCodePoint(bmp[r]);
                cps++;
            } else if (r < bmp.length + astral.length) {
                text.appendCodePoint(astral[r - bmp.length]);
                cps++;
            } else if (r < bmp.length + astral.length + 3) {
                text.append((char) ('x' + (r - bmp.length - astral.length)));
                cps++;
            } else {
                text.appendCodePoint(combiners[r - bmp.length - astral.length - 3]);
                cps++; // 组合符本身也是一个 code point
            }
        }
        // 种植：emoji 跨代理对的模式、BMP 模式、ASCII 与中文混合模式。
        String[] planted = {
                new String(Character.toChars(0x1F600)) + new String(Character.toChars(0x1F680)),
                "你好世界",
                "x" + new String(Character.toChars(0x1F4A9)) + "y",
                "中文",
        };
        int at = 5;
        for (String p : planted) {
            text.replace(at, at + p.length(), p);
            at += 17;
        }
        Map<String, String> pats = new LinkedHashMap<>();
        int n = 0;
        for (String p : planted) {
            pats.put("uni-seed-" + n++, p);
        }
        // 加入单 emoji 与单字符模式，制造大量重叠。
        for (int cp : astral) {
            pats.putIfAbsent("uni-emoji-" + Integer.toHexString(cp),
                    new String(Character.toChars(cp)));
        }
        for (int cp : bmp) {
            if (pats.size() >= Math.min(patCount + planted.length, 40)) {
                break;
            }
            pats.putIfAbsent("uni-char-" + Integer.toHexString(cp),
                    new String(Character.toChars(cp)));
        }
        return toProfile("unicode", text.toString(), pats);
    }

    private static CorpusProfile toProfile(String name, String text, Map<String, String> pats) {
        List<String> ids = new ArrayList<>(pats.keySet());
        List<String> patterns = new ArrayList<>(pats.values());
        return new CorpusProfile(name, text, ids, patterns);
    }
}
