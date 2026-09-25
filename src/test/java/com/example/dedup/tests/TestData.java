package com.example.dedup.tests;

import com.example.dedup.json.Json;
import com.example.dedup.model.Event;

import java.util.TreeMap;

/** Helpers for building events/payloads in tests. */
final class TestData {
    private TestData() {
    }

    static Json.Value payload(String k, long v) {
        Json.JsonObject o = Json.obj();
        o.members().put(k, Json.num(v));
        return o;
    }

    static Json.Value payload(String k, String v) {
        Json.JsonObject o = Json.obj();
        o.members().put(k, Json.str(v));
        return o;
    }

    static Event upsert(String id, long t, String key, Json.Value payload) {
        return Event.upsert(id, t, key, payload);
    }

    static Event delete(String id, long t, String key) {
        return Event.delete(id, t, key);
    }

    static TreeMap<String, Object> ordered() {
        return new TreeMap<>();
    }
}
