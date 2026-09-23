package com.example.sessionwindow.engine;

import com.example.sessionwindow.model.Aggregate;
import com.example.sessionwindow.model.ResultRecord;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Folds the explicit ADD / RETRACT / SEALED record stream into the materialized
 * table of current results, exactly as a downstream consumer would.
 *
 * <p>Results are keyed by {@code (key, windowStart, windowEnd)} so retractions
 * address the exact superseded window, including a reopened session whose
 * end moved.</p>
 */
public final class Results {

    private Results() {
    }

    public record WindowRef(String key, long start, long end) {
    }

    /** Materialized table after folding all records. */
    public static Map<WindowRef, Aggregate> fold(List<ResultRecord> records) {
        Map<WindowRef, Aggregate> table = new LinkedHashMap<>();
        for (ResultRecord r : records) {
            switch (r.type()) {
                case ADD, SEALED -> table.put(ref(r), r.aggregate());
                case RETRACT -> table.remove(ref(r));
                case PURGED -> {
                    // State is gone from the engine, but the final answer stays
                    // visible to consumers (removal happens upstream, not here).
                }
                case DROPPED, WATERMARK -> {
                    // informational
                }
            }
        }
        return table;
    }

    private static WindowRef ref(ResultRecord r) {
        return new WindowRef(r.key(), r.aggregate().start(), r.aggregate().end());
    }

    /** Per-key view, sessions sorted by start. */
    public static Map<String, TreeMap<Long, Aggregate>> byKey(List<ResultRecord> records) {
        Map<String, TreeMap<Long, Aggregate>> out = new LinkedHashMap<>();
        for (Map.Entry<WindowRef, Aggregate> e : fold(records).entrySet()) {
            out.computeIfAbsent(e.getKey().key(), k -> new TreeMap<>())
                    .put(e.getKey().start(), e.getValue());
        }
        return out;
    }
}
