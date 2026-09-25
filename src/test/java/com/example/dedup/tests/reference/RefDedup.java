package com.example.dedup.tests.reference;

import com.example.dedup.json.Json;
import com.example.dedup.model.Event;

import java.util.HashSet;
import java.util.Set;

/**
 * Exact small-data reference: unbounded set of every event id ever seen.
 * This is the ground truth for "duplicate or not" within a single run.
 */
public final class RefDedup {
    private final Set<String> seen = new HashSet<>();
    public long duplicates;

    /** First occurrence? */
    public boolean isFirst(String id) {
        return seen.add(id);
    }

    public int size() {
        return seen.size();
    }

    public static boolean payloadEqual(Json.Value a, Json.Value b) {
        if (a == null) {
            return b == null;
        }
        return Json.canonical(a).equals(Json.canonical(b));
    }

    public boolean isFirst(Event e) {
        return isFirst(e.id());
    }
}
