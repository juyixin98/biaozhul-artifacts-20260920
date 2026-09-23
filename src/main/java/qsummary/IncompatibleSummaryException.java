package qsummary;

/**
 * Thrown when two summaries cannot be merged because their parameters
 * are incompatible (different epsilon, algorithm version or comparator).
 *
 * Merging incompatible summaries would silently weaken the rank-error
 * guarantee, so the service rejects the request instead.
 */
public class IncompatibleSummaryException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public IncompatibleSummaryException(String message) {
        super(message);
    }
}
