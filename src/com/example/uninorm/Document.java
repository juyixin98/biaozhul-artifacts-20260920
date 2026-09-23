package com.example.uninorm;

/** One corpus document: an id and its raw text. */
public record Document(String id, String text) {
    public Document {
        if (id == null || id.isEmpty()) {
            throw new IllegalArgumentException("document id must be non-empty");
        }
        if (text == null) {
            throw new IllegalArgumentException("document text must not be null");
        }
    }
}
