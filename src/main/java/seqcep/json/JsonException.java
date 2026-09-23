package seqcep.json;

/** Thrown for malformed JSON or type mismatches in parsed values. */
public class JsonException extends RuntimeException {
    public JsonException(String message) {
        super(message);
    }
}
