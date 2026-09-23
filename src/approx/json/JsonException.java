package approx.json;

/** Thrown when JSON text cannot be parsed. */
public class JsonException extends IllegalArgumentException {
    public JsonException(String message) {
        super(message);
    }
}
