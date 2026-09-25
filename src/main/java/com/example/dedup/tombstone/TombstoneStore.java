package com.example.dedup.tombstone;

import com.example.dedup.json.Json;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

/**
 * Bounded keyed tombstone (DELETE) registry with event-time semantics.
 *
 * For every business key, the set of DELETE event times is retained while
 * watermark &lt;= deleteTime + ttlMillis. An UPSERT at event time t is
 * suppressed iff the key has a retained tombstone with deleteTime &gt;= t
 * (the delete happened at or after the upsert in event time). Tombstones
 * with deleteTime &lt; t never suppress, matching out-of-order arrival.
 *
 * After TTL expiry a tombstone is released; a very late UPSERT then meets
 * {@link #uncertain}: there might once have been a covering tombstone, so
 * the service reports uncertainty instead of claiming the upsert is live.
 *
 * Capacity is bounded per distinct key (oldest key by latest delete time is
 * evicted); such eviction only widens the uncertain window, never creates
 * a false suppression.
 */
public final class TombstoneStore {

    private final int maxKeys;
    private final long ttlMillis;
    private final LinkedHashMap<String, TreeSet<Long>> byKey = new LinkedHashMap<>();
    private int touchCounter;
    private final LinkedHashMap<String, Integer> touchOrder = new LinkedHashMap<>();

    public TombstoneStore(int maxKeys, long ttlMillis) {
        if (maxKeys < 1) {
            throw new IllegalArgumentException("maxKeys must be >= 1");
        }
        if (ttlMillis < 0) {
            throw new IllegalArgumentException("ttlMillis must be >= 0");
        }
        this.maxKeys = maxKeys;
        this.ttlMillis = ttlMillis;
    }

    public int sizeKeys() {
        return byKey.size();
    }

    public long ttlMillis() {
        return ttlMillis;
    }

    /** Record an accepted DELETE event time for a key. */
    public synchronized void addDelete(String key, long deleteTime) {
        TreeSet<Long> times = byKey.computeIfAbsent(key, k -> new TreeSet<>());
        times.add(deleteTime);
        touchOrder.put(key, touchCounter++);
        if (byKey.size() > maxKeys) {
            evictCapacity();
        }
    }

    /**
     * Decide suppression/uncertainty for an UPSERT of key at event time t.
     */
    public synchronized TombstoneDecision lookup(String key, long t, long watermark) {
        TreeSet<Long> times = byKey.get(key);
        if (times != null) {
            Long cover = times.ceiling(t);
            if (cover != null) {
                return TombstoneDecision.SUPPRESSED;
            }
        }
        boolean uncertain = watermark != Long.MIN_VALUE && watermark - ttlMillis > t;
        return uncertain ? TombstoneDecision.UNCERTAIN : TombstoneDecision.NONE;
    }

    /** Watermark-driven release; returns number of individual tombstones released. */
    public synchronized int evictExpired(long watermark) {
        if (watermark == Long.MIN_VALUE) {
            return 0;
        }
        int removed = 0;
        Iterator<Map.Entry<String, TreeSet<Long>>> it = byKey.entrySet().iterator();
        while (it.hasNext()) {
            Map.Entry<String, TreeSet<Long>> en = it.next();
            TreeSet<Long> times = en.getValue();
            int before = times.size();
            times.removeIf(d -> watermark - ttlMillis > d);
            removed += before - times.size();
            if (times.isEmpty()) {
                it.remove();
                touchOrder.remove(en.getKey());
            }
        }
        return removed;
    }

    private void evictCapacity() {
        String victim = null;
        long victimLatest = Long.MAX_VALUE;
        int victimTouch = Integer.MAX_VALUE;
        for (Map.Entry<String, TreeSet<Long>> en : byKey.entrySet()) {
            long latest = en.getValue().last();
            int touch = touchOrder.getOrDefault(en.getKey(), Integer.MAX_VALUE);
            if (latest < victimLatest || (latest == victimLatest && touch < victimTouch)) {
                victim = en.getKey();
                victimLatest = latest;
                victimTouch = touch;
            }
        }
        if (victim != null) {
            byKey.remove(victim);
            touchOrder.remove(victim);
        }
    }

    // ----------------------------------------------------------------
    // Snapshot / restore
    // ----------------------------------------------------------------

    public synchronized Json.Value snapshot() {
        Json.JsonObject root = Json.obj();
        Json.JsonObject keys = Json.obj();
        for (Map.Entry<String, TreeSet<Long>> en : byKey.entrySet()) {
            Json.JsonArray arr = Json.arr();
            for (Long d : en.getValue()) {
                arr.elements().add(Json.num(d));
            }
            keys.members().put(en.getKey(), arr);
        }
        root.members().put("keys", keys);
        root.members().put("touchCounter", Json.num(touchCounter));
        return root;
    }

    public synchronized void restore(Json.JsonObject root) {
        byKey.clear();
        touchOrder.clear();
        touchCounter = (int) root.getLong("touchCounter", 0);
        if (root.get("keys") instanceof Json.JsonObject keys) {
            int i = 0;
            for (Map.Entry<String, Json.Value> en : keys.members().entrySet()) {
                if (en.getValue() instanceof Json.JsonArray arr) {
                    TreeSet<Long> times = new TreeSet<>();
                    for (Json.Value v : arr.elements()) {
                        if (v instanceof Json.JsonNumber n) {
                            times.add(n.longValue());
                        }
                    }
                    byKey.put(en.getKey(), times);
                    touchOrder.put(en.getKey(), i++);
                }
            }
        }
    }

    /** Debug view. */
    public synchronized List<String> retainedKeys() {
        return new ArrayList<>(byKey.keySet());
    }
}
