package joinplanner.core;

/** Raised for invalid client input; mapped to HTTP 400. */
public class BadRequestException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public BadRequestException(String message) {
        super(message);
    }
}
