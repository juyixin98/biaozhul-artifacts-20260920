package com.bm25stable;

/** 单条命中：文档 id + BM25 得分。 */
public record SearchHit(String docId, double score) {
}
