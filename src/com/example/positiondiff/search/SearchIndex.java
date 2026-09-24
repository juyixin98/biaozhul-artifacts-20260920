package com.example.positiondiff.search;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * In-memory local full-text index over the synthetic corpus. Scoring is a
 * small tf-idf variant: each query term contributes
 * {@code tf * idf * fieldBoost}, and the best matching line per document is
 * reported with its one-based line number. No external search service is
 * involved at any point.
 */
public final class SearchIndex {

    private static final double TITLE_BOOST = 2.0;

    private final List<Doc> docs;
    private final Map<String, Map<Integer, Integer>> postings = new HashMap<>();
    private final int docCount;

    public SearchIndex(List<Doc> docs) {
        this.docs = new ArrayList<>(docs);
        this.docCount = this.docs.size();
        build();
    }

    public static SearchIndex synthetic() {
        return new SearchIndex(Corpus.docs());
    }

    private void build() {
        for (int docId = 0; docId < docs.size(); docId++) {
            Doc doc = docs.get(docId);
            addTerms(docId, Tokenizer.tokenize(doc.body()), 1.0);
            addTerms(docId, Tokenizer.tokenize(doc.title()), TITLE_BOOST);
        }
    }

    private void addTerms(int docId, List<String> terms, double boost) {
        // boost folded into a single integer weight for title terms
        int weight = boost >= TITLE_BOOST ? 2 : 1;
        for (String term : terms) {
            postings.computeIfAbsent(term, k -> new HashMap<>())
                    .merge(docId, weight, Integer::sum);
        }
    }

    public static final class Hit {
        private final String docId;
        private final String path;
        private final String title;
        private final double score;
        private final int matchedTerms;
        private final int totalTerms;
        private final int lineNumber;
        private final String line;

        Hit(String docId, String path, String title, double score,
            int matchedTerms, int totalTerms, int lineNumber, String line) {
            this.docId = docId;
            this.path = path;
            this.title = title;
            this.score = score;
            this.matchedTerms = matchedTerms;
            this.totalTerms = totalTerms;
            this.lineNumber = lineNumber;
            this.line = line;
        }

        public String docId() { return docId; }
        public String path() { return path; }
        public String title() { return title; }
        public double score() { return score; }
        public int matchedTerms() { return matchedTerms; }
        public int totalTerms() { return totalTerms; }
        public int lineNumber() { return lineNumber; }
        public String line() { return line; }
    }

    public static final class Response {
        private final String query;
        private final List<String> queryTerms;
        private final List<Hit> hits;

        Response(String query, List<String> queryTerms, List<Hit> hits) {
            this.query = query;
            this.queryTerms = queryTerms;
            this.hits = hits;
        }

        public String query() { return query; }
        public List<String> queryTerms() { return queryTerms; }
        public List<Hit> hits() { return hits; }
    }

    public Response search(String query, int limit) {
        List<String> terms = Tokenizer.tokenize(query);
        Map<Integer, Double> scores = new HashMap<>();
        Map<Integer, Integer> matched = new HashMap<>();

        for (String term : terms) {
            Map<Integer, Integer> dfMap = postings.get(term);
            if (dfMap == null) continue;
            double idf = Math.log(1.0 + (double) docCount / dfMap.size());
            for (Map.Entry<Integer, Integer> e : dfMap.entrySet()) {
                int docId = e.getKey();
                double tf = 1.0 + Math.log(e.getValue());
                scores.merge(docId, tf * idf, Double::sum);
                matched.merge(docId, 1, Integer::sum);
            }
        }

        List<Hit> hits = new ArrayList<>();
        for (Map.Entry<Integer, Double> e : scores.entrySet()) {
            int docId = e.getKey();
            Doc doc = docs.get(docId);
            int[] bestLine = bestLine(doc, terms);
            hits.add(new Hit(doc.id(), doc.path(), doc.title(), round(e.getValue()),
                    matched.get(docId), terms.size(), bestLine[0], bestLine[1] == -1 ? "" : doc.lines().get(bestLine[1])));
        }
        hits.sort(Comparator.comparingDouble(Hit::score).reversed()
                .thenComparing(Hit::docId));
        if (limit > 0 && hits.size() > limit) {
            hits = hits.subList(0, limit);
        }
        return new Response(query, terms, hits);
    }

    /** Returns [lineNumber, lineIndex] (1-based / 0-based) of the line with most query terms. */
    private int[] bestLine(Doc doc, List<String> terms) {
        int bestIndex = -1;
        int bestCount = 0;
        List<String> lines = doc.lines();
        for (int i = 0; i < lines.size(); i++) {
            List<String> lineTokens = Tokenizer.tokenize(lines.get(i));
            int count = 0;
            for (String term : terms) {
                if (lineTokens.contains(term)) count++;
            }
            if (count > bestCount) {
                bestCount = count;
                bestIndex = i;
            }
        }
        return new int[]{bestIndex + 1, bestIndex};
    }

    private static double round(double v) {
        return Math.round(v * 1000.0) / 1000.0;
    }
}
