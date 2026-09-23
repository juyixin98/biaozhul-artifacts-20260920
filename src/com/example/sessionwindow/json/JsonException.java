package com.example.sessionwindow.json;

/** Thrown when input is not valid JSON or does not match the expected shape. */
public class JsonException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public JsonException(String message) {
        super(message);
    }
}
