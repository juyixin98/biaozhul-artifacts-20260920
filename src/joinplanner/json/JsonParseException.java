package joinplanner.json;

/** Thrown when a JSON document is malformed. */
public class JsonParseException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public JsonParseException(String message) {
        super(message);
    }
}
