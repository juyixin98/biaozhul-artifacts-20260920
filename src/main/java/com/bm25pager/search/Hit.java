package com.bm25pager.search;

import com.bm25pager.model.Document;

/**
 * 一条检索命中。score 为该文档相对查询的 BM25 总分。
 */
public final class Hit {

    private final String docId;
    private final double score;
    private final Document document;

    public Hit(String docId, double score, Document document) {
        this.docId = docId;
        this.score = score;
        this.document = document;
    }

    public String docId() {
        return docId;
    }

    public double score() {
        return score;
    }

    public Document document() {
        return document;
    }
}
