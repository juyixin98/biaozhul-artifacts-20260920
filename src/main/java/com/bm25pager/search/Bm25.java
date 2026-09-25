package com.bm25pager.search;

import com.bm25pager.index.Postings;
import java.util.Map;

/**
 * BM25 打分器，参数全局固定（不允许通过 API 调整）：
 *
 *   k1 = 1.2, b = 0.75
 *
 * IDF 采用 Lucene 经典形式：
 *
 *   idf(t) = log(1 + (N - n + 0.5) / (n + 0.5))
 *
 *   N：快照内文档总数；n：包含词项 t 的文档数。
 *   该形式恒为非负数，避免了经典 Robertson IDF 在 n &gt; N/2 时出现负分的问题。
 *
 * 文档总分：
 *
 *   score(D, Q) = Σ_t idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * |D| / avgdl))
 *
 * 仅对文档实际包含的查询词项求和；空文档对任何查询总分均为 0。
 */
public final class Bm25 {

    /** 词频饱和参数。 */
    public static final double K1 = 1.2d;

    /** 文档长度归一化参数。 */
    public static final double B = 0.75d;

    private Bm25() {
    }

    public static double idf(int docCount, int docFreq) {
        return Math.log(1.0d + (docCount - docFreq + 0.5d) / (docFreq + 0.5d));
    }

    /**
     * @param termFreq       词项在该文档中的词频（0 时返回 0）
     * @param docLength      该文档长度（token 数）
     * @param avgDocLength   快照平均文档长度
     * @param idf            该词项的 idf
     */
    public static double termScore(int termFreq, int docLength, double avgDocLength, double idf) {
        if (termFreq <= 0 || avgDocLength <= 0.0d) {
            return 0.0d;
        }
        double norm = 1.0d - B + B * (docLength / avgDocLength);
        return idf * (termFreq * (K1 + 1.0d)) / (termFreq + K1 * norm);
    }

    /**
     * 计算单个文档对一组查询词项的总分。
     *
     * @param postingsByTerm 查询词项 -> 倒排记录（调用方负责去重）
     */
    public static double score(String docId,
                               int docLength,
                               double avgDocLength,
                               int docCount,
                               Map<String, Postings> postingsByTerm) {
        double total = 0.0d;
        for (Map.Entry<String, Postings> e : postingsByTerm.entrySet()) {
            Postings p = e.getValue();
            int tf = p.termFreq(docId);
            if (tf > 0) {
                total += termScore(tf, docLength, avgDocLength, idf(docCount, p.docFreq()));
            }
        }
        return total;
    }
}
