package com.example.dedup.model;

import com.example.dedup.json.Json;

import java.util.LinkedHashMap;
import java.util.Map;

/** Observable counters/gauges for the whole pipeline. */
public final class Metrics {
    public long eventsIn;
    public long acceptedOut;
    public long duplicatesIn;
    public long payloadMismatches;
    public long eventTimeSkews;
    public long lateIn;
    public long unverifiedDuplicates;
    public long tombstoneSuppressed;
    public long tombstoneUncertain;
    public long windowLateDropped;
    public long capacityEvictions;
    public long watermarkEvictions;
    public long tombstoneEvictions;
    public long watermarkRegressions;
    public long windowsFired;
    public long clockRollbacks;

    public long watermark = Long.MIN_VALUE;
    public int activeDedupEntries;
    public int activeTombstoneKeys;

    public void inc(String name) {
        add(name, 1);
    }

    public void add(String name, long delta) {
        switch (name) {
            case "eventsIn" -> eventsIn += delta;
            case "acceptedOut" -> acceptedOut += delta;
            case "duplicatesIn" -> duplicatesIn += delta;
            case "payloadMismatches" -> payloadMismatches += delta;
            case "eventTimeSkews" -> eventTimeSkews += delta;
            case "lateIn" -> lateIn += delta;
            case "unverifiedDuplicates" -> unverifiedDuplicates += delta;
            case "tombstoneSuppressed" -> tombstoneSuppressed += delta;
            case "tombstoneUncertain" -> tombstoneUncertain += delta;
            case "windowLateDropped" -> windowLateDropped += delta;
            case "capacityEvictions" -> capacityEvictions += delta;
            case "watermarkEvictions" -> watermarkEvictions += delta;
            case "tombstoneEvictions" -> tombstoneEvictions += delta;
            case "watermarkRegressions" -> watermarkRegressions += delta;
            case "windowsFired" -> windowsFired += delta;
            case "clockRollbacks" -> clockRollbacks += delta;
            default -> throw new IllegalArgumentException("unknown metric: " + name);
        }
    }

    public Json.Value toJson() {
        Json.JsonObject o = Json.obj();
        Map<String, Json.Value> m = new LinkedHashMap<>();
        m.put("eventsIn", Json.num(eventsIn));
        m.put("acceptedOut", Json.num(acceptedOut));
        m.put("duplicatesIn", Json.num(duplicatesIn));
        m.put("payloadMismatches", Json.num(payloadMismatches));
        m.put("eventTimeSkews", Json.num(eventTimeSkews));
        m.put("lateIn", Json.num(lateIn));
        m.put("unverifiedDuplicates", Json.num(unverifiedDuplicates));
        m.put("tombstoneSuppressed", Json.num(tombstoneSuppressed));
        m.put("tombstoneUncertain", Json.num(tombstoneUncertain));
        m.put("windowLateDropped", Json.num(windowLateDropped));
        m.put("capacityEvictions", Json.num(capacityEvictions));
        m.put("watermarkEvictions", Json.num(watermarkEvictions));
        m.put("tombstoneEvictions", Json.num(tombstoneEvictions));
        m.put("watermarkRegressions", Json.num(watermarkRegressions));
        m.put("windowsFired", Json.num(windowsFired));
        m.put("clockRollbacks", Json.num(clockRollbacks));
        m.put("watermark", watermark == Long.MIN_VALUE ? Json.JsonNull.INSTANCE : Json.num(watermark));
        m.put("activeDedupEntries", Json.num(activeDedupEntries));
        m.put("activeTombstoneKeys", Json.num(activeTombstoneKeys));
        o.members().putAll(m);
        return o;
    }
}
