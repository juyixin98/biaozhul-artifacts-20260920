package qsummary;

/** Thrown when a referenced summary does not exist (HTTP 404). */
public class NotFoundException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public NotFoundException(String message) {
        super(message);
    }
}
