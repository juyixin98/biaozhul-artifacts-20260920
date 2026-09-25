package com.example.streammatch;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * 自建合成语料：不读取任何外部数据，全部由确定性随机种子生成。
 *
 * <p>提供四类语料，覆盖验收要求的形态：</p>
 * <ol>
 *   <li>{@code sharedPrefix} —— 大量共享前缀模式（trie 深而窄）+ 保证命中的文本；</li>
 *   <li>{@code overlap}      —— 刻意重叠/互为子串的模式（he/she/hers/his/...）；</li>
 *   <li>{@code unicode}      —emoji、代理对、组合字符、CJK 与边界压力；</li>
 *   <li>{@code randomDna}    —— 小字母表上的高命中频率随机流（性能/一致性压测）。</li>
 * </ol>
 */
public final class CorpusGenerator {

    /** 一份生成结果：模式表 + 逻辑文本。 */
    public record Corpus(String name, List<String> patterns, String text) {
    }

    private CorpusGenerator() {
    }

    public static List<String> names() {
        return List.of("sharedPrefix", "overlap", "unicode", "randomDna");
    }

    public static Corpus generate(String name, long seed) {
        return switch (name) {
            case "sharedPrefix" -> sharedPrefix(seed);
            case "overlap" -> overlap(seed);
            case "unicode" -> unicode(seed);
            case "randomDna" -> randomDna(seed, 200_000, 2_000);
            default -> throw new IllegalArgumentException(
                    "未知语料: " + name + "；可选: " + names());
        };
    }

    /** 大规模共享前缀：全部模式形如 pr3fix/000…NNN，绝大多数命中通过注入保证。 */
    private static Corpus sharedPrefix(long seed) {
        Random rnd = new Random(seed);
        int patternN = 2_000;
        String stem = "pr3fix/0000/";
        List<String> patterns = new ArrayList<>(patternN + 4);
        patterns.add(stem);                 // 极短前缀模式，制造海量重叠命中
        patterns.add("pr3fix");
        patterns.add("pr3");
        patterns.add("x");
        for (int i = 0; i < patternN; i++) {
            patterns.add(stem + String.format("%06d", i));
        }
        StringBuilder sb = new StringBuilder();
        // 注入 500 个确定命中
        for (int i = 0; i < 500; i++) {
            int which = rnd.nextInt(patternN);
            sb.append(stem).append(String.format("%06d", which)).append(' ');
        }
        // 再追加随机噪声（小字母表，偶尔自然命中）
        String alpha = "pr3fix/012456789 \t";
        for (int i = 0; i < 100_000; i++) {
            sb.append(alpha.charAt(rnd.nextInt(alpha.length())));
        }
        return new Corpus("sharedPrefix", patterns, sb.toString());
    }

    /** 经典重叠模式集 + 含重复模式，文本由片段拼接保证密集命中。 */
    private static Corpus overlap(long seed) {
        Random rnd = new Random(seed);
        List<String> patterns = List.of(
                "he", "she", "his", "hers", "he", // "he" 故意重复：ID 0 与 4 独立
                "a", "ah", "aha",
                "aa", "aaa", "aaaa",             // 周期模式制造重叠
                "shes", "h"
        );
        String[] pieces = {
                "ushers", "she", "he thought he saw his hers",
                "ahahaha", "aaaaaaa", "sheshe", "xxhxx", "ahishers"
        };
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 2_000; i++) {
            sb.append(pieces[rnd.nextInt(pieces.length)]).append(' ');
        }
        return new Corpus("overlap", new ArrayList<>(patterns), sb.toString());
    }

    /** Unicode 压力：BMP / 星面层（代理对）/ 组合字符 / 零宽连接序列 / 边界碎片。 */
    private static Corpus unicode(long seed) {
        List<String> patterns = List.of(
                "日", "日本語", "カタカナ",
                "😀", "🇯🇵",               // emoji 与区域指示符序列（多个码点）
                "👨‍👩‍👧",              // ZWJ 家庭序列
                "é",              // e + 组合重音（é 的分解形式）
                "a", "ab", ""            // 空模式：默认 SKIP，可在请求中改策略
        );
        String[] pieces = {
                "日本語のテキストでカタカナと日本語",
                "hello 😀 world 🇯🇵 !!",
                "family: 👨‍👩‍👧 done",
                "café é vs é",
                "ab😀ab 日本 語 aaa",
                "🇯" + "🇵",              // 拆开的区域指示符
                "日" + "本" + "語",
                "👨", "‍", "👩"           // ZWJ 拆开
        };
        Random rnd = new Random(seed);
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 500; i++) {
            sb.append(pieces[rnd.nextInt(pieces.length)]);
        }
        return new Corpus("unicode", new ArrayList<>(patterns), sb.toString());
    }

    /** DNA 风格小字母表随机流：高频率自然命中，适合性能与分块一致性压测。 */
    private static Corpus randomDna(long seed, int textCodePoints, int patternN) {
        Random rnd = new Random(seed);
        char[] alpha = {'A', 'C', 'G', 'T'};
        List<String> patterns = new ArrayList<>(patternN);
        for (int i = 0; i < patternN; i++) {
            int len = 6 + rnd.nextInt(9); // 6..14
            StringBuilder p = new StringBuilder(len);
            for (int k = 0; k < len; k++) {
                p.append(alpha[rnd.nextInt(4)]);
            }
            patterns.add(p.toString());
        }
        // 注入若干确定命中
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 100; i++) {
            sb.append(patterns.get(rnd.nextInt(patternN)));
        }
        while (sb.codePointCount(0, sb.length()) < textCodePoints) {
            sb.append(alpha[rnd.nextInt(4)]);
        }
        String full = sb.toString();
        int end = full.offsetByCodePoints(0, textCodePoints);
        return new Corpus("randomDna", patterns, full.substring(0, end));
    }
}
