package joinplanner.json;

/** Thrown when request JSON is well-formed but violates the service schema. Maps to HTTP 400. */
public class BadInputException extends RuntimeException {
    public BadInputException(String message) {
        super(message);
    }
}
