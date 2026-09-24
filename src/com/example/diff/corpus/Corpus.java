package com.example.diff.corpus;

import java.util.ArrayList;
import java.util.List;

/**
 * Self-built synthetic corpus. Everything is generated in code from fixed
 * strings &mdash; no external search service, no network, no model calls.
 *
 * <p>The documents deliberately include repeated lines, CRLF endings and
 * trailing-newline variants so the corpus doubles as diff test material.
 */
public final class Corpus {

    private Corpus() {
    }

    public static List<Document> synthetic() {
        List<Document> docs = new ArrayList<>();
        docs.add(new Document("log-001", "build log alpha",
                "STEP start build\n"
              + "STEP compile module core\n"
              + "INFO  compilation ok\n"
              + "STEP compile module web\n"
              + "INFO  compilation ok\n"
              + "STEP link\n"
              + "WARN  cache miss for asset app.css\n"
              + "INFO  link ok\n"
              + "STEP build done\n"));
        docs.add(new Document("log-002", "build log beta",
                "STEP start build\r\n"
              + "STEP compile module core\r\n"
              + "INFO  compilation ok\r\n"
              + "STEP compile module worker\r\n"
              + "ERROR compile failed in worker\r\n"
              + "STEP build failed\r\n"));
        docs.add(new Document("cfg-001", "service config",
                "[server]\n"
              + "host = 0.0.0.0\n"
              + "port = 8080\n"
              + "workers = 4\n"
              + "\n"
              + "[diff]\n"
              + "max_edit_distance = 64\n"
              + "context = 3\n"));
        docs.add(new Document("cfg-002", "service config revised",
                "[server]\n"
              + "host = 127.0.0.1\n"
              + "port = 8080\n"
              + "workers = 8\n"
              + "\n"
              + "[diff]\n"
              + "max_edit_distance = 128\n"
              + "context = 5\n"));
        docs.add(new Document("notes-001", "release notes",
                "Release 1.0\n"
              + "- add line level myers diff\n"
              + "- add budget based fallback\n"
              + "- keep newline form as content\n"
              + "- add http json service\n"
              + "known issues:\n"
              + "- none\n"));
        docs.add(new Document("notes-002", "release notes draft",
                "Release 1.1 draft\n"
              + "- add line level myers diff\n"
              + "- add budget based fallback\n"
              + "- keep newline form as content, CRLF included\n"
              + "- add http json service\n"
              + "- add local corpus search\n"
              + "known issues:\n"
              + "- myers on very large similar inputs is slow\n"));
        docs.add(new Document("dup-001", "repeated lines sample",
                "same\n"
              + "same\n"
              + "same\n"
              + "different\n"
              + "same\n"
              + "same\n"));
        docs.add(new Document("empty-001", "empty marker", ""));
        docs.add(new Document("one-001", "single unterminated line", "only line no newline"));
        return docs;
    }
}
