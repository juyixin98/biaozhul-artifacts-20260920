package hlc;

/**
 * Raised when the 64-bit logical counter cannot be incremented.
 *
 * <p>The counter is stored as a non-negative signed {@code long}, so its practical maximum
 * is {@code 2^63 - 1}. Saturation or silent wraparound would manufacture timestamps that
 * violate HLC ordering, so exhaustion is a hard, explicit error. Advancing physical time
 * past {@code l} resets the counter to 0 and clears the condition.
 */
public final class LogicalCounterOverflowException extends HLCException {

    public static final long MAX_COUNTER = Long.MAX_VALUE;

    public LogicalCounterOverflowException(String message) {
        super(message);
    }
}
