package phraseindex;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.locks.ReentrantReadWriteLock;

/**
 * Positional inverted index.
 *
 * Stored structures:
 * <ul>
 *   <li>{@code documents}: document id -> current raw text;</li>
 *   <li>{@code postings}: term -> (document id -> ascending token positions).</li>
 * </ul>
 *
 * Replacing or deleting a document removes all of its old positions from
 * every term posting, so no stale positions can survive an update.
 */
public final class InvertedIndex {

    private final Map<Integer, String> documents = new HashMap<>();
    final Map<String, Map<Integer, List<Integer>>> postings = new HashMap<>();
    private final ReentrantReadWriteLock lock = new ReentrantReadWriteLock();

    /** Inserts or replaces a document and (re)indexes every token position. */
    public void put(int docId, String text) {
        List<String> tokens = Tokenizer.tokenize(text);
        lock.writeLock().lock();
        try {
            // Rebuild: first drop every position of the previous version, then
            // add the new tokenization. Even if the new text is empty, no old
            // position is left behind.
            removeFromPostings(docId);
            documents.put(docId, text == null ? "" : text);
            for (int pos = 0; pos < tokens.size(); pos++) {
                postings.computeIfAbsent(tokens.get(pos), k -> new HashMap<>())
                        .computeIfAbsent(docId, k -> new ArrayList<>())
                        .add(pos);
            }
        } finally {
            lock.writeLock().unlock();
        }
    }

    /** Deletes a document. Returns false if the id was not present. */
    public boolean remove(int docId) {
        lock.writeLock().lock();
        try {
            if (documents.remove(docId) == null) {
                return false;
            }
            removeFromPostings(docId);
            return true;
        } finally {
            lock.writeLock().unlock();
        }
    }

    /** Caller must hold the write lock. */
    private void removeFromPostings(int docId) {
        var it = postings.entrySet().iterator();
        while (it.hasNext()) {
            Map<Integer, List<Integer>> byDoc = it.next().getValue();
            byDoc.remove(docId);
            // Drop the term key entirely when no document mentions it any
            // more; otherwise the empty entry would be stale index state.
            if (byDoc.isEmpty()) {
                it.remove();
            }
        }
    }

    public String get(int docId) {
        lock.readLock().lock();
        try {
            return documents.get(docId);
        } finally {
            lock.readLock().unlock();
        }
    }

    public boolean contains(int docId) {
        lock.readLock().lock();
        try {
            return documents.containsKey(docId);
        } finally {
            lock.readLock().unlock();
        }
    }

    public Set<Integer> allDocIds() {
        lock.readLock().lock();
        try {
            return new HashSet<>(documents.keySet());
        } finally {
            lock.readLock().unlock();
        }
    }

    public int size() {
        lock.readLock().lock();
        try {
            return documents.size();
        } finally {
            lock.readLock().unlock();
        }
    }

    /** All document ids matching a boolean / phrase query, unordered. */
    public Set<Integer> matchingDocIds(Query query) {
        lock.readLock().lock();
        try {
            return eval(query);
        } finally {
            lock.readLock().unlock();
        }
    }

    private Set<Integer> eval(Query q) {
        if (q instanceof Query.Phrase p) {
            return phraseDocs(p.terms());
        } else if (q instanceof Query.And a) {
            Set<Integer> l = eval(a.left());
            l.retainAll(eval(a.right()));
            return l;
        } else if (q instanceof Query.Or o) {
            Set<Integer> l = eval(o.left());
            l.addAll(eval(o.right()));
            return l;
        } else if (q instanceof Query.Not n) {
            Set<Integer> all = new HashSet<>(documents.keySet());
            all.removeAll(eval(n.child()));
            return all;
        }
        throw new IllegalStateException("unknown query type: " + q.getClass());
    }

    private Set<Integer> phraseDocs(List<String> terms) {
        Set<Integer> out = new HashSet<>();
        if (terms.isEmpty()) {
            return out;
        }
        Map<Integer, List<Integer>> firstDocs = postings.get(terms.get(0));
        if (firstDocs == null) {
            return out;
        }
        for (Map.Entry<Integer, List<Integer>> e : firstDocs.entrySet()) {
            if (positionsPresent(e.getKey(), terms, e.getValue())) {
                out.add(e.getKey());
            }
        }
        return out;
    }

    /** Token positions at which the full phrase starts in one document. */
    public List<Integer> phrasePositions(int docId, List<String> terms) {
        if (terms.isEmpty()) {
            return List.of();
        }
        lock.readLock().lock();
        try {
            Map<Integer, List<Integer>> firstDocs = postings.get(terms.get(0));
            if (firstDocs == null) {
                return List.of();
            }
            List<Integer> firstPositions = firstDocs.get(docId);
            if (firstPositions == null) {
                return List.of();
            }
            List<Integer> out = new ArrayList<>();
            if (positionsPresent(docId, terms, firstPositions)) {
                for (int start : firstPositions) {
                    if (phraseAt(docId, terms, start)) {
                        out.add(start);
                    }
                }
            }
            return out;
        } finally {
            lock.readLock().unlock();
        }
    }

    private boolean positionsPresent(int docId, List<String> terms, List<Integer> firstPositions) {
        for (int start : firstPositions) {
            if (phraseAt(docId, terms, start)) {
                return true;
            }
        }
        return false;
    }

    /** All posting lists per document are stored in ascending token order. */
    private boolean phraseAt(int docId, List<String> terms, int start) {
        for (int k = 1; k < terms.size(); k++) {
            Map<Integer, List<Integer>> byDoc = postings.get(terms.get(k));
            if (byDoc == null) {
                return false;
            }
            List<Integer> positions = byDoc.get(docId);
            if (positions == null
                    || Collections.binarySearch(positions, start + k) < 0) {
                return false;
            }
        }
        return true;
    }
}
