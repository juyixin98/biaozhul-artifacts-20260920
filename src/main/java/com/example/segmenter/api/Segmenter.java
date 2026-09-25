package com.example.segmenter.api;

import com.example.segmenter.model.Dictionary;
import com.example.segmenter.model.Segmentation;
import com.example.segmenter.model.Token;
import com.example.segmenter.model.Trie;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

/**
 * 基于词典代价的中文最小代价分词器（动态规划 / Viterbi）。
 *
 * <h2>模型</h2>
 * <ul>
 *   <li>原文按 Unicode 码点编号 0..n；位置 i 到 j 的边对应一个候选词。</li>
 *   <li>词典词代价 cost(w) = -ln(freq(w) / F)（见 {@link Dictionary}）。</li>
 *   <li>任何不在词典中的单字都产生一条“未知字”边，代价为固定值
 *       {@code unknownCharCost}（默认 {@code 10.0}）。因此任意输入（含生僻字、
 *       标点、空白）都能被完整覆盖，未知字符代价明确。</li>
 * </ul>
 *
 * <h2>同分确定性决胜（总顺序）</h2>
 * 两条路径按以下顺序比较，任一维度不同即定序：
 * <ol>
 *   <li>总代价升序（按 double 精确比较，所有代价均为有限值）；</li>
 *   <li>词数升序（越少越好，倾向长词）；</li>
 *   <li>词序列从左到右按 Unicode 码点字典序升序（逐词、逐码点比较）；</li>
 *   <li>以上全相同时（例如某字既按词典单字词、又按未知字走），
 *       按“未知标记序列”决胜：词典词(false)先于未知词(true)。</li>
 * </ol>
 * 比较沿假设链从右向左进行：先递归比较前驱（更早的词），再比较当前最右侧词，
 * 全程不额外分配字符串；每个桶内保留的是全序下的前 N 条互异路径，
 * 因此 1-best 与 N-best 的第 1 名永远一致，结果与松弛顺序无关、可重复。
 *
 * <h2>复杂度</h2>
 * 设最长词为 L、请求 N 条路径：时间 O(n · L · N · log(LN))，空间 O(n · N)，
 * 对输入长度和 N 都是多项式级，不枚举 2^(n-1) 种切分。
 */
public final class Segmenter {

    public static final double DEFAULT_UNKNOWN_CHAR_COST = 10.0;
    public static final int MAX_N_BEST = 64;

    private final Dictionary dictionary;
    private final Trie trie;
    private final double unknownCharCost;

    public Segmenter(Dictionary dictionary) {
        this(dictionary, DEFAULT_UNKNOWN_CHAR_COST);
    }

    public Segmenter(Dictionary dictionary, double unknownCharCost) {
        if (!Double.isFinite(unknownCharCost)) {
            throw new IllegalArgumentException("unknownCharCost must be finite");
        }
        this.dictionary = dictionary;
        this.trie = new Trie(dictionary);
        this.unknownCharCost = unknownCharCost;
    }

    public Dictionary dictionary() {
        return dictionary;
    }

    public double unknownCharCost() {
        return unknownCharCost;
    }

    /** 最小代价切分（1-best）。 */
    public Segmentation segment(String text) {
        return segmentNBest(text, 1).get(0);
    }

    /**
     * N 最佳切分（1 &lt;= n &lt;= {@value #MAX_N_BEST}）。
     * 返回列表按全序比较器排列；候选不足时列表可能小于 n
     * （空串返回 1 条空路径；非空输入因单字边恒存在至少有 1 条）。
     */
    public List<Segmentation> segmentNBest(String text, int n) {
        if (text == null) {
            throw new IllegalArgumentException("text must not be null");
        }
        if (n < 1 || n > MAX_N_BEST) {
            throw new IllegalArgumentException("n must be in [1, " + MAX_N_BEST + "], got " + n);
        }
        int[] cps = text.codePoints().toArray();
        int length = cps.length;

        if (length == 0) {
            return List.of(new Segmentation(List.of(), 0.0, 1));
        }

        // dp[i]：到达位置 i 的候选假设桶。来自前驱 i'<i 的边在处理 i' 时
        // 直接追加到对应终点桶中；进入位置 i 的处理时，所有前驱都已松弛完毕，
        // 此刻再 prune 成全序前 n 条（list-Viterbi：每个节点保留前 n 条即可，
        // 因为全序在“追加相同后缀”下保持）。
        List<List<Hypothesis>> dp = new ArrayList<>(length + 1);
        for (int i = 0; i <= length; i++) {
            dp.add(null);
        }
        dp.set(0, new ArrayList<>(Collections.singletonList(Hypothesis.START)));

        for (int i = 0; i < length; i++) {
            List<Hypothesis> bucket = dp.get(i);

            // 1) 未知单字边 i -> i+1：每个前驱无条件产生一条（即便该单字也在
            //    词典中——“按词典单字词走”和“按未知字走”是代价/标记不同的两条候选）。
            int[] unkWord = new int[]{cps[i]};
            for (Hypothesis pred : bucket) {
                append(dp, i + 1, new Hypothesis(pred, i, i + 1, unkWord, unknownCharCost, true));
            }

            // 2) 词典边 i -> j+1：从 i 沿 Trie 行走，命中终止节点即松弛到对应终点桶。
            Trie.Node node = trie.root();
            for (int j = i; j < length; j++) {
                Trie.Node child = node.children().get(cps[j]);
                if (child == null) {
                    break;
                }
                node = child;
                if (node.isTerminal()) {
                    double edgeCost = node.cost();
                    int[] word = Arrays.copyOfRange(cps, i, j + 1);
                    for (Hypothesis pred : bucket) {
                        append(dp, j + 1, new Hypothesis(pred, i, j + 1, word, edgeCost, false));
                    }
                }
            }

            // 下一轮进入 i+1 前定稿其候选桶（此刻 i+1 的所有前驱都已处理完）。
            List<Hypothesis> raw = dp.get(i + 1);
            dp.set(i + 1, prune(raw, n));
        }

        List<Hypothesis> finals = dp.get(length);
        List<Segmentation> result = new ArrayList<>(finals.size());
        for (int r = 0; r < finals.size(); r++) {
            result.add(toSegmentation(finals.get(r), r + 1, cps));
        }
        return result;
    }

    /** 向位置 j 的原始候选桶追加一条假设（惰性建桶）。 */
    private static void append(List<List<Hypothesis>> dp, int j, Hypothesis h) {
        List<Hypothesis> bucket = dp.get(j);
        if (bucket == null) {
            bucket = new ArrayList<>();
            dp.set(j, bucket);
        }
        bucket.add(h);
    }

    /** 合并松弛产生的候选，按全序去重后保留前 n 条。 */
    private static List<Hypothesis> prune(List<Hypothesis> candidates, int n) {
        candidates.sort(Hypothesis.ORDER);
        List<Hypothesis> kept = new ArrayList<>(Math.min(n, candidates.size()));
        for (Hypothesis h : candidates) {
            if (kept.size() >= n) {
                break;
            }
            boolean duplicate = false;
            for (Hypothesis k : kept) {
                if (samePath(k, h)) {
                    duplicate = true;
                    break;
                }
            }
            if (!duplicate) {
                kept.add(h);
            }
        }
        return kept;
    }

    /** 两条假设是否为同一条切分路径（边界面与未知标记逐边相同）。 */
    private static boolean samePath(Hypothesis a, Hypothesis b) {
        Hypothesis x = a;
        Hypothesis y = b;
        while (x != Hypothesis.START || y != Hypothesis.START) {
            if (x == Hypothesis.START || y == Hypothesis.START) {
                return false;
            }
            if (x.unknown != y.unknown || x.start != y.start || x.end != y.end) {
                return false;
            }
            x = x.pred;
            y = y.pred;
        }
        return true;
    }
    private Segmentation toSegmentation(Hypothesis h, int rank, int[] cps) {
        List<Token> reversed = new ArrayList<>();
        Hypothesis cur = h;
        while (cur != Hypothesis.START) {
            String word = new String(cur.word, 0, cur.word.length);
            double cost = cur.unknown ? unknownCharCost : dictionary.costOf(word);
            reversed.add(new Token(word, cost, cur.unknown));
            cur = cur.pred;
        }
        Collections.reverse(reversed);
        return new Segmentation(reversed, h.cost, rank);
    }

    /** 路径假设：链表节点，pred 指向前驱位置的假设，边覆盖 [start, end)。 */
    private static final class Hypothesis {
        static final Hypothesis START = new Hypothesis(null, 0, 0, null, 0.0, false);

        final Hypothesis pred;
        final int start;
        final int end;
        final int[] word;     // 该边词面的码点序列
        final double cost;    // 从起点累计的总代价
        final int tokens;     // 从起点到该假设的词数
        final boolean unknown;

        Hypothesis(Hypothesis pred, int start, int end, int[] word, double edgeCost, boolean unknown) {
            this.pred = pred;
            this.start = start;
            this.end = end;
            this.word = word;
            this.cost = (pred == null ? 0.0 : pred.cost) + edgeCost;
            this.tokens = pred == null ? 0 : pred.tokens + 1;
            this.unknown = unknown;
        }

        /** 总顺序：代价 → 词数 → 词序列码点字典序。 */
        static final Comparator<Hypothesis> ORDER = (a, b) -> {
            if (a == b) return 0;
            int byCost = Double.compare(a.cost, b.cost);
            if (byCost != 0) return byCost;
            int byCount = Integer.compare(a.tokens, b.tokens);
            if (byCount != 0) return byCount;
            return compareWords(a, b);
        };

        /**
         * 词序列字典序（仅在总代价与词数都相同后调用，故两链等长、同步到达 START）。
         *
         * <p>迭代实现（避免沿长链递归导致栈溢出）：从最右一条边向左走，每当当前边对
         * 出现差异就覆盖记录——后看到的边更靠左，而字典序由最左侧第一个差异决定，
         * 因此一趟结束后留下的即最终结果。每条边先比码点序列，再比未知标记。</p>
         */
        private static int compareWords(Hypothesis a, Hypothesis b) {
            int result = 0;
            Hypothesis x = a;
            Hypothesis y = b;
            while (x != START && y != START) {
                int c = compareCodePoints(x.word, y.word);
                if (c == 0) {
                    // 词面相同（单字既在词典中又按未知字走）：词典边优先于未知边。
                    c = Boolean.compare(x.unknown, y.unknown);
                }
                if (c != 0) {
                    result = c; // 当前边比之前记录的更靠左，覆盖
                }
                x = x.pred;
                y = y.pred;
            }
            return result;
        }

        private static int compareCodePoints(int[] x, int[] y) {
            int len = Math.min(x.length, y.length);
            for (int k = 0; k < len; k++) {
                int c = Integer.compare(x[k], y[k]);
                if (c != 0) return c;
            }
            return Integer.compare(x.length, y.length);
        }
    }
}
