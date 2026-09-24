package com.example.segindex;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

/**
 * In-memory view of one immutable on-disk segment. Everything is loaded at
 * open time, so a reader is unaffected by later file deletions (e.g. after
 * a merge) — that is what makes snapshot queries trivially consistent.
 */
public final class SegmentReader {

    private final String name;
    private final Map<String, List<Posting>> postings;
    /** docKey (id + \u0001 + generation) -> original text. */
    private final Map<String, String> docs;

    private SegmentReader(String name, Map<String, List<Posting>> postings, Map<String, String> docs) {
        this.name = name;
        this.postings = postings;
        this.docs = docs;
    }

    public static SegmentReader open(Path indexDir, String name, ObjectMapper mapper) {
        Path segDir = indexDir.resolve(name);
        try {
            Map<String, List<Posting>> postings = mapper.readValue(
                    segDir.resolve(SegmentWriter.POSTINGS_FILE).toFile(),
                    new TypeReference<>() {
                    });
            Map<String, String> docs = mapper.readValue(
                    segDir.resolve(SegmentWriter.DOCS_FILE).toFile(),
                    new TypeReference<>() {
                    });
            return new SegmentReader(name, postings, docs);
        } catch (IOException e) {
            throw new UncheckedIOException("failed to open segment " + name, e);
        }
    }

    public String name() {
        return name;
    }

    public Map<String, List<Posting>> postings() {
        return postings;
    }

    public List<Posting> postingsFor(String term) {
        return postings.getOrDefault(term, List.of());
    }

    public String docText(String docId, long generation) {
        return docs.get(docKey(docId, generation));
    }

    public Map<String, String> docs() {
        return docs;
    }

    static String docKey(String docId, long generation) {
        return docId + "\u0001" + generation;
    }
}
