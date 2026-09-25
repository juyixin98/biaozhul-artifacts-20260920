package com.example.dedup.window;

import com.example.dedup.json.Json;
import com.example.dedup.model.Event;
import com.example.dedup.model.WindowResult;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Tumbling event-time window operator with bounded lateness.
 *
 * One accumulator per (windowStart, key). A window
 * [n*size, (n+1)*size) becomes eligible to fire once
 * watermark &gt;= (n+1)*size + allowedLatenessMillis; an accepted event
 * belonging to an already-eligible window is "too late": it is not folded
 * in (the result was already emitted) and reported back as a late drop.
 */
public final class TumblingWindowOperator {

    private final long sizeMillis;
    private final long allowedLatenessMillis;
    // windowStart -> (key -> acc)
    private final TreeMap<Long, Map<String, KeyAcc>> windows = new TreeMap<>();

    public TumblingWindowOperator(long sizeMillis, long allowedLatenessMillis) {
        if (sizeMillis <= 0) {
            throw new IllegalArgumentException("window size must be > 0");
        }
        if (allowedLatenessMillis < 0) {
            throw new IllegalArgumentException("allowedLatenessMillis must be >= 0");
        }
        this.sizeMillis = sizeMillis;
        this.allowedLatenessMillis = allowedLatenessMillis;
    }

    public long sizeMillis() {
        return sizeMillis;
    }

    public long allowedLatenessMillis() {
        return allowedLatenessMillis;
    }

    public long windowStart(long eventTime) {
        return Math.floorDiv(eventTime, sizeMillis) * sizeMillis;
    }

    public boolean isWindowLate(long eventTime, long watermark) {
        if (watermark == Long.MIN_VALUE) {
            return false;
        }
        long start = windowStart(eventTime);
        return watermark >= start + sizeMillis + allowedLatenessMillis;
    }

    /** Fold one accepted event. Caller has already checked {@link #isWindowLate}. */
    public void add(Event e) {
        long start = windowStart(e.eventTime());
        Map<String, KeyAcc> byKey = windows.computeIfAbsent(start, k -> new TreeMap<>());
        KeyAcc acc = byKey.computeIfAbsent(e.key(), k -> new KeyAcc());
        switch (e.type()) {
            case UPSERT -> {
                acc.upsertCount++;
                acc.lastUpsertId = e.id();
                acc.lastPayload = e.payload();
                if (e.eventTime() > acc.lastUpsertTime) {
                    acc.lastUpsertTime = e.eventTime();
                }
            }
            case DELETE -> {
                acc.deleteCount++;
                if (e.eventTime() > acc.lastDeleteTime) {
                    acc.lastDeleteTime = e.eventTime();
                }
            }
        }
    }

    /** Fire and purge every window eligible under the new watermark. */
    public List<WindowResult> fire(long watermark) {
        List<WindowResult> out = new ArrayList<>();
        if (watermark == Long.MIN_VALUE) {
            return out;
        }
        Iterator<Map.Entry<Long, Map<String, KeyAcc>>> it = windows.entrySet().iterator();
        while (it.hasNext()) {
            Map.Entry<Long, Map<String, KeyAcc>> w = it.next();
            long start = w.getKey();
            if (watermark < start + sizeMillis + allowedLatenessMillis) {
                break; // TreeMap is ordered; later windows cannot be eligible
            }
            for (Map.Entry<String, KeyAcc> ka : w.getValue().entrySet()) {
                KeyAcc acc = ka.getValue();
                boolean deletedByTombstone = acc.deleteCount > 0
                        && acc.lastDeleteTime >= acc.lastUpsertTime;
                out.add(new WindowResult(
                        start,
                        start + sizeMillis,
                        ka.getKey(),
                        acc.lastUpsertId,
                        acc.lastPayload,
                        acc.upsertCount,
                        acc.deleteCount,
                        deletedByTombstone));
            }
            it.remove();
        }
        return out;
    }

    // ----------------------------------------------------------------
    // Snapshot / restore
    // ----------------------------------------------------------------

    public Json.Value snapshot() {
        Json.JsonObject root = Json.obj();
        Json.JsonObject ws = Json.obj();
        for (Map.Entry<Long, Map<String, KeyAcc>> w : windows.entrySet()) {
            Json.JsonObject ks = Json.obj();
            for (Map.Entry<String, KeyAcc> k : w.getValue().entrySet()) {
                KeyAcc a = k.getValue();
                Json.JsonObject ao = Json.obj();
                ao.members().put("lastUpsertId",
                        a.lastUpsertId == null ? Json.JsonNull.INSTANCE : Json.str(a.lastUpsertId));
                ao.members().put("lastPayload", a.lastPayload == null ? Json.JsonNull.INSTANCE : a.lastPayload);
                ao.members().put("upsertCount", Json.num(a.upsertCount));
                ao.members().put("deleteCount", Json.num(a.deleteCount));
                ao.members().put("lastUpsertTime", Json.num(a.lastUpsertTime));
                ao.members().put("lastDeleteTime", Json.num(a.lastDeleteTime));
                ks.members().put(k.getKey(), ao);
            }
            ws.members().put(String.valueOf(w.getKey()), ks);
        }
        root.members().put("windows", ws);
        return root;
    }

    public void restore(Json.JsonObject root) {
        windows.clear();
        if (root.get("windows") instanceof Json.JsonObject ws) {
            for (Map.Entry<String, Json.Value> w : ws.members().entrySet()) {
                long start = Long.parseLong(w.getKey());
                Map<String, KeyAcc> byKey = new TreeMap<>();
                if (w.getValue() instanceof Json.JsonObject ks) {
                    for (Map.Entry<String, Json.Value> k : ks.members().entrySet()) {
                        if (k.getValue() instanceof Json.JsonObject ao) {
                            KeyAcc a = new KeyAcc();
                            if (ao.get("lastUpsertId") instanceof Json.JsonString s) {
                                a.lastUpsertId = s.value();
                            }
                            Json.Value p = ao.get("lastPayload");
                            if (!(p instanceof Json.JsonNull)) {
                                a.lastPayload = p;
                            }
                            a.upsertCount = ao.getLong("upsertCount", 0);
                            a.deleteCount = ao.getLong("deleteCount", 0);
                            a.lastUpsertTime = ao.getLong("lastUpsertTime", Long.MIN_VALUE);
                            a.lastDeleteTime = ao.getLong("lastDeleteTime", Long.MIN_VALUE);
                            byKey.put(k.getKey(), a);
                        }
                    }
                }
                windows.put(start, byKey);
            }
        }
    }
}
