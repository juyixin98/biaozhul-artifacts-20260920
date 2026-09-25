package approxheavy.server;

/** 404-style error for the HTTP layer. */
public class NotFoundException extends RuntimeException {
    public NotFoundException(String message) {
        super(message);
    }
}
