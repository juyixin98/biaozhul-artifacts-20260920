package invidx.reference;

import invidx.analyzer.Tokenizer;
import invidx.search.Query;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Reference full-scan implementation used as an oracle in tests.
 *
 * <p>It keeps the current live text of every document id in memory and
 * answers each query by scanning all documents. The segmented index must
 * produce exactly the same hits (id + generation) for every interleaving
 * of adds, updates and deletes.
 */
public final class FullScan {

    private final Map<Integer, Revision> docs = new HashMap<>();

    private record Revision(long gen, String text) {
    }

    public synchronized void put(int id, long gen, String text) {
        docs.put(id, new Revision(gen, text));
    }

    public synchronized void delete(int id) {
        docs.remove(id);
    }

    public synchronized int count() {
        return docs.size();
    }

    public synchronized List<int[]> keys() {
        return docs.entrySet().stream()
                .map(e -> new int[]{e.getKey(), (int) e.getValue().gen()})
                .toList();
    }

    /** Sorted hits of {@code (id, gen)} matching the query. */
    public synchronized List<long[]> search(Query query) {
        List<long[]> out = new ArrayList<>();
        for (Map.Entry<Integer, Revision> e : docs.entrySet()) {
            Set<String> tokens = new HashSet<>(Tokenizer.tokenize(e.getValue().text()));
            if (matches(query, tokens)) {
                out.add(new long[]{e.getKey(), e.getValue().gen()});
            }
        }
        out.sort((a, b) -> a[0] != b[0] ? Long.compare(a[0], b[0]) : Long.compare(a[1], b[1]));
        return out;
    }

    private static boolean matches(Query query, Set<String> tokens) {
        return switch (query) {
            case Query.Term t -> tokens.contains(t.term());
            case Query.Boolean b -> b.op() == Query.Operator.AND
                    ? b.terms().stream().allMatch(tokens::contains)
                    : b.terms().stream().anyMatch(tokens::contains);
        };
    }
}
