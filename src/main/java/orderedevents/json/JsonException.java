package orderedevents.json;

/** Thrown when a request body is not valid JSON for this API. */
@SuppressWarnings("serial")
public class JsonException extends RuntimeException {
    public JsonException(String message) {
        super(message);
    }
}
