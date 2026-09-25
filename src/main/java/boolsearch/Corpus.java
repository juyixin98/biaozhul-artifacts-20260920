package boolsearch;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Random;

/** 自建合成语料：固定小词表 + 种子随机生成，保证可复现。 */
public final class Corpus {
    public static final String[] VOCAB = {
            "apple", "banana", "cherry", "grape", "lemon", "melon",
            "orange", "peach", "pear", "plum", "kiwi", "mango"
    };

    private Corpus() {}

    /** 生成 docs 篇文档（id 从 1 开始），每篇 3~8 个词。 */
    public static Map<Integer, String> generate(int docs, long seed) {
        Random r = new Random(seed);
        Map<Integer, String> out = new LinkedHashMap<>();
        for (int id = 1; id <= docs; id++) {
            int n = 3 + r.nextInt(6);
            StringBuilder sb = new StringBuilder();
            for (int k = 0; k < n; k++) {
                if (k > 0) sb.append(' ');
                sb.append(VOCAB[r.nextInt(VOCAB.length)]);
            }
            out.put(id, sb.toString());
        }
        return out;
    }
}
