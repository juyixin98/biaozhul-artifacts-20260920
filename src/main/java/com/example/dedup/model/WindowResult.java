package com.example.dedup.model;

import com.example.dedup.json.Json;

/**
 * Fired result for one tumbling event-time window and key.
 * For each key, the last UPSERT payload (by arrival) and its id are kept;
 * deleteCount counts DELETE events for the key within the window.
 */
public record WindowResult(
        long windowStart,
        long windowEnd,
        String key,
        String lastUpsertId,
        Json.Value lastPayload,
        long upsertCount,
        long deleteCount,
        boolean deletedByTombstone) {

    public Json.Value toJson() {
        Json.JsonObject o = Json.obj();
        o.members().put("windowStart", Json.num(windowStart));
        o.members().put("windowEnd", Json.num(windowEnd));
        o.members().put("key", Json.str(key));
        o.members().put("lastUpsertId", lastUpsertId == null ? Json.JsonNull.INSTANCE : Json.str(lastUpsertId));
        o.members().put("lastPayload", lastPayload == null ? Json.JsonNull.INSTANCE : lastPayload);
        o.members().put("upsertCount", Json.num(upsertCount));
        o.members().put("deleteCount", Json.num(deleteCount));
        o.members().put("deletedByTombstone", Json.JsonBool.of(deletedByTombstone));
        return o;
    }
}
