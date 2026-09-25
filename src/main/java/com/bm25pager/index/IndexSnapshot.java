package com.bm25pager.index;

import com.bm25pager.model.Document;
import com.bm25pager.search.Bm25;
import com.bm25pager.search.Hit;
import com.bm25pager.text.Tokenizer;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 不可变索引快照。
 *
 * 快照构建后包含：文档集合、每词项倒排记录、每文档长度、平均文档长度。
 * 后续任何 upsert/delete/commit 都只会产生新的快照，本对象永不改变 ——
 * 旧游标（绑定 version）的每一页都在同一快照、同一排名上切片，
 * 因而索引更新不会影响翻页过程，既不会漏文档也不会重复。
 */
public final class IndexSnapshot {

    private final long version;
    private final long createdAt;

    private final Map<String, Document> documents;
    private final Map<String, Postings> postingsByTerm;
    private final Map<String, Integer> docLengths;

    private final int docCount;
    private final long totalLength;
    private final double avgDocLength;

    private IndexSnapshot(long version,
                          long createdAt,
                          Map<String, Document> documents,
                          Map<String, Postings> postingsByTerm,
                          Map<String, Integer> docLengths,
                          long totalLength) {
        this.version = version;
        this.createdAt = createdAt;
        this.documents = documents;
        this.postingsByTerm = postingsByTerm;
        this.docLengths = docLengths;
        this.docCount = documents.size();
        this.totalLength = totalLength;
        this.avgDocLength = totalLength == 0 ? 0.0d : (double) totalLength / this.docCount;
    }

    /** 基于一批文档构建快照。 */
    public static IndexSnapshot build(long version, long createdAt, Map<String, Document> docs) {
        Map<String, Document> immutableDocs = Map.copyOf(docs);

        Map<String, Map<String, Integer>> rawPostings = new HashMap<>();
        Map<String, Integer> lengths = new HashMap<>();
        long totalLen = 0;

        for (Document doc : immutableDocs.values()) {
            List<String> tokens = Tokenizer.tokenize(doc.content());
            lengths.put(doc.docId(), tokens.size());
            totalLen += tokens.size();

            // 同一文档内的词频
            Map<String, Integer> tfByTerm = new HashMap<>();
            for (String token : tokens) {
                tfByTerm.merge(token, 1, Integer::sum);
            }
            for (Map.Entry<String, Integer> e : tfByTerm.entrySet()) {
                rawPostings
                        .computeIfAbsent(e.getKey(), k -> new HashMap<>())
                        .put(doc.docId(), e.getValue());
            }
        }

        Map<String, Postings> postings = new HashMap<>();
        for (Map.Entry<String, Map<String, Integer>> e : rawPostings.entrySet()) {
            postings.put(e.getKey(), new Postings(e.getValue()));
        }

        return new IndexSnapshot(
                version,
                createdAt,
                immutableDocs,
                Map.copyOf(postings),
                Map.copyOf(lengths),
                totalLen);
    }

    public long version() {
        return version;
    }

    public long createdAt() {
        return createdAt;
    }

    public int docCount() {
        return docCount;
    }

    public long totalLength() {
        return totalLength;
    }

    public double avgDocLength() {
        return avgDocLength;
    }

    public Document document(String docId) {
        return documents.get(docId);
    }

    /** 当前快照内唯一词项数（供状态查看/测试）。 */
    public int termCount() {
        return postingsByTerm.size();
    }

    /**
     * 在本快照上对一组已分词的查询词项计算完整排名。
     *
     * 规则：
     * - 查询词项去重后分别打分求和；
     * - 总分严格大于 0 的文档进入排名（不含任何查询词项的文档自然被排除）；
     * - 排序：score 降序；score 相同时按 docId 的字典序（String#compareTo）升序，
     *   保证同分结果顺序稳定、确定。
     */
    public List<Hit> rank(List<String> queryTerms) {
        // 去重，保留首次出现顺序
        Set<String> uniqueTerms = new HashSet<>();
        Map<String, Postings> queryPostings = new LinkedHashMap<>();
        for (String term : queryTerms) {
            if (uniqueTerms.add(term)) {
                Postings p = postingsByTerm.get(term);
                if (p != null) {
                    queryPostings.put(term, p);
                }
            }
        }

        // 候选集合：至少包含一个查询词项的文档
        Set<String> candidates = new HashSet<>();
        for (Postings p : queryPostings.values()) {
            candidates.addAll(p.termFreqByDoc().keySet());
        }

        List<Hit> hits = new ArrayList<>(candidates.size());
        for (String docId : candidates) {
            int len = docLengths.get(docId);
            double score = Bm25.score(docId, len, avgDocLength, docCount, queryPostings);
            if (score > 0.0d) {
                hits.add(new Hit(docId, score, documents.get(docId)));
            }
        }

        hits.sort((a, b) -> {
            int cmp = Double.compare(b.score(), a.score());
            if (cmp != 0) {
                return cmp;
            }
            return a.docId().compareTo(b.docId());
        });
        return hits;
    }
}
