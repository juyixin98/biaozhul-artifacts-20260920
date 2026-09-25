package invidx.engine;

import invidx.analyzer.Tokenizer;
import invidx.model.Document;
import invidx.util.LongList;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;

/**
 * Mutable in-memory buffer of indexed-but-not-yet-flushed documents.
 * Holds at most one revision per id; replacing or deleting a revision
 * removes exactly its term membership, so no stale term survives.
 */
final class RamBuffer {

    private final Map<Integer, Document> docs = new HashMap<>();
    private final Map<Integer, Set<String>> docTerms = new HashMap<>();
    private final Map<String, LongList> postings = new HashMap<>();

    synchronized int size() {
        return docs.size();
    }

    synchronized boolean isEmpty() {
        return docs.isEmpty();
    }

    synchronized Document docById(int id) {
        return docs.get(id);
    }

    synchronized void add(Document doc) {
        removeById(doc.id());
        docs.put(doc.id(), doc);
        long key = doc.key().encoded();
        Set<String> terms = new LinkedHashSet<>(Tokenizer.tokenize(doc.text()));
        docTerms.put(doc.id(), terms);
        for (String term : terms) {
            postings.computeIfAbsent(term, t -> new LongList()).add(key);
        }
    }

    synchronized void removeById(int id) {
        Document old = docs.remove(id);
        if (old == null) {
            return;
        }
        long key = old.key().encoded();
        Set<String> terms = docTerms.remove(id);
        if (terms != null) {
            for (String term : terms) {
                LongList list = postings.get(term);
                if (list == null) {
                    continue;
                }
                long[] arr = list.getReferenceArray();
                int write = 0;
                for (int i = 0; i < list.size(); i++) {
                    if (arr[i] != key) {
                        arr[write++] = arr[i];
                    }
                }
                list.truncate(write);
                if (list.size() == 0) {
                    postings.remove(term);
                }
            }
        }
    }

    /** Remove and return all buffered documents in key order. */
    synchronized List<Document> drain() {
        List<Document> out = docs.values().stream()
                .sorted(java.util.Comparator.comparing(Document::key))
                .toList();
        docs.clear();
        docTerms.clear();
        postings.clear();
        return out;
    }

    synchronized Map<String, long[]> postingsSnapshot() {
        TreeMap<String, long[]> out = new TreeMap<>();
        for (Map.Entry<String, LongList> e : postings.entrySet()) {
            long[] p = e.getValue().toArray();
            java.util.Arrays.sort(p);
            out.put(e.getKey(), p);
        }
        return out;
    }

    synchronized Map<Integer, Document> docsSnapshot() {
        return Map.copyOf(docs);
    }
}
