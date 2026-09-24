package com.example.segindex;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Authoritative index state, persisted as manifest.json and updated
 * atomically (write tmp file, then rename). Anything not referenced by the
 * manifest is garbage collected on open, so a crash can never publish a
 * half-written segment.
 */
public class Manifest {

    public int version = 1;
    public long nextSegmentSeq = 1;
    /** docId -> latest generation ever assigned (including pending deletes). */
    public Map<String, Long> generations = new LinkedHashMap<>();
    /** Delete markers applied at query and merge time. */
    public List<Tombstone> tombstones = new ArrayList<>();
    /** Live segment directory names, oldest first. */
    public List<String> segments = new ArrayList<>();

    public Manifest copy() {
        Manifest m = new Manifest();
        m.version = version;
        m.nextSegmentSeq = nextSegmentSeq;
        m.generations = new LinkedHashMap<>(generations);
        m.tombstones = new ArrayList<>(tombstones);
        m.segments = new ArrayList<>(segments);
        return m;
    }
}
