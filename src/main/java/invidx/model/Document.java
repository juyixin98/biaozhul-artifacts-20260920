package invidx.model;

/** One stored document revision: id + generation + original text. */
public record Document(int id, long gen, String text) {
    public DocKey key() {
        return new DocKey(id, gen);
    }
}
