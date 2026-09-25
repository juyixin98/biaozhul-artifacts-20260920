package com.example.editdistance;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;

/**
 * Deterministic synthetic corpus generator (self-built, no external data).
 *
 * <p>The corpus deliberately mixes the tricky cases called out in the acceptance
 * criteria: the empty string, combining-character sequences in both NFC and NFD
 * spellings, families with long common prefixes, CJK text, emoji (supplementary
 * plane), and random mutations of base words.
 */
public final class CorpusGenerator {

    private static final String[] BASE_WORDS = {
        "apple", "application", "apply", "banana", "band", "bandana", "orange",
        "search", "research", "distance", "instance", "filter", "index", "candidate",
        "threshold", "levenshtein", "edit", "editor", "edition", "unicode",
        "normalization", "corpus", "query", "result", "service", "grape", "graph"
    };

    private static final String[] CJK_WORDS = {
        "编辑距离", "候选筛选", "阈值索引", "文本检索", "字符串", "规范化",
        "距离计算", "倒排索引", "精确匹配", "模糊查询"
    };

    private static final String[] COMBINING_WORDS = {
        "caf\u00E9", "cafe\u0301", "na\u00EFve", "nai\u0308ve",
        "r\u00E9sum\u00E9", "re\u0301sume\u0301", "Z\u00FCrich", "Zu\u0308rich",
        "\u00E5ngstr\u00F6m", "a\u030Angstro\u0308m"
    };

    private static final String[] EMOJI_WORDS = {
        "😀", "😃", "😀😃", "🚀launch", "🧪test", "👍", "🎉🎊", "caf\u00E9\u2615"
    };

    private static final String LONG_PREFIX =
            "the-quick-brown-fox-jumps-over-the-lazy-dog-";

    private CorpusGenerator() {
    }

    /**
     * Generates a corpus of roughly {@code size} terms (may be slightly smaller
     * after deduplication). The empty string is always included.
     */
    public static List<String> generate(int size, long seed) {
        Random random = new Random(seed);
        Set<String> terms = new LinkedHashSet<>();
        terms.add(""); // empty string must always be present

        for (String word : BASE_WORDS) {
            terms.add(word);
        }
        for (String word : CJK_WORDS) {
            terms.add(word);
        }
        for (String word : COMBINING_WORDS) {
            terms.add(word);
        }
        for (String word : EMOJI_WORDS) {
            terms.add(word);
        }
        // Long-common-prefix family: same 40+ char prefix, different suffixes.
        for (int i = 0; i < 20; i++) {
            terms.add(LONG_PREFIX + "v" + i);
        }

        while (terms.size() < size) {
            terms.add(mutate(pickBase(random), random));
        }
        return new ArrayList<>(terms);
    }

    private static String pickBase(Random random) {
        int roll = random.nextInt(10);
        if (roll < 5) {
            return BASE_WORDS[random.nextInt(BASE_WORDS.length)];
        }
        if (roll < 7) {
            return CJK_WORDS[random.nextInt(CJK_WORDS.length)];
        }
        if (roll < 9) {
            return LONG_PREFIX + "v" + random.nextInt(20);
        }
        return EMOJI_WORDS[random.nextInt(EMOJI_WORDS.length)];
    }

    /** Applies 1-3 random code-point edits (insert/delete/substitute) to a base term. */
    private static String mutate(String base, Random random) {
        int edits = 1 + random.nextInt(3);
        StringBuilder sb = new StringBuilder(base);
        for (int e = 0; e < edits; e++) {
            int op = random.nextInt(3);
            int len = sb.length();
            if (op == 0 || len == 0) { // insert a random BMP letter
                int pos = random.nextInt(len + 1);
                sb.insert(pos, (char) ('a' + random.nextInt(26)));
            } else if (op == 1) { // delete one UTF-16 unit region aligned to code points
                int cpIndex = random.nextInt(sb.codePointCount(0, len));
                int offset = sb.offsetByCodePoints(0, cpIndex);
                sb.delete(offset, sb.offsetByCodePoints(offset, 1));
            } else { // substitute one code point
                int cpIndex = random.nextInt(sb.codePointCount(0, len));
                int offset = sb.offsetByCodePoints(0, cpIndex);
                sb.replace(offset, sb.offsetByCodePoints(offset, 1),
                        String.valueOf((char) ('a' + random.nextInt(26))));
            }
        }
        return sb.toString();
    }
}
