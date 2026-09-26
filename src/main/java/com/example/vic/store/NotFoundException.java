package com.example.vic.store;

/** Thrown when a referenced version or rule does not exist. */
public class NotFoundException extends RuntimeException {

    public NotFoundException(String message) {
        super(message);
    }
}
