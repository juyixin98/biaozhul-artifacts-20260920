package phrase.search;

import phrase.core.Analyzer;
import phrase.core.Token;
import phrase.doc.Corpus;
import phrase.doc.Doc;
import phrase.index.InvertedIndex;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * 检索服务：为每个分析器各建一份倒排索引，查询时选取对应索引。
 * 语料为 {@link Corpus#synthetic()}，纯本地内存。
 */
public final class SearchService {

    private final Map<String, InvertedIndex> indexes;
    private final Map<String, Analyzer> analyzers;
    private final int maxMatchesPerDoc;

    public SearchService(int fieldGap, int maxMatchesPerDoc) {
        List<Doc> docs = Corpus.synthetic();
        this.analyzers = Map.of(
                "standard", phrase.core.Analyzers.standard(),
                "stopword", phrase.core.Analyzers.stopword());
        Map<String, InvertedIndex> idx = new LinkedHashMap<>();
        for (Map.Entry<String, Analyzer> e : analyzers.entrySet()) {
            idx.put(e.getKey(), InvertedIndex.build(docs, e.getValue(), fieldGap));
        }
        this.indexes = Map.copyOf(idx);
        this.maxMatchesPerDoc = maxMatchesPerDoc;
    }

    public Set<String> analyzerNames() {
        return analyzers.keySet();
    }

    /** 供 /analyze 端点使用。 */
    public List<Token> analyze(String analyzerName, String text) {
        Analyzer a = analyzers.get(analyzerName);
        if (a == null) {
            throw new IllegalArgumentException(
                    "unknown analyzer: " + analyzerName + " (supported: standard, stopword)");
        }
        return a.analyze(text == null ? "" : text);
    }

    public InvertedIndex index(String analyzerName) {
        InvertedIndex idx = indexes.get(analyzerName);
        if (idx == null) {
            throw new IllegalArgumentException(
                    "unknown analyzer: " + analyzerName + " (supported: standard, stopword)");
        }
        return idx;
    }

    /**
     * 从原始查询串构造词项：用指定分析器分析查询文本。
     * 若分析后没有词项（如停用词模式下查询仅含停用词），抛 IllegalArgumentException。
     */
    public List<String> termsFromQueryString(String analyzerName, String query) {
        if (query == null || query.isBlank()) {
            throw new IllegalArgumentException("query must not be empty");
        }
        List<Token> tokens = analyze(analyzerName, query);
        List<String> terms = new ArrayList<>();
        for (Token t : tokens) {
            terms.add(t.term());
        }
        if (terms.isEmpty()) {
            throw new IllegalArgumentException(
                    "query produced no terms after analysis (all terms are stopwords?)");
        }
        return terms;
    }

    /** 与 termsFromQueryString 相同的小写规则，用于校验直接给出的 terms 数组。 */
    public static String normalizeTerm(String raw) {
        if (raw == null) {
            throw new IllegalArgumentException("term must not be null");
        }
        String t = raw.trim().toLowerCase(Locale.ROOT);
        if (t.isEmpty()) {
            throw new IllegalArgumentException("term must not be empty");
        }
        if (!t.chars().allMatch(Character::isLetterOrDigit)) {
            throw new IllegalArgumentException(
                    "term '" + raw + "' must contain only letters/digits");
        }
        return t;
    }

    public SearchResult search(String analyzerName, String rawQuery, List<String> rawTerms,
                               int slop, String field) {
        InvertedIndex idx = index(analyzerName);

        List<String> terms;
        if (rawTerms != null && !rawTerms.isEmpty()) {
            terms = new ArrayList<>();
            for (String t : rawTerms) {
                terms.add(normalizeTerm(t));
            }
        } else {
            terms = termsFromQueryString(analyzerName, rawQuery);
        }

        if (field != null && !field.isBlank()) {
            boolean exists = idx.docIds().stream()
                    .map(idx::doc)
                    .anyMatch(d -> d.fields().containsKey(field));
            if (!exists) {
                throw new IllegalArgumentException("unknown field: " + field);
            }
        }
        String scope = (field == null || field.isBlank()) ? null : field;
        PhraseQuery pq = new PhraseQuery(terms, slop, scope);

        // AND 预过滤：候选文档必须包含每个词项
        Set<String> candidates = null;
        for (String t : terms) {
            Set<String> d = idx.docsContaining(t);
            if (candidates == null) {
                candidates = new HashSet<>(d);
            } else {
                candidates.retainAll(d);
            }
            if (candidates.isEmpty()) {
                return new SearchResult(analyzerName, slop, scope, List.copyOf(terms),
                        List.of(), 0, false);
            }
        }

        PhraseMatcher matcher = new PhraseMatcher(maxMatchesPerDoc + 1);
        List<DocMatch> hits = new ArrayList<>();
        boolean anyTruncated = false;
        int totalHits = 0;
        for (String docId : idx.docIds()) {
            if (!candidates.contains(docId)) {
                continue;
            }
            List<Match> ms = matcher.find(idx, docId, pq);
            if (ms.isEmpty()) {
                continue;
            }
            boolean trunc = ms.size() > maxMatchesPerDoc;
            if (trunc) {
                ms = ms.subList(0, maxMatchesPerDoc);
                anyTruncated = true;
            }
            totalHits += ms.size();
            hits.add(new DocMatch(docId, ms.size(), trunc, ms));        }
        hits.sort(Comparator.comparingInt(DocMatch::matchCount).reversed()
                .thenComparing(DocMatch::docId));
        return new SearchResult(analyzerName, slop, scope, List.copyOf(terms),
                List.copyOf(hits), totalHits, anyTruncated);
    }
}
