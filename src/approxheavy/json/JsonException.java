package approxheavy.json;

/** Thrown for malformed JSON or wrong field types. */
public class JsonException extends RuntimeException {
    public JsonException(String message) {
        super(message);
    }
}
