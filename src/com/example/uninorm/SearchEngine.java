package com.example.uninorm;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * In-memory substring search over the normalized corpus. Every hit found in
 * normalized space is mapped back to the full original span via
 * {@link NormalizedText#mapRange(int, int)}.
 *
 * <p>Matching is plain {@link String#indexOf} on the normalized keys - no
 * external search service, no model calls. Overlapping normalized matches are
 * enumerated; hits that map to the same original span are de-duplicated so
 * each original range is reported once.
 */
public final class SearchEngine {

    private record IndexedDoc(Document doc, NormalizedText norm) {
    }

    private final List<IndexedDoc> docs = new ArrayList<>();

    public void addDocument(Document doc) {
        docs.add(new IndexedDoc(doc, TextNormalizer.normalize(doc.text())));
    }

    public int documentCount() {
        return docs.size();
    }

    /** Normalized key of a document, exposed for diagnostics and tests. */
    public String normalizedOf(String docId) {
        for (IndexedDoc d : docs) {
            if (d.doc().id().equals(docId)) {
                return d.norm().normalized();
            }
        }
        throw new IllegalArgumentException("unknown document: " + docId);
    }

    /**
     * Search all documents for {@code query}.
     *
     * @param query raw query text; normalized with the identical pipeline
     * @param limit maximum number of hits to return (values &lt;= 0 mean
     *              "no limit")
     * @return hits in document order, then by original start offset
     */
    public List<SearchHit> search(String query, int limit) {
        if (query == null) {
            throw new IllegalArgumentException("query must not be null");
        }
        List<SearchHit> hits = new ArrayList<>();
        if (query.isEmpty()) {
            return hits;
        }
        String foldedQuery = TextNormalizer.normalize(query).normalized();
        if (foldedQuery.isEmpty()) {
            return hits;
        }
        for (IndexedDoc d : docs) {
            String normText = d.norm().normalized();
            // Enumerate overlapping matches: advance by one char each time.
            Set<OffsetRange> seen = new LinkedHashSet<>();
            int from = 0;
            while (from <= normText.length() - foldedQuery.length()) {
                int idx = normText.indexOf(foldedQuery, from);
                if (idx < 0) {
                    break;
                }
                OffsetRange range = d.norm().mapRange(idx, idx + foldedQuery.length());
                if (seen.add(range)) {
                    hits.add(new SearchHit(d.doc().id(), range,
                            d.norm().excerpt(range)));
                    if (limit > 0 && hits.size() >= limit) {
                        return hits;
                    }
                }
                from = idx + 1;
            }
        }
        return hits;
    }
}
