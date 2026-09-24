package com.example.segindex;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Immutable point-in-time view of the index. Captures the segment list and
 * tombstones at open time; commits and merges that happen afterwards are
 * invisible to this reader. Create a new reader to see newer state.
 */
public final class IndexReader {

    /** One search hit. */
    public record Hit(String docId, long generation, String text, int freq) {
    }

    private final List<SegmentReader> segments;
    /** docId -> list of tombstone max-generations. */
    private final Map<String, List<Long>> tombstones;

    IndexReader(List<SegmentReader> segments, List<Tombstone> tombstones) {
        this.segments = List.copyOf(segments);
        this.tombstones = new HashMap<>();
        for (Tombstone t : tombstones) {
            this.tombstones.computeIfAbsent(t.docId(), k -> new ArrayList<>()).add(t.maxGeneration());
        }
    }

    public int segmentCount() {
        return segments.size();
    }

    public boolean isDeleted(String docId, long generation) {
        List<Long> marks = tombstones.get(docId);
        if (marks == null) {
            return false;
        }
        for (long maxGen : marks) {
            if (generation <= maxGen) {
                return true;
            }
        }
        return false;
    }

    /**
     * Returns the newest live generation of each document containing
     * {@code term}, sorted by docId, capped at {@code limit}.
     */
    public List<Hit> search(String queryTerm, int limit) {
        String term = Tokenizer.normalizeTerm(queryTerm);
        Map<String, Posting> bestByDocId = new LinkedHashMap<>();
        for (SegmentReader segment : segments) {
            for (Posting p : segment.postingsFor(term)) {
                if (isDeleted(p.docId(), p.generation())) {
                    continue;
                }
                bestByDocId.merge(p.docId(), p,
                        (a, b) -> a.generation() >= b.generation() ? a : b);
            }
        }
        List<Hit> hits = new ArrayList<>();
        for (Posting p : bestByDocId.values()) {
            String text = findText(p);
            hits.add(new Hit(p.docId(), p.generation(), text, p.freq()));
        }
        hits.sort(Comparator.comparing(Hit::docId));
        return hits.size() > limit ? hits.subList(0, limit) : hits;
    }

    private String findText(Posting p) {
        for (SegmentReader segment : segments) {
            String text = segment.docText(p.docId(), p.generation());
            if (text != null) {
                return text;
            }
        }
        return null;
    }
}
