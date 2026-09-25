package com.example.uninorm;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * In-memory local search engine. Documents are stored verbatim; search runs
 * as a normalized substring match over each document's normalized form and
 * every hit is mapped back to the original text's UTF-16 offsets.
 *
 * No external search service, no model calls — plain local string matching.
 */
public final class SearchEngine {

    public static final class Hit {
        public final String docId;
        public final int start;      // original UTF-16 offset, inclusive
        public final int end;        // original UTF-16 offset, exclusive
        public final String matched; // the original text of the hit

        Hit(String docId, int start, int end, String matched) {
            this.docId = docId;
            this.start = start;
            this.end = end;
            this.matched = matched;
        }
    }

    private static final class Doc {
        final String text;
        final NormalizedText norm;
        Doc(String text) {
            this.text = text;
            this.norm = TextNormalizer.normalize(text);
        }
    }

    private final Map<String, Doc> docs = new LinkedHashMap<>();

    public synchronized void addDocument(String id, String text) {
        docs.put(id, new Doc(text));
    }

    public synchronized boolean removeDocument(String id) {
        return docs.remove(id) != null;
    }

    public synchronized List<String> documentIds() {
        return new ArrayList<>(docs.keySet());
    }

    public synchronized int documentCount() {
        return docs.size();
    }

    /** Raw normalized form of a stored document (for debugging/tests), or null. */
    public synchronized String normalizedOf(String id) {
        Doc d = docs.get(id);
        return d == null ? null : d.norm.text;
    }

    public synchronized List<Hit> search(String query, int maxHits) {
        List<Hit> hits = new ArrayList<>();
        if (query == null || query.isEmpty()) {
            return hits;
        }
        NormalizedText q = TextNormalizer.normalize(query);
        if (q.text.isEmpty()) {
            return hits;
        }
        for (Map.Entry<String, Doc> entry : docs.entrySet()) {
            Doc doc = entry.getValue();
            int from = 0;
            while (true) {
                int idx = doc.norm.text.indexOf(q.text, from);
                if (idx < 0) {
                    break;
                }
                int normEnd = idx + q.text.length();
                int[] range = doc.norm.toOriginalRange(idx, normEnd);
                hits.add(new Hit(entry.getKey(), range[0], range[1],
                        doc.text.substring(range[0], range[1])));
                if (hits.size() >= maxHits) {
                    return hits;
                }
                from = idx + 1; // allow overlapping matches
            }
        }
        return hits;
    }
}
