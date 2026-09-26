package com.example.vic.store;

/** Thrown when two rules of equal priority overlap; the write is rejected. */
public class ConflictException extends RuntimeException {

    public ConflictException(String message) {
        super(message);
    }
}
