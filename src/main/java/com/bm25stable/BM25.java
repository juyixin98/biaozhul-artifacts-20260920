package com.bm25stable;

/**
 * BM25 打分，参数固定：k1 = 1.2，b = 0.75。
 *
 * <p>IDF 采用 +1 平滑变体（保证非负）：
 * <pre>idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))</pre>
 *
 * <p>文档得分（查询词去重后求和）：
 * <pre>score(d) = Σ idf(t) * tf(t,d) * (k1 + 1) / (tf(t,d) + k1 * (1 - b + b * dl / avgdl))</pre>
 */
public final class BM25 {

    public static final double K1 = 1.2;
    public static final double B = 0.75;

    private BM25() {
    }

    /** 平滑 IDF，N 为文档总数，df 为包含该词的文档数。 */
    public static double idf(int totalDocs, int docFreq) {
        return Math.log(1.0 + (totalDocs - docFreq + 0.5) / (docFreq + 0.5));
    }

    /** 单个查询词对单篇文档的贡献分。 */
    public static double termScore(int termFreq, int docLength, double avgDocLength, int totalDocs, int docFreq) {
        double idf = idf(totalDocs, docFreq);
        double denom = termFreq + K1 * (1.0 - B + B * docLength / avgDocLength);
        return idf * (termFreq * (K1 + 1.0)) / denom;
    }
}
