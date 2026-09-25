package com.example.edcand;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 阈值候选索引（threshold candidate index）。
 *
 * <h2>两阶段结构</h2>
 * <ol>
 *   <li><b>粗筛（保证召回，不允许漏）</b>：长度门 + 二元组（q=2）多重集重叠下界；</li>
 *   <li><b>精算确认（保证精度）</b>：对粗筛候选逐一计算精确的码点 Levenshtein
 *       （{@link Levenshtein#cappedDistance}），只有距离 &le; 阈值才输出。</li>
 * </ol>
 *
 * <h2>为什么粗筛不漏（q-gram 引理）</h2>
 * <p>对长度 m、n 的码点序列 x、y，两端各补 q-1 个哨兵（码点 -1）后取
 * 相邻 q 元组的<b>多重集</b> G(x)、G(y)，其大小为 |G(x)| = m + q - 1。
 * 若 Levenshtein(x,y) = d，则一次编辑至多破坏 q 个 q 元组，故
 * <pre>  |G(x) ∩ G(y)（多重集交）| &ge; max(m,n) + q - 1 - q&middot;d.</pre>
 * 因此 d &le; k 的词项，其重叠度必定 &ge; {@code max(m,n)+q-1-q*k}。
 * 再加上必要条件 {@code |m-n| &le; k}（长度门）。两个条件都是<b>必要条件</b>，
 * 所以只可能产生多余候选（假阳性，由精算排除），<b>不可能漏掉</b>阈值内结果。
 * 该性质由 {@code IndexRecallTest} 的全扫描随机对拍强制验证。
 *
 * <p>多重集交按“共同 q 元组、频次取 min”计数；倒排表中每个 (词项, 元组)
 * 保存该元组在词项中的出现次数，打分时与查询中的次数取 min。
 */
public final class CandidateIndex {

    /** q-gram 的 q：使用二元组（对短词更友好）。 */
    public static final int Q = 2;

    /** 哨兵码点，不属于任何合法 Unicode scalar（合法范围 0..0x10FFFF）。 */
    private static final int SENTINEL = -1;

    /** 编码基数：合法码点最大值 +1，再给哨兵留 +1 的平移空间。 */
    private static final long BASE = 0x110001L;

    private record Entry(int[] cps, String norm, String original) {
    }

    private final TextNormalization normalization;
    private final List<Entry> entries = new ArrayList<>();
    /** 长度（码点数）→ 该长度的 entry 下标。 */
    private final Map<Integer, List<Integer>> lengthBuckets = new HashMap<>();
    /** 二元组键 → 倒排表，每项 [entryId, 该元组在词项中的频次]。 */
    private final Map<Long, List<int[]>> postings = new HashMap<>();

    public CandidateIndex(TextNormalization normalization) {
        this.normalization = normalization == null ? TextNormalization.NFC : normalization;
    }

    /** 用默认 NFC 规范化构建。 */
    public static CandidateIndex build(List<String> terms) {
        return build(terms, TextNormalization.NFC);
    }

    /** 从词表构建索引：两侧统一规范化，按规范化结果去重（保留首个原始串）。 */
    public static CandidateIndex build(List<String> terms, TextNormalization norm) {
        CandidateIndex idx = new CandidateIndex(norm);
        Set<String> seen = new HashSet<>();
        for (String t : terms) {
            if (t == null) {
                continue;
            }
            String n = norm.apply(t);
            if (!seen.add(n)) {
                continue;
            }
            int id = idx.entries.size();
            int[] cps = CodePoints.of(n);
            idx.entries.add(new Entry(cps, n, n.equals(t) ? null : t));
            idx.lengthBuckets.computeIfAbsent(cps.length, k -> new ArrayList<>()).add(id);

            Map<Long, Integer> freq = profile(cps);
            for (Map.Entry<Long, Integer> e : freq.entrySet()) {
                idx.postings.computeIfAbsent(e.getKey(), g -> new ArrayList<>())
                        .add(new int[]{id, e.getValue()});
            }
        }
        return idx;
    }

    public int size() {
        return entries.size();
    }

    /** 已规范化词项的只读视图（测试对拍用）。 */
    List<String> normalizedTerms() {
        List<String> out = new ArrayList<>(entries.size());
        for (Entry e : entries) {
            out.add(e.norm());
        }
        return Collections.unmodifiableList(out);
    }

    /**
     * 候选筛选 + 精算确认。
     *
     * @param rawQuery 查询串（构建时同样的规范化会先作用于它）
     * @param threshold 距离阈值 k（含），必须 &ge; 0
     */
    public SearchOutcome search(String rawQuery, int threshold) {
        if (rawQuery == null) {
            throw new IllegalArgumentException("query must not be null");
        }
        if (threshold < 0) {
            throw new IllegalArgumentException("threshold must be >= 0");
        }
        String qnorm = normalization.apply(rawQuery);
        int[] q = CodePoints.of(qnorm);
        int m = q.length;

        // ---- 第一阶段：长度门 ----
        List<Integer> inRange = new ArrayList<>();
        for (int len = Math.max(0, m - threshold); len <= m + threshold; len++) {
            List<Integer> bucket = lengthBuckets.get(len);
            if (bucket != null) {
                inRange.addAll(bucket);
            }
        }
        int passedLengthGate = inRange.size();

        // ---- 第一阶段：q-gram 多重集重叠下界 ----
        Map<Long, Integer> qfreq = profile(q);
        int[] score = new int[entries.size()];
        for (Map.Entry<Long, Integer> qe : qfreq.entrySet()) {
            List<int[]> plist = postings.get(qe.getKey());
            if (plist == null) {
                continue;
            }
            int fq = qe.getValue();
            for (int[] p : plist) {
                score[p[0]] += Math.min(p[1], fq); // 多重集交：频次取 min
            }
        }
        // 对具体候选的 q-gram 重叠下界是 max(m,n)+q-1-qk（q-gram 引理，见类注释）。
        // 长度门内 |m-n|<=k，下面逐候选按其自身长度 n 计算，是精确必要条件，不漏。
        List<Entry> candidates = new ArrayList<>();
        for (int id : inRange) {
            Entry e = entries.get(id);
            long t = (long) Math.max(m, e.cps.length) + Q - 1 - (long) Q * threshold;
            if (score[id] >= t) {
                candidates.add(e);
            }
        }

        // ---- 第二阶段：逐个精算确认（阈值内返回精确距离）----
        List<SearchOutcome.Match> matches = new ArrayList<>();
        for (Entry e : candidates) {
            int d = Levenshtein.cappedDistance(q, e.cps, threshold);
            if (d <= threshold) {
                matches.add(new SearchOutcome.Match(e.norm(), e.original(), d));
            }
        }
        matches.sort(Comparator
                .comparingInt(SearchOutcome.Match::distance)
                .thenComparing(SearchOutcome.Match::term));

        return new SearchOutcome(Collections.unmodifiableList(matches),
                entries.size(), passedLengthGate, candidates.size(), candidates.size());
    }

    /**
     * 两端补 q-1 个哨兵后，生成相邻 q 元组的频次表。
     * 序列长度为 n 时，窗口数为 n + q - 1（n=0 时恰有一个“哨兵-哨兵”元组）。
     */
    private static Map<Long, Integer> profile(int[] cps) {
        int windows = cps.length + Q - 1;
        Map<Long, Integer> freq = new HashMap<>(Math.max(4, windows));
        for (int w = 0; w < windows; w++) {
            long key = 0;
            for (int off = 0; off < Q; off++) {
                int pos = w + off - (Q - 1);
                int cp = (pos >= 0 && pos < cps.length) ? cps[pos] : SENTINEL;
                key = key * BASE + (cp + 1);
            }
            freq.merge(key, 1, Integer::sum);
        }
        return freq;
    }
}
