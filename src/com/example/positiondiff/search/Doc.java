package com.example.positiondiff.search;

import java.util.List;

/** One synthetic corpus document. {@code path} is a synthetic local identifier. */
public final class Doc {
    private final String id;
    private final String path;
    private final String title;
    private final String body;

    public Doc(String id, String path, String title, String body) {
        this.id = id;
        this.path = path;
        this.title = title;
        this.body = body;
    }

    public String id() { return id; }
    public String path() { return path; }
    public String title() { return title; }
    public String body() { return body; }

    public List<String> lines() {
        return body.lines().toList();
    }
}
