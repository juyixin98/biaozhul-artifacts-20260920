package invidx.search;

import invidx.model.DocKey;
import invidx.segment.Segment;

import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Evaluates {@link Query} objects against an immutable {@link Snapshot}.
 *
 * <p>When several segments contain different generations of the same id,
 * only the revision equal to the snapshot's latest live generation can
 * match — an older surviving posting never leaks through even before its
 * tombstoned generation is physically removed by a merge.
 */
public final class Searcher {

    private final Snapshot snapshot;

    public Searcher(Snapshot snapshot) {
        this.snapshot = snapshot;
    }

    public List<Hit> search(Query query) {
        return switch (query) {
            case Query.Term t -> sort(collect(t.term()).keySet());
            case Query.Boolean b -> b.op() == Query.Operator.AND ? and(b.terms()) : or(b.terms());
        };
    }

    public List<Hit> search(String spec) {
        return search(Query.parse(spec));
    }

    public int count(Query query) {
        return search(query).size();
    }

    /** id -&gt; live gen for documents whose current revision contains the term. */
    private Map<Integer, Long> collect(String term) {
        Map<Integer, Long> out = new HashMap<>();
        for (Segment seg : snapshot.segments()) {
            long[] p = seg.postings(term);
            if (p == null) {
                continue;
            }
            for (long enc : p) {
                int id = DocKey.idOf(enc);
                long gen = DocKey.genOf(enc);
                Snapshot.LiveDoc live = snapshot.latest().get(id);
                // Keep the posting only if it is exactly the current live revision.
                if (live != null && live.gen() == gen) {
                    out.merge(id, gen, Math::max);
                }
            }
        }
        return out;
    }

    private List<Hit> and(List<String> terms) {
        Map<Integer, Long> first = collect(terms.get(0));
        TreeMap<Integer, Long> result = new TreeMap<>(first);
        for (int i = 1; i < terms.size() && !result.isEmpty(); i++) {
            Map<Integer, Long> next = collect(terms.get(i));
            result.keySet().retainAll(next.keySet());
        }
        return sort(result.keySet());
    }

    private List<Hit> or(List<String> terms) {
        java.util.Set<Integer> ids = new java.util.HashSet<>();
        for (String term : terms) {
            ids.addAll(collect(term).keySet());
        }
        return sort(ids);
    }

    private List<Hit> sort(java.util.Set<Integer> ids) {
        return ids.stream().sorted().map(id -> {
            Snapshot.LiveDoc live = snapshot.latest().get(id);
            return new Hit(id, live.gen(), live.text());
        }).toList();
    }
}
