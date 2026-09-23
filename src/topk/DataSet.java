package topk;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * In-memory dataset. Rows are appended with a monotonically increasing,
 * globally unique sequence number; that number is the stable tie-breaker
 * used everywhere in ranking.
 */
public final class DataSet {

    private final List<Row> rows = new ArrayList<>();
    private long nextSeq = 0;

    public synchronized Row add(String group, long value) {
        Row r = new Row(group, value, nextSeq++);
        rows.add(r);
        return r;
    }

    public synchronized int size() {
        return rows.size();
    }

    /** Remove all rows and restart the sequence numbering from 0. */
    public synchronized void reset() {
        rows.clear();
        nextSeq = 0;
    }

    /** Defensive copy in load order (seq order). */
    public synchronized List<Row> rows() {
        return new ArrayList<>(rows);
    }

    /**
     * Split rows into {@code shards} shards by round-robin over load order.
     * Deterministic for a fixed dataset and shard count.
     */
    public List<List<Row>> shard(int shards) {
        if (shards < 1) {
            throw new IllegalArgumentException("shards must be >= 1, got " + shards);
        }
        List<Row> snapshot = rows();
        List<List<Row>> out = new ArrayList<>(shards);
        for (int i = 0; i < shards; i++) out.add(new ArrayList<>());
        for (int i = 0; i < snapshot.size(); i++) {
            out.get(i % shards).add(snapshot.get(i));
        }
        return out;
    }

    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("rowCount", size());
        List<Object> arr = new ArrayList<>();
        for (Row r : rows()) arr.add(r.toJson());
        m.put("rows", arr);
        return m;
    }
}
