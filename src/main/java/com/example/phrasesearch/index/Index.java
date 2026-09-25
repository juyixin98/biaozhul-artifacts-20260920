package com.example.phrasesearch.index;

import com.example.phrasesearch.analyze.Analyzer;
import com.example.phrasesearch.model.AnalyzedField;
import com.example.phrasesearch.model.Document;
import com.example.phrasesearch.model.GlobalToken;
import com.example.phrasesearch.model.IndexedDoc;
import com.example.phrasesearch.model.Posting;
import com.example.phrasesearch.model.Token;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 内存倒排索引。
 *
 * 对每篇文档：
 *  1. 用分析器切分每个字段（得到字段内局部位置）；
 *  2. 按字段声明顺序拼接为全局位置流——字段 B 第一个 token 的全局位置
 *     = 字段 A 的 positionCount（含停用词/空洞），字段之间不加额外空隙；
 *  3. 写入倒排表 term -> 有序 Posting 列表。
 *
 * 每条 Posting 同时带全局位置（跨字段短语用）和局部位置（字段内短语用），
 * 字段内检索时按 field 过滤即可，无需第二份倒排表。
 */
public final class Index {

    private final Analyzer analyzer;
    private final List<IndexedDoc> docs = new ArrayList<>();
    private final Map<String, List<Posting>> inverted = new TreeMap<>();
    private final Map<String, Integer> idToDocId = new LinkedHashMap<>();

    public Index(Analyzer analyzer) {
        this.analyzer = analyzer;
    }

    public Analyzer analyzer() {
        return analyzer;
    }

    public synchronized int addDocument(Document doc) {
        if (idToDocId.containsKey(doc.id())) {
            throw new IllegalArgumentException("duplicate document id: " + doc.id());
        }
        int docId = docs.size();

        List<String> fieldOrder = doc.orderedFieldNames();
        List<GlobalToken> globalTokens = new ArrayList<>();

        int globalBase = 0;
        for (String fieldName : fieldOrder) {
            AnalyzedField af = analyzer.analyze(fieldName, doc.field(fieldName));
            for (Token t : af.tokens()) {
                int globalPos = globalBase + t.position();
                globalTokens.add(new GlobalToken(t.term(), globalPos, fieldName,
                        t.position(), t.startOffset(), t.endOffset()));

                Posting p = new Posting(docId, globalPos, t.position(), fieldName,
                        t.startOffset(), t.endOffset());
                inverted.computeIfAbsent(t.term(), k -> new ArrayList<>()).add(p);
            }
            // 下一字段紧接：基址前移本字段消耗的全部位置槽（含尾部被删停用词的洞）
            globalBase += af.positionCount();
        }

        // globalTokens 按字段顺序拼接、字段内局部位置严格递增 => 全局位置天然严格递增，无需排序。
        IndexedDoc indexed = new IndexedDoc(docId, doc.id(), fieldOrder, List.copyOf(globalTokens));
        docs.add(indexed);
        idToDocId.put(doc.id(), docId);
        return docId;
    }

    /** 返回词项的全部倒排记录（按 docId、position、field 有序，不可变）；不存在返回空列表。 */
    public synchronized List<Posting> postings(String term) {
        List<Posting> list = inverted.get(term);
        return list == null ? List.of() : Collections.unmodifiableList(list);
    }

    public synchronized IndexedDoc doc(int docId) {
        return docs.get(docId);
    }

    public synchronized IndexedDoc docByExternalId(String externalId) {
        Integer id = idToDocId.get(externalId);
        return id == null ? null : docs.get(id);
    }

    public synchronized List<IndexedDoc> allDocs() {
        return List.copyOf(docs);
    }

    public synchronized int size() {
        return docs.size();
    }

    public synchronized List<String> vocabulary() {
        return List.copyOf(inverted.keySet());
    }
}
