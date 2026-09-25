package dev.timeprecision.json;

/** Raised when input is not well-formed JSON. */
public final class JsonParseException extends RuntimeException {

    public JsonParseException(String message) {
        super(message);
    }
}
