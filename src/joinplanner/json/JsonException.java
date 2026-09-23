package joinplanner.json;

/** Thrown on malformed JSON. Mapped to HTTP 400 by the server. */
public class JsonException extends RuntimeException {
    public JsonException(String message) {
        super(message);
    }
}
