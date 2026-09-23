package com.example.sessionwindow;

import java.util.Map;

/**
 * An aggregated session for one key. Adjacent events whose gap is <= gapMillis
 * belong to the same session; sessions are merged when a late event bridges them.
 *
 * The session id is stable: when two sessions merge, the surviving session keeps
 * the smaller local id, so recovery (which replays events in the same order)
 * yields identical ids.
 */
public final class Session {

    private final int localId;
    private long startTs;
    private long endTs;
    private int count;
    private int version;

    public Session(int localId, long startTs, long endTs, int count, int version) {
        this.localId = localId;
        this.startTs = startTs;
        this.endTs = endTs;
        this.count = count;
        this.version = version;
    }

    public int localId() {
        return localId;
    }

    public long startTs() {
        return startTs;
    }

    public long endTs() {
        return endTs;
    }

    public int count() {
        return count;
    }

    public int version() {
        return version;
    }

    public void addEvent(long ts) {
        if (ts < startTs) {
            startTs = ts;
        }
        if (ts > endTs) {
            endTs = ts;
        }
        count++;
        version++;
    }

    /**
     * Merge the other session into this one; the other is afterwards discarded.
     * Version arithmetic: each input session carries its own version count; the
     * merged snapshot is one further edit, e.g. bridging v1 and v1 yields v3.
     */
    public void merge(Session other, int extraEvents) {
        startTs = Math.min(startTs, other.startTs);
        endTs = Math.max(endTs, other.endTs);
        count += other.count + extraEvents;
        version++;
    }

    public Map<String, Object> toMap(String key) {
        var m = new java.util.LinkedHashMap<String, Object>();
        m.put("sessionId", key + "#" + localId);
        m.put("key", key);
        m.put("version", version);
        m.put("startTs", startTs);
        m.put("endTs", endTs);
        m.put("count", count);
        return m;
    }
}
