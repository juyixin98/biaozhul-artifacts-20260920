package seqcep.engine;

/**
 * One completed A&rarr;B&rarr;C match for a single entity.
 *
 * <p>All three timestamps must satisfy
 * {@code tsA <= tsB <= tsC} (ties broken by seq) and {@code tsC - tsA <= 10_000}.
 * Overlapping matches are allowed: the same A or B event may appear in multiple matches.
 */
public record Match(String entityId, long seqA, long seqB, long seqC,
                    long tsA, long tsB, long tsC) {

    /** Matches are ordered primarily by detection order (the C event), then by A, B. */
    public static int comparePresentation(Match x, Match y) {
        int c = Long.compare(x.seqC, y.seqC);
        if (c != 0) return c;
        c = Long.compare(x.seqA, y.seqA);
        if (c != 0) return c;
        return Long.compare(x.seqB, y.seqB);
    }
}
