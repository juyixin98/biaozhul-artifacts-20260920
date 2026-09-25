package com.example.dedup.dedup;

import com.example.dedup.json.Json;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Bounded exactly-once-ish dedup state keyed by event id.
 *
 * Promise horizon: an entry for an event first seen at event time t is
 * retained until the watermark passes t + retentionMillis. A duplicate
 * occurrence with event time d arriving while watermark &lt;= d + retentionMillis
 * is therefore guaranteed to be found and suppressed (assuming the capacity
 * bound was never hit). After that the state is released and a returning
 * occurrence is reported as {@code unverified}: it may be a true duplicate
 * or a genuinely new id that reused an old timestamp — the system can no
 * longer tell, and says so via an observable flag rather than pretending.
 *
 * The state is bounded in two ways:
 *  - time: entries older than watermark - retentionMillis are evicted;
 *  - capacity: at most maxEntries entries; on overflow the entry with the
 *    smallest max event time (ties: earliest insertion) is evicted, and the
 *    eviction is reported + counted so callers know the promise is narrowed.
 */
public final class DedupState {

    private final int maxEntries;
    private final long retentionMillis;
    private final LinkedHashMap<String, DedupEntry> entries = new LinkedHashMap<>();
    private int orderCounter;

    public DedupState(int maxEntries, long retentionMillis) {
        if (maxEntries < 1) {
            throw new IllegalArgumentException("maxEntries must be >= 1");
        }
        if (retentionMillis < 0) {
            throw new IllegalArgumentException("retentionMillis must be >= 0");
        }
        this.maxEntries = maxEntries;
        this.retentionMillis = retentionMillis;
    }

    public int size() {
        return entries.size();
    }

    public int maxEntries() {
        return maxEntries;
    }

    public long retentionMillis() {
        return retentionMillis;
    }

    /**
     * Process one occurrence.
     *
     * @param watermark current watermark <em>before</em> this event updates it
     */
    public synchronized DedupResult process(String id, long eventTime, String payloadHash, long watermark) {
        DedupEntry e = entries.get(id);
        if (e != null) {
            boolean mismatch = !eqHash(e.payloadHash, payloadHash);
            boolean skew = eventTime != e.firstEventTime;
            if (eventTime > e.maxEventTime) {
                e.maxEventTime = eventTime;
            }
            return new DedupResult(true, mismatch, skew, false, false, null);
        }

        // No live state. Past the promise horizon for this event time?
        boolean unverified = watermark != Long.MIN_VALUE
                && watermark - retentionMillis > eventTime;

        DedupEntry ne = new DedupEntry(id, eventTime, payloadHash, orderCounter++);
        entries.put(id, ne);

        boolean capacityEvicted = false;
        String evictedId = null;
        if (entries.size() > maxEntries) {
            evictedId = evictCapacity();
            capacityEvicted = evictedId != null;
        }
        return new DedupResult(false, false, false, unverified, capacityEvicted, evictedId);
    }

    /** Watermark-driven release. Returns number of entries released. */
    public synchronized int evictExpired(long watermark) {
        if (watermark == Long.MIN_VALUE) {
            return 0;
        }
        int removed = 0;
        Iterator<Map.Entry<String, DedupEntry>> it = entries.entrySet().iterator();
        while (it.hasNext()) {
            DedupEntry e = it.next().getValue();
            if (watermark - retentionMillis > e.maxEventTime) {
                it.remove();
                removed++;
            }
        }
        return removed;
    }

    private String evictCapacity() {
        String victim = null;
        DedupEntry victimEntry = null;
        for (Map.Entry<String, DedupEntry> en : entries.entrySet()) {
            DedupEntry e = en.getValue();
            if (victimEntry == null
                    || e.maxEventTime < victimEntry.maxEventTime
                    || (e.maxEventTime == victimEntry.maxEventTime && e.insertOrder < victimEntry.insertOrder)) {
                victim = en.getKey();
                victimEntry = e;
            }
        }
        if (victim != null) {
            entries.remove(victim);
        }
        return victim;
    }

    private static boolean eqHash(String a, String b) {
        return a == null ? b == null : a.equals(b);
    }

    // ----------------------------------------------------------------
    // Snapshot / restore (restart recovery)
    // ----------------------------------------------------------------

    public synchronized Json.Value snapshot() {
        Json.JsonArray arr = Json.arr();
        for (DedupEntry e : entries.values()) {
            Json.JsonObject o = Json.obj();
            o.members().put("id", Json.str(e.id));
            o.members().put("firstEventTime", Json.num(e.firstEventTime));
            o.members().put("maxEventTime", Json.num(e.maxEventTime));
            o.members().put("payloadHash", e.payloadHash == null ? Json.JsonNull.INSTANCE : Json.str(e.payloadHash));
            o.members().put("order", Json.num(e.insertOrder));
            arr.elements().add(o);
        }
        Json.JsonObject o = Json.obj();
        o.members().put("entries", arr);
        o.members().put("orderCounter", Json.num(orderCounter));
        return o;
    }

    public synchronized void restore(Json.JsonObject o) {
        entries.clear();
        orderCounter = (int) o.getLong("orderCounter", 0);
        Json.Value a = o.get("entries");
        if (a instanceof Json.JsonArray arr) {
            for (Json.Value v : arr.elements()) {
                if (v instanceof Json.JsonObject eo) {
                    String id = eo.getString("id", null);
                    long ft = eo.getLong("firstEventTime", 0);
                    long mt = eo.getLong("maxEventTime", ft);
                    String hash = eo.get("payloadHash") instanceof Json.JsonString s ? s.value() : null;
                    int order = (int) eo.getLong("order", 0);
                    DedupEntry e = new DedupEntry(id, ft, hash, order);
                    e.maxEventTime = mt;
                    entries.put(id, e);
                }
            }
        }
    }

    /** Test/debug view of retained ids, in insertion order. */
    public synchronized List<String> retainedIds() {
        return new ArrayList<>(entries.keySet());
    }
}
