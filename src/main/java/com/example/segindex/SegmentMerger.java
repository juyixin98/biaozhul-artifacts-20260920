package com.example.segindex;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Merges a set of segments into one new segment, physically dropping
 * tombstoned postings. The new segment is published by a single atomic
 * manifest swap; a crash before that swap leaves the old segments untouched
 * and the new (orphan) segment is cleaned up on the next open.
 */
final class SegmentMerger {

    private final Index index;

    SegmentMerger(Index index) {
        this.index = index;
    }

    /** Merges {@code toMerge} (must all be live segments) into a single segment. */
    void merge(List<String> toMerge) {
        if (toMerge.size() < 2) {
            return;
        }
        Manifest snapshot = index.manifestSnapshot();
        List<Tombstone> tombstones = snapshot.tombstones;
        IndexReader filter = new IndexReader(List.of(), tombstones);

        Map<String, List<Posting>> mergedPostings = new TreeMap<>();
        Map<String, String> mergedDocs = new LinkedHashMap<>();
        for (String name : toMerge) {
            SegmentReader reader = index.openSegment(name);
            collectLive(reader, filter, mergedPostings, mergedDocs);
        }

        String newName = index.allocateSegmentName();
        SegmentWriter.write(index.directory(), newName, mergedPostings, mergedDocs,
                index.mapper(), index.config().crashHook());
        index.publishMerge(toMerge, newName);
    }

    private void collectLive(SegmentReader reader, IndexReader filter,
                             Map<String, List<Posting>> postingsOut,
                             Map<String, String> docsOut) {
        for (Map.Entry<String, List<Posting>> entry : reader.postings().entrySet()) {
            String term = entry.getKey();
            for (Posting p : entry.getValue()) {
                if (filter.isDeleted(p.docId(), p.generation())) {
                    continue;
                }
                postingsOut.computeIfAbsent(term, k -> new ArrayList<>()).add(p);
                docsOut.putIfAbsent(SegmentReader.docKey(p.docId(), p.generation()),
                        reader.docText(p.docId(), p.generation()));
            }
        }
    }

    /** Builds term -> postings map from buffered (not yet committed) documents. */
    static Map<String, List<Posting>> buildPostings(List<IndexedDoc> docs) {
        Map<String, List<Posting>> postings = new TreeMap<>();
        for (IndexedDoc doc : docs) {
            Map<String, Integer> freqs = termFreqs(doc.text());
            freqs.forEach((term, freq) -> postings
                    .computeIfAbsent(term, k -> new ArrayList<>())
                    .add(new Posting(doc.id(), doc.generation(), freq)));
        }
        return postings;
    }

    static Map<String, String> buildDocStore(List<IndexedDoc> docs) {
        Map<String, String> store = new LinkedHashMap<>();
        for (IndexedDoc doc : docs) {
            store.put(SegmentReader.docKey(doc.id(), doc.generation()), doc.text());
        }
        return store;
    }

    private static Map<String, Integer> termFreqs(String text) {
        Map<String, Integer> freqs = new HashMap<>();
        for (String term : Tokenizer.tokenize(text)) {
            freqs.merge(term, 1, Integer::sum);
        }
        return freqs;
    }

    /** A document buffered by the writer before commit. */
    record IndexedDoc(String id, long generation, String text) {
    }
}
