package com.example.dedup.model;

import com.example.dedup.json.Json;

/**
 * Result of ingesting one event. Observable flags make every edge case
 * explicit to the caller rather than silently changing output semantics.
 *
 * @param event                  the ingested event
 * @param accepted               true if this is the first live occurrence (passes dedup)
 * @param duplicate              same id seen while state for it was still retained
 * @param payloadMismatch        duplicate occurrence carried a different payload
 * @param eventTimeSkew          duplicate occurrence had a different (esp. later) event time
 * @param late                   event time is behind the current watermark
 * @param dedupUnverified       duplicate whose state had already been released (outside promise)
 * @param suppressedByTombstone UPSERT hidden by a live DELETE tombstone for the same key
 * @param tombstoneUncertain    tombstone state for key had expired; suppression unknown
 * @param windowLateDropped     window operator discarded it (older than window+allowed lateness)
 * @param capacityEvicted       this insert forced an entry out of bounded dedup state
 */
public record IngestResult(
        Event event,
        boolean accepted,
        boolean duplicate,
        boolean payloadMismatch,
        boolean eventTimeSkew,
        boolean late,
        boolean dedupUnverified,
        boolean suppressedByTombstone,
        boolean tombstoneUncertain,
        boolean windowLateDropped,
        boolean capacityEvicted) {

    /** True if this occurrence produces an emitted (visible) record. */
    public boolean emitted() {
        return accepted && !suppressedByTombstone && !windowLateDropped;
    }

    public Json.Value toJson() {
        Json.JsonObject o = Json.obj();
        o.members().put("event", event.toJson());
        o.members().put("accepted", Json.JsonBool.of(accepted));
        o.members().put("emitted", Json.JsonBool.of(emitted()));
        o.members().put("duplicate", Json.JsonBool.of(duplicate));
        o.members().put("payloadMismatch", Json.JsonBool.of(payloadMismatch));
        o.members().put("eventTimeSkew", Json.JsonBool.of(eventTimeSkew));
        o.members().put("late", Json.JsonBool.of(late));
        o.members().put("dedupUnverified", Json.JsonBool.of(dedupUnverified));
        o.members().put("suppressedByTombstone", Json.JsonBool.of(suppressedByTombstone));
        o.members().put("tombstoneUncertain", Json.JsonBool.of(tombstoneUncertain));
        o.members().put("windowLateDropped", Json.JsonBool.of(windowLateDropped));
        o.members().put("capacityEvicted", Json.JsonBool.of(capacityEvicted));
        return o;
    }
}
