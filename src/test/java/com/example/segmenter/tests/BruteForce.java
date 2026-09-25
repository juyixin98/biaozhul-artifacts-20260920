package com.example.segmenter.tests;

import com.example.segmenter.model.Dictionary;
import com.example.segmenter.model.Segmentation;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * 穷举参考实现：枚举全部 2^(n-1) 种切分（仅测试用，句子很短），
 * 用于与动态规划结果逐条对照。
 *
 * <p>路径总顺序与 {@link com.example.segmenter.api.Segmenter} 完全一致：
 * 代价 → 词数 → 词面码点字典序 → 未知标记序列。</p>
 */
public final class BruteForce {

    private final Dictionary dictionary;
    private final double unknownCost;

    public BruteForce(Dictionary dictionary, double unknownCost) {
        this.dictionary = dictionary;
        this.unknownCost = unknownCost;
    }

    /** 穷举并返回全序排序、去重后的全部路径。 */
    public List<Segmentation> enumerate(String text) {
        int[] cps = text.codePoints().toArray();
        List<Path> all = new ArrayList<>();
        if (cps.length == 0) {
            all.add(new Path(List.of(), 0.0, List.of()));
        } else {
            enumerate(cps, 0, new ArrayList<>(), new ArrayList<>(), 0.0, all);
        }
        all.sort(PATH_ORDER);
        List<Path> deduped = new ArrayList<>();
        for (Path p : all) {
            if (deduped.isEmpty() || PATH_ORDER.compare(deduped.get(deduped.size() - 1), p) != 0) {
                deduped.add(p);
            }
        }
        List<Segmentation> result = new ArrayList<>();
        for (int i = 0; i < deduped.size(); i++) {
            Path p = deduped.get(i);
            List<com.example.segmenter.model.Token> tokens = new ArrayList<>();
            for (int k = 0; k < p.words.size(); k++) {
                String w = p.words.get(k);
                boolean unk = p.unknown.get(k);
                tokens.add(new com.example.segmenter.model.Token(
                        w, unk ? unknownCost : dictionary.costOf(w), unk));
            }
            result.add(new Segmentation(tokens, p.cost, i + 1));
        }
        return result;
    }

    private void enumerate(int[] cps, int start, List<String> words, List<Boolean> unknown,
                           double cost, List<Path> out) {
        int n = cps.length;
        if (start == n) {
            out.add(new Path(List.copyOf(words), cost, List.copyOf(unknown)));
            return;
        }
        // 边 1：未知单字（任何位置都可行）
        String single = new String(Character.toChars(cps[start]));
        words.add(single);
        unknown.add(true);
        enumerate(cps, start + 1, words, unknown, cost + unknownCost, out);
        words.remove(words.size() - 1);
        unknown.remove(unknown.size() - 1);

        // 边 2..：词典词（长度 >=1；长度为 1 的词典词是与未知字不同的候选）
        StringBuilder sb = new StringBuilder();
        for (int end = start; end < n; end++) {
            sb.appendCodePoint(cps[end]);
            String candidate = sb.toString();
            Double dictCost = dictionary.costOf(candidate);
            if (dictCost != null) {
                boolean isSingle = end == start;
                words.add(candidate);
                unknown.add(false);
                enumerate(cps, end + 1, words, unknown, cost + dictCost, out);
                words.remove(words.size() - 1);
                unknown.remove(unknown.size() - 1);
                if (isSingle) {
                    // 词典单字词与未知单字词是两条不同的边，均已枚举。
                }
            }
        }
    }

    private static final class Path {
        final List<String> words;
        final double cost;
        final List<Boolean> unknown;
        final int count;

        Path(List<String> words, double cost, List<Boolean> unknown) {
            this.words = words;
            this.cost = cost;
            this.unknown = unknown;
            this.count = words.size();
        }
    }

    static final Comparator<Path> PATH_ORDER = (a, b) -> {
        int byCost = Double.compare(a.cost, b.cost);
        if (byCost != 0) return byCost;
        int byCount = Integer.compare(a.count, b.count);
        if (byCount != 0) return byCount;
        int k = Math.min(a.words.size(), b.words.size());
        for (int i = 0; i < k; i++) {
            int c = compareByCodePoint(a.words.get(i), b.words.get(i));
            if (c != 0) return c;
        }
        int byLen = Integer.compare(a.words.size(), b.words.size());
        if (byLen != 0) return byLen;
        for (int i = 0; i < a.unknown.size(); i++) {
            int c = Boolean.compare(a.unknown.get(i), b.unknown.get(i));
            if (c != 0) return c;
        }
        return 0;
    };

    static int compareByCodePoint(String x, String y) {
        int[] ax = x.codePoints().toArray();
        int[] by = y.codePoints().toArray();
        int len = Math.min(ax.length, by.length);
        for (int i = 0; i < len; i++) {
            int c = Integer.compare(ax[i], by[i]);
            if (c != 0) return c;
        }
        return Integer.compare(ax.length, by.length);
    }
}
