package com.example.tjoin.state;

import com.example.tjoin.model.StreamEvent;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.NavigableMap;
import java.util.Set;
import java.util.TreeMap;

/**
 * Buffered state of one side of the join.
 *
 * <p>Events are indexed two ways:</p>
 * <ul>
 *   <li>{@code byKey}: key -> event-time -> list of events (duplicate
 *       values/ids at the same timestamp are kept as distinct entries),</li>
 *   <li>a global {@code byTime} ordered map of event-time -> set of ids, so
 *       DROP_OLDEST and size accounting do not scan every key.</li>
 * </ul>
 *
 * <p>All events are also remembered in {@code admittedIds}, which serves
 * duplicate-id detection. Ids of cleaned/evicted events are forgotten
 * (redelivery after cleanup is treated as new — see README "Delivery
 * guarantees").</p>
 */
public final class SideState {

    /** key -> (eventTime -> events at that timestamp for that key) */
    private final Map<String, TreeMap<Long, List<StreamEvent>>> byKey = new HashMap<>();

    /** eventTime -> ids (global timestamp order, across keys) */
    private final TreeMap<Long, Set<String>> byTime = new TreeMap<>();

    /** id -> event, for every event currently buffered. */
    private final Map<String, StreamEvent> admitted = new HashMap<>();

    /** Returns true if this id is currently buffered on this side. */
    public boolean containsId(String id) {
        return admitted.containsKey(id);
    }

    public int size() {
        return admitted.size();
    }

    public boolean isEmpty() {
        return admitted.isEmpty();
    }

    /** Buffer a new event. */
    public void add(StreamEvent event) {
        admitted.put(event.getId(), event);
        byKey.computeIfAbsent(event.getKey(), k -> new TreeMap<>())
                .computeIfAbsent(event.getTimestamp(), t -> new ArrayList<>())
                .add(event);
        byTime.computeIfAbsent(event.getTimestamp(), t -> new HashSet<>()).add(event.getId());
    }

    /**
     * Every buffered event for {@code key} with event time in the inclusive
     * range {@code [fromTs, toTs]}.
     */
    public List<StreamEvent> rangeByKey(String key, long fromTs, long toTs) {
        TreeMap<Long, List<StreamEvent>> perKey = byKey.get(key);
        if (perKey == null) {
            return List.of();
        }
        NavigableMap<Long, List<StreamEvent>> slice = perKey.subMap(fromTs, true, toTs, true);
        List<StreamEvent> out = new ArrayList<>();
        for (List<StreamEvent> bucket : slice.values()) {
            out.addAll(bucket);
        }
        return out;
    }

    /**
     * Remove and return every buffered event with event time strictly less
     * than {@code thresholdTs}. Used for watermark-driven state cleanup
     * (the caller computes the side-specific threshold).
     */
    public List<StreamEvent> removeOlderThan(long thresholdTs) {
        List<StreamEvent> removed = new ArrayList<>();
        if (byTime.isEmpty() || byTime.firstKey() >= thresholdTs) {
            return removed;
        }
        NavigableMap<Long, Set<String>> head = byTime.headMap(thresholdTs, false);
        for (Iterator<Map.Entry<Long, Set<String>>> it = head.entrySet().iterator(); it.hasNext(); ) {
            Map.Entry<Long, Set<String>> e = it.next();
            for (String id : new ArrayList<>(e.getValue())) {
                StreamEvent ev = admitted.remove(id);
                if (ev != null) {
                    removed.add(ev);
                    removeFromKeyIndex(ev);
                }
            }
            it.remove();
        }
        return removed;
    }

    /**
     * Remove and return the buffered event with the smallest event time
     * (ties broken by smallest id for determinism). Returns null if empty.
     */
    public StreamEvent removeOldest() {
        if (byTime.isEmpty()) {
            return null;
        }
        Map.Entry<Long, Set<String>> firstEntry = byTime.firstEntry();
        String oldestId = firstEntry.getValue().stream().min(String::compareTo).orElseThrow();
        firstEntry.getValue().remove(oldestId);
        if (firstEntry.getValue().isEmpty()) {
            byTime.pollFirstEntry();
        }
        StreamEvent ev = admitted.remove(oldestId);
        if (ev != null) {
            removeFromKeyIndex(ev);
        }
        return ev;
    }

    private void removeFromKeyIndex(StreamEvent ev) {
        TreeMap<Long, List<StreamEvent>> perKey = byKey.get(ev.getKey());
        if (perKey == null) {
            return;
        }
        List<StreamEvent> bucket = perKey.get(ev.getTimestamp());
        if (bucket != null) {
            bucket.removeIf(e -> e.getId().equals(ev.getId()));
            if (bucket.isEmpty()) {
                perKey.remove(ev.getTimestamp());
            }
        }
        if (perKey.isEmpty()) {
            byKey.remove(ev.getKey());
        }
    }
}
