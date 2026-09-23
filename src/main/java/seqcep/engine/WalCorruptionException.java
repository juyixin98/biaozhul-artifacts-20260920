package seqcep.engine;

/**
 * Thrown when the write-ahead log is corrupt (bad magic, CRC mismatch or truncated record).
 *
 * <p>The engine deliberately has no "truncate and keep going" code path: silently dropping
 * records would change matching results. The operator must inspect/repair the log, or clear
 * the data directory explicitly (DELETE /v1/state) to start fresh.
 */
public class WalCorruptionException extends RuntimeException {
    public WalCorruptionException(String message) {
        super(message);
    }

    public WalCorruptionException(String message, Throwable cause) {
        super(message, cause);
    }
}
