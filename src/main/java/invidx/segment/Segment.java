package invidx.segment;

import invidx.model.DocKey;

import java.util.Map;
import java.util.SortedMap;

/**
 * Immutable, fully published segment. All lookups are in-memory; the
 * on-disk format is the durable source this object was decoded from.
 *
 * <p>A segment contains at most one generation per document id.
 */
public final class Segment {

    public record Entry(long gen, String text) {
    }

    private final String name;
    private final boolean merged;
    private final Map<Integer, Entry> docs;
    private final SortedMap<String, long[]> postings;

    Segment(String name, boolean merged,
            Map<Integer, Entry> docs,
            SortedMap<String, long[]> postings) {
        this.name = name;
        this.merged = merged;
        this.docs = Map.copyOf(docs);
        this.postings = postings;
    }

    /** Package bridge for the engine's ephemeral RAM view (never on disk). */
    public static Segment inMemory(String name,
                                  Map<Integer, Entry> docs,
                                  SortedMap<String, long[]> postings) {
        return new Segment(name, false, docs, postings);
    }

    public String name() {
        return name;
    }

    public boolean merged() {
        return merged;
    }

    public int docCount() {
        return docs.size();
    }

    public Entry get(int id) {
        return docs.get(id);
    }

    public Map<Integer, Entry> docs() {
        return docs;
    }

    /** Sorted encoded {@link DocKey} postings for a term, or {@code null}. */
    public long[] postings(String term) {
        return postings.get(term);
    }

    public SortedMap<String, long[]> allPostings() {
        return postings;
    }
}
