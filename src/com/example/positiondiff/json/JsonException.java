package com.example.positiondiff.json;

/** Thrown when JSON input cannot be parsed. */
public class JsonException extends RuntimeException {
    public JsonException(String message) {
        super(message);
    }
}
