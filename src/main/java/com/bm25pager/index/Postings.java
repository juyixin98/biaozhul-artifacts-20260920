package com.bm25pager.index;

import java.util.Collections;
import java.util.Map;

/**
 * 单个词项（term）的倒排记录：文档频率 df 与各文档内词频 tf。
 */
public final class Postings {

    private final int docFreq;
    private final Map<String, Integer> termFreqByDoc;

    public Postings(Map<String, Integer> termFreqByDoc) {
        this.termFreqByDoc = Collections.unmodifiableMap(termFreqByDoc);
        this.docFreq = termFreqByDoc.size();
    }

    /** 包含该词项的文档数 n。 */
    public int docFreq() {
        return docFreq;
    }

    /** 该词项在指定文档中的词频，不存在返回 0。 */
    public int termFreq(String docId) {
        return termFreqByDoc.getOrDefault(docId, 0);
    }

    public Map<String, Integer> termFreqByDoc() {
        return termFreqByDoc;
    }
}
