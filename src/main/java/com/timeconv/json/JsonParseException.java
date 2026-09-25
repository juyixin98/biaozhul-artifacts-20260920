package com.timeconv.json;

/** Thrown when JSON text cannot be parsed. */
public class JsonParseException extends RuntimeException {
    public JsonParseException(String message) {
        super(message);
    }
}
