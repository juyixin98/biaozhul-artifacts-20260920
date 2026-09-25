package invidx.search;

import invidx.model.DocKey;

/** One query hit: the live document revision that matched. */
public record Hit(int id, long gen, String text) implements Comparable<Hit> {

    public DocKey key() {
        return new DocKey(id, gen);
    }

    @Override
    public int compareTo(Hit o) {
        int c = Integer.compare(id, o.id);
        return c != 0 ? c : Long.compare(gen, o.gen);
    }
}
