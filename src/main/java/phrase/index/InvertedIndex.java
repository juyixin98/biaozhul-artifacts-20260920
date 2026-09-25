package phrase.index;

import phrase.core.Analyzer;
import phrase.core.Token;
import phrase.doc.Doc;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 内存倒排索引（纯本地，无外部服务）。
 *
 * <p>每个文档按字段顺序把所有字段分析后拼接为一条全局词项序列：
 * 字段内位置为分析器给出的 1 基位置；跨到下一个字段时额外加 {@code fieldGap}
 * 个虚拟位置（{@code fieldGap=0} 表示字段首尾紧邻，{@code fieldGap=1} 表示
 * 字段之间有 1 个可被 slop 跳过的间隔位）。
 *
 * <p>索引同时保留每个全局位置的出现信息（字段名），用于把命中还原为实际位置。
 */
public final class InvertedIndex {

    private final Analyzer analyzer;
    private final int fieldGap;
    private final Map<String, Doc> docs = new HashMap<>();
    private final List<String> docOrder = new ArrayList<>();

    /** term -> docId -> 该词在该文档中的出现列表（按全局位置升序） */
    private final Map<String, Map<String, List<Occ>>> postings = new HashMap<>();

    /** docId -> 全局位置 -> Occ（稀疏，命中位置反查字段用） */
    private final Map<String, Map<Integer, Occ>> occAt = new HashMap<>();

    /** docId -> 字段名 -> 该字段在全局序列中的起始偏移 */
    private final Map<String, Map<String, Integer>> fieldOffsets = new HashMap<>();

    public InvertedIndex(Analyzer analyzer, int fieldGap) {
        if (fieldGap < 0) {
            throw new IllegalArgumentException("fieldGap must be >= 0");
        }
        this.analyzer = analyzer;
        this.fieldGap = fieldGap;
    }

    public Analyzer analyzer() {
        return analyzer;
    }

    public int fieldGap() {
        return fieldGap;
    }

    public int docCount() {
        return docs.size();
    }

    public List<String> docIds() {
        return Collections.unmodifiableList(docOrder);
    }

    public Doc doc(String docId) {
        return docs.get(docId);
    }

    public List<Occ> occurrences(String term, String docId) {
        Map<String, List<Occ>> byDoc = postings.get(term);
        if (byDoc == null) {
            return List.of();
        }
        return byDoc.getOrDefault(docId, List.of());
    }

    /** 某文档中出现过指定词项的集合（供调用方做 AND 预过滤）。 */
    public java.util.Set<String> docsContaining(String term) {
        Map<String, List<Occ>> byDoc = postings.get(term);
        return byDoc == null ? Set.of() : Collections.unmodifiableSet(byDoc.keySet());
    }

    public Occ occAt(String docId, int globalPosition) {
        Map<Integer, Occ> map = occAt.get(docId);
        return map == null ? null : map.get(globalPosition);
    }

    /** 把某字段内分析器给出的局部位置换算为全局位置。 */
    public int toGlobalPosition(String docId, String field, int localPosition) {
        Integer offset = fieldOffsets.get(docId).get(field);
        return offset + localPosition;
    }

    public void add(Doc doc) {
        if (docs.containsKey(doc.id())) {
            throw new IllegalArgumentException("duplicate doc id: " + doc.id());
        }
        docs.put(doc.id(), doc);
        docOrder.add(doc.id());
        occAt.put(doc.id(), new HashMap<>());
        fieldOffsets.put(doc.id(), new HashMap<>());

        int offset = 0;
        boolean firstField = true;
        for (Map.Entry<String, String> e : doc.fields().entrySet()) {
            String fieldName = e.getKey();
            if (!firstField) {
                offset += fieldGap;
            }
            fieldOffsets.get(doc.id()).put(fieldName, offset);
            List<Token> tokens = analyzer.analyze(e.getValue());
            for (Token t : tokens) {
                int global = offset + t.position();
                Occ occ = new Occ(global, fieldName);
                postings.computeIfAbsent(t.term(), k -> new HashMap<>())
                        .computeIfAbsent(doc.id(), k -> new ArrayList<>())
                        .add(occ);
                occAt.get(doc.id()).put(global, occ);
            }
            if (!tokens.isEmpty()) {
                offset += tokens.get(tokens.size() - 1).position();
            }
            firstField = false;
        }
    }

    public static InvertedIndex build(Iterable<Doc> docs, Analyzer analyzer, int fieldGap) {
        InvertedIndex idx = new InvertedIndex(analyzer, fieldGap);
        for (Doc d : docs) {
            idx.add(d);
        }
        return idx;
    }
}
