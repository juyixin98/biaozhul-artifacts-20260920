package com.bm25stable;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 不可变索引快照。每次语料发生变化时整体重建一个新快照，
 * 旧快照在保留期内继续服务旧游标，因此更新不会影响已开始的分页。
 */
public final class IndexSnapshot {

    private final int version;
    private final long createdAtMillis;
    private final int docCount;
    private final double avgDocLength;
    /** 文档 id 字典序排列。 */
    private final List<String> docIds;
    private final Map<String, String> texts;
    private final Map<String, Integer> docLengths;
    /** term -> (docId -> tf)，内层按 docId 升序。 */
    private final Map<String, Map<String, Integer>> invertedIndex;

    private IndexSnapshot(int version, long createdAtMillis, int docCount, double avgDocLength,
                          List<String> docIds, Map<String, String> texts,
                          Map<String, Integer> docLengths,
                          Map<String, Map<String, Integer>> invertedIndex) {
        this.version = version;
        this.createdAtMillis = createdAtMillis;
        this.docCount = docCount;
        this.avgDocLength = avgDocLength;
        this.docIds = docIds;
        this.texts = texts;
        this.docLengths = docLengths;
        this.invertedIndex = invertedIndex;
    }

    /** 由文档集合构建快照，version 由引擎分配（单调递增）。 */
    public static IndexSnapshot build(int version, Map<String, String> docs) {
        List<String> docIds = new ArrayList<>(docs.keySet());
        Collections.sort(docIds);

        Map<String, String> texts = new LinkedHashMap<>();
        Map<String, Integer> docLengths = new LinkedHashMap<>();
        Map<String, Map<String, Integer>> index = new HashMap<>();
        long totalLength = 0;

        for (String id : docIds) {
            String text = docs.get(id);
            texts.put(id, text);
            List<String> tokens = Tokenizer.tokenize(text);
            docLengths.put(id, tokens.size());
            totalLength += tokens.size();
            for (String token : tokens) {
                Map<String, Integer> postings = index.computeIfAbsent(token, k -> new LinkedHashMap<>());
                postings.merge(id, 1, Integer::sum);
            }
        }

        double avg = docIds.isEmpty() ? 0.0 : (double) totalLength / docIds.size();

        Map<String, Map<String, Integer>> frozenIndex = new HashMap<>();
        for (Map.Entry<String, Map<String, Integer>> e : index.entrySet()) {
            frozenIndex.put(e.getKey(), Collections.unmodifiableMap(e.getValue()));
        }

        return new IndexSnapshot(
                version,
                System.currentTimeMillis(),
                docIds.size(),
                avg,
                Collections.unmodifiableList(docIds),
                Collections.unmodifiableMap(texts),
                Collections.unmodifiableMap(docLengths),
                Collections.unmodifiableMap(frozenIndex));
    }

    public int version() {
        return version;
    }

    public long createdAtMillis() {
        return createdAtMillis;
    }

    public int docCount() {
        return docCount;
    }

    public double avgDocLength() {
        return avgDocLength;
    }

    public List<String> docIds() {
        return docIds;
    }

    public String textOf(String docId) {
        return texts.get(docId);
    }

    public int docLength(String docId) {
        return docLengths.get(docId);
    }

    /** 该词的倒排表（docId -> tf，按 docId 升序）；词不存在时返回 null。 */
    public Map<String, Integer> postings(String term) {
        return invertedIndex.get(term);
    }
}
