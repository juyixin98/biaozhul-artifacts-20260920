package com.example.sessionwindow.service;

import com.example.sessionwindow.engine.ReferenceGrouper;
import com.example.sessionwindow.engine.Results;
import com.example.sessionwindow.engine.SessionWindowEngine;
import com.example.sessionwindow.model.Aggregate;
import com.example.sessionwindow.model.Event;
import com.example.sessionwindow.model.ResultRecord;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Stateless batch driver: runs one request's ordered item list through a fresh
 * engine and compares the materialized result against the exact offline
 * grouping ({@link ReferenceGrouper}). This is the deterministic,
 * watermark-explicit entry point used by the acceptance tests and by
 * {@code POST /session-windows/run}.
 */
public final class BatchProcessor {

    private BatchProcessor() {
    }

    public static final class Outcome {
        public final List<ResultRecord> records = new ArrayList<>();
        public final List<Event> acceptedEvents = new ArrayList<>();
        public final List<Event> droppedEvents = new ArrayList<>();
        public final Map<String, TreeMap<Long, Aggregate>> results = new LinkedHashMap<>();
        public final Map<String, List<Aggregate>> reference = new LinkedHashMap<>();
        public boolean matchesReference;
        public long finalWatermark;
        public int remainingKeys;
        public int remainingSessions;

        /**
         * Folds records into key -> ordered aggregates. SEALED/ADD entries
         * survive, RETRACT removes the addressed window.
         */
        public Map<String, List<Aggregate>> resultsAsLists() {
            Map<String, List<Aggregate>> out = new LinkedHashMap<>();
            for (Map.Entry<String, TreeMap<Long, Aggregate>> e : results.entrySet()) {
                out.put(e.getKey(), new ArrayList<>(e.getValue().values()));
            }
            return out;
        }
    }

    /** One input item: either an event or a watermark marker. */
    public record Item(Event event, Long watermark) {
        public boolean isWatermark() {
            return watermark != null;
        }

        public static Item event(Event e) {
            return new Item(e, null);
        }

        public static Item watermark(long w) {
            return new Item(null, w);
        }
    }

    public static Outcome run(long gap, long allowedLateness, List<Item> items, boolean flushAtEnd) {
        SessionWindowEngine engine = new SessionWindowEngine(gap, allowedLateness);
        List<Event> allEvents = new ArrayList<>();

        for (Item item : items) {
            if (item.isWatermark()) {
                engine.processWatermark(item.watermark());
            } else {
                allEvents.add(item.event());
                engine.processEvent(item.event());
            }
        }
        if (flushAtEnd) {
            engine.flush();
        }

        Outcome out = new Outcome();
        out.records.addAll(engine.drainRecords());
        out.finalWatermark = engine.watermark();

        // Identity-based split: dropped events never touched engine state.
        for (ResultRecord r : out.records) {
            if (r.type() == ResultRecord.Type.DROPPED) {
                out.droppedEvents.add(r.event());
            }
        }
        for (Event e : allEvents) {
            boolean dropped = false;
            for (Event d : out.droppedEvents) {
                if (d == e) {
                    dropped = true;
                    break;
                }
            }
            if (!dropped) {
                out.acceptedEvents.add(e);
            }
        }

        out.results.putAll(Results.byKey(out.records));
        out.reference.putAll(ReferenceGrouper.group(out.acceptedEvents, gap));
        out.matchesReference = compare(out.resultsAsLists(), out.reference);
        out.remainingKeys = engine.keyCount();
        out.remainingSessions = engine.totalSessionCount();
        return out;
    }

    private static boolean compare(Map<String, List<Aggregate>> online,
                                   Map<String, List<Aggregate>> offline) {
        if (!online.keySet().equals(offline.keySet())) {
            return false;
        }
        for (String key : online.keySet()) {
            List<Aggregate> a = online.get(key);
            List<Aggregate> b = offline.get(key);
            if (a.size() != b.size()) {
                return false;
            }
            for (int i = 0; i < a.size(); i++) {
                if (!sameAggregate(a.get(i), b.get(i))) {
                    return false;
                }
            }
        }
        return true;
    }

    private static boolean sameAggregate(Aggregate a, Aggregate b) {
        return a.start() == b.start()
                && a.end() == b.end()
                && a.count() == b.count()
                && a.min() == b.min()
                && a.max() == b.max()
                && Math.abs(a.sum() - b.sum()) < 1e-9;
    }
}
