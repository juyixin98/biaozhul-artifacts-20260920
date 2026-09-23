package hllengine.json;

/**
 * Thrown when JSON text cannot be parsed, or when a value is not of the
 * expected shape. Carries a line/column position when produced by the parser.
 */
public final class JsonException extends RuntimeException {

    public JsonException(String message) {
        super(message);
    }

    public JsonException(String message, Throwable cause) {
        super(message, cause);
    }
}
