package invidx.model;

/**
 * A document identity is the pair (numeric id, generation).
 * The same numeric id may be reused by updates; every reuse gets a
 * strictly larger generation, so postings of different revisions never
 * alias each other.
 */
public record DocKey(int id, long gen) implements Comparable<DocKey> {

    /** Packed representation used inside postings arrays. */
    public long encoded() {
        return encode(id, gen);
    }

    public static long encode(int id, long gen) {
        return ((long) id << 32) | (gen & 0xFFFFFFFFL);
    }

    public static int idOf(long encoded) {
        return (int) (encoded >>> 32);
    }

    public static long genOf(long encoded) {
        return encoded & 0xFFFFFFFFL;
    }

    public static DocKey of(long encoded) {
        return new DocKey(idOf(encoded), genOf(encoded));
    }

    @Override
    public int compareTo(DocKey o) {
        int c = Integer.compare(id, o.id);
        return c != 0 ? c : Long.compare(gen, o.gen);
    }
}
