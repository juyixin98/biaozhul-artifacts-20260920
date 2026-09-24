package com.example.diff.corpus;

/** One document in the local synthetic corpus. */
public final class Document {
    public final String id;
    public final String title;
    public final String body;

    public Document(String id, String title, String body) {
        this.id = id;
        this.title = title;
        this.body = body;
    }
}
