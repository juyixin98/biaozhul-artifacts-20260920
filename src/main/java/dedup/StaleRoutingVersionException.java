package dedup;

/**
 * Thrown when a request carries a routing version that does not match the
 * service's current routing version (e.g. a client using a stale partition
 * table after a migration).
 */
public class StaleRoutingVersionException extends RuntimeException {
    private final long expected;
    private final long actual;

    public StaleRoutingVersionException(long expected, long actual) {
        super("stale routing version: expected " + expected + ", got " + actual);
        this.expected = expected;
        this.actual = actual;
    }

    public long expected() { return expected; }
    public long actual() { return actual; }
}
