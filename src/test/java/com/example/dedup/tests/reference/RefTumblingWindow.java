package com.example.dedup.tests.reference;

import com.example.dedup.model.Event;
import com.example.dedup.model.WindowResult;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Exact reference tumbling windows: keeps all windows forever and fires
 * them on demand at the end of the run. Every window the bounded operator
 * emits before the test's "verification watermark" must match this exactly.
 */
public final class RefTumblingWindow {

    private static final class Acc {
        String lastUpsertId;
        com.example.dedup.json.Json.Value lastPayload;
        long upserts;
        long deletes;
        long lastUpsertTime = Long.MIN_VALUE;
        long lastDeleteTime = Long.MIN_VALUE;
    }

    private final long size;
    private final TreeMap<Long, Map<String, Acc>> windows = new TreeMap<>();

    public RefTumblingWindow(long size) {
        this.size = size;
    }

    public void add(Event e) {
        long start = Math.floorDiv(e.eventTime(), size) * size;
        Acc a = windows.computeIfAbsent(start, k -> new TreeMap<>())
                .computeIfAbsent(e.key(), k -> new Acc());
        switch (e.type()) {
            case UPSERT -> {
                a.upserts++;
                a.lastUpsertId = e.id();
                a.lastPayload = e.payload();
                a.lastUpsertTime = Math.max(a.lastUpsertTime, e.eventTime());
            }
            case DELETE -> {
                a.deletes++;
                a.lastDeleteTime = Math.max(a.lastDeleteTime, e.eventTime());
            }
        }
    }

    /** Exact results for windows with end <= horizon (i.e. guaranteed fired). */
    public List<WindowResult> resultsUpTo(long horizonExclusiveEnd) {
        List<WindowResult> out = new ArrayList<>();
        for (Map.Entry<Long, Map<String, Acc>> w : windows.entrySet()) {
            long end = w.getKey() + size;
            if (end <= horizonExclusiveEnd) {
                for (Map.Entry<String, Acc> ka : w.getValue().entrySet()) {
                    Acc a = ka.getValue();
                    out.add(new WindowResult(w.getKey(), end, ka.getKey(),
                            a.lastUpsertId, a.lastPayload, a.upserts, a.deletes,
                            a.deletes > 0 && a.lastDeleteTime >= a.lastUpsertTime));
                }
            }
        }
        return out;
    }
}
