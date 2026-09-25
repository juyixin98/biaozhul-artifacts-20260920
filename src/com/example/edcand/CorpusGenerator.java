package com.example.edcand;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;

/**
 * 自建合成语料（不依赖任何外部语料或服务）。
 *
 * <p>固定随机种子，可重复生成。内容刻意覆盖：
 * <ul>
 *   <li>空串、单码点串；</li>
 *   <li>组合字符：NFC 与 NFD 两种表示的 é/ç/ñ 等；</li>
 *   <li>长公共前缀家族（{@code prefixed_*}）；</li>
 *   <li>增补平面字符：emoji（😀/🎉/🇨🇳 等代理对）；</li>
 *   <li>CJK、全角字符、连字 ﬁ、兼容数字；</li>
 *   <li>大小写/空白变体；</li>
 *   <li>基础英文小词及其插入/删除/替换噪声变体。</li>
 * </ul>
 */
public final class CorpusGenerator {

    private CorpusGenerator() {
    }

    /** 边界与特殊场景（每个都有明确测试意图）。 */
    public static List<String> edgeCases() {
        return Arrays.asList(
                "", // 空串
                "a", "A", // 单字符大小写
                "é", // NFC：U+00E9 预组合
                "é", // NFD：e + 组合重音 U+0301
                "café", "café",
                "naïve", "naïve",
                "ñoño",
                "日本語", "汉字", "東京", // CJK（BMP，每字 1 码点 3 字节）
                "😀", "🎉", "🇨🇳", // 增补平面：emoji 与区域指示符序列（代理对）
                "😀😁😂",
                "ａｂｃ", // 全角字母（NFKC 兼容归一）
                "ﬁnd", // 连字 ﬁ（NFKC → fi）
                "Ⅲ", // 罗马数字兼容字符（NFKC → iii）
                "   ", " hello ", "hello\tworld", // 空白变体
                "0", "42", "2026" // 数字
        );
    }

    /** 长公共前缀家族：相同长前缀 + 不同短后缀，用于考验前缀区分能力。 */
    public static List<String> longPrefixFamily() {
        List<String> out = new ArrayList<>();
        String prefix = "document_section_paragraph_";
        String[] tails = {"one", "two", "ten", "ton", "tonne", "tone",
                "tree", "three", "thirty", "closing", "closure", "cloth"};
        for (String tail : tails) {
            out.add(prefix + tail);
        }
        // 一个前缀本身被截断的成员
        out.add(prefix.substring(0, prefix.length() - 1));
        return out;
    }

    /** 基础英文小词表（噪声变体的来源，也是“随机小词表”对拍的词库之一）。 */
    public static List<String> baseWords() {
        return Arrays.asList(
                "time", "year", "people", "way", "day", "man", "woman", "child",
                "world", "life", "hand", "part", "place", "case", "week", "company",
                "system", "program", "question", "work", "government", "number", "night",
                "point", "home", "water", "room", "mother", "area", "money", "story",
                "fact", "month", "lot", "right", "study", "book", "eye", "job",
                "word", "business", "issue", "side", "kind", "head", "house", "service",
                "friend", "father", "power", "hour", "game", "line", "end", "member",
                "law", "car", "city", "community", "name", "team", "minute", "idea",
                "kid", "body", "back", "parent", "face", "others", "level", "office",
                "door", "health", "person", "art", "war", "history", "party", "result",
                "change", "morning", "reason", "research", "girl", "guy", "moment",
                "air", "teacher", "force", "education", "foot", "boy", "age", "policy",
                "music", "market", "sense", "nation", "plan", "college", "interest",
                "death", "experience", "effect", "use", "class", "control", "care",
                "field", "development", "role", "effort", "rate", "heart", "drug",
                "show", "leader", "light", "voice", "wife", "police", "mind", "price",
                "report", "decision", "hope", "view", "relationship", "town", "road",
                "arm", "difference", "culture", "hotel", "marriage", "freedom", "source");
    }

    /**
     * 生成默认合成语料（固定种子 20260924，可复现）。
     */
    public static List<String> defaultCorpus() {
        // 全量基础词（约 130 个）+ 每词 3 个噪声变体 + 边界与长前缀家族，固定种子可复现
        return generate(20260924L, 3, 3, 0);
    }

    /**
     * @param seed         随机种子
     * @param variantsEach 每个基础词生成的噪声变体数
     * @param maxEdits     每个变体最多编辑次数
     * @param wordLimit    使用的基础词数量上限（用于随机小词表场景）
     */
    public static List<String> generate(long seed, int variantsEach, int maxEdits, int wordLimit) {
        Set<String> corpus = new LinkedHashSet<>();
        corpus.addAll(edgeCases());
        corpus.addAll(longPrefixFamily());

        List<String> bases = baseWords();
        if (wordLimit > 0 && wordLimit < bases.size()) {
            bases = bases.subList(0, wordLimit);
        } // wordLimit <= 0 表示使用全部基础词
        corpus.addAll(bases);

        Random rnd = new Random(seed);
        String alphabet = "abcdefghijklmnopqrstuvwxyz";
        for (String base : bases) {
            for (int v = 0; v < variantsEach; v++) {
                corpus.add(mutate(base, rnd, alphabet, 1 + rnd.nextInt(maxEdits)));
            }
        }
        return new ArrayList<>(corpus);
    }

    /** 对码点序列随机做 1..edits 次插入/删除/替换；保证结果可与原串不同。 */
    static String mutate(String s, Random rnd, String alphabet, int edits) {
        int[] cps = CodePoints.of(s);
        for (int e = 0; e < edits; e++) {
            if (cps.length == 0) {
                cps = new int[]{alphabet.charAt(rnd.nextInt(alphabet.length()))};
                continue;
            }
            int op = rnd.nextInt(3);
            int pos = rnd.nextInt(cps.length + (op == 0 ? 1 : 0));
            switch (op) {
                case 0 -> { // 插入
                    int ch = alphabet.charAt(rnd.nextInt(alphabet.length()));
                    int[] next = new int[cps.length + 1];
                    System.arraycopy(cps, 0, next, 0, pos);
                    next[pos] = ch;
                    System.arraycopy(cps, pos, next, pos + 1, cps.length - pos);
                    cps = next;
                }
                case 1 -> { // 删除
                    if (pos < cps.length) {
                        int[] next = new int[cps.length - 1];
                        System.arraycopy(cps, 0, next, 0, pos);
                        System.arraycopy(cps, pos + 1, next, pos, cps.length - pos - 1);
                        cps = next;
                    }
                }
                default -> { // 替换
                    if (pos < cps.length) {
                        cps[pos] = alphabet.charAt(rnd.nextInt(alphabet.length()));
                    }
                }
            }
        }
        return CodePoints.toString(cps);
    }
}
