package qsummary;

/** Thrown for malformed client input (bad JSON, bad numbers, bad path ids). */
public class BadRequestException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public BadRequestException(String message) {
        super(message);
    }
}
