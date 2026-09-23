package com.example.sessionwindow;

import java.util.Map;

/**
 * One changelog record. A consumer builds the materialized view by:
 *   UPSERT  -> put versioned session snapshot (keyed by sessionId)
 *   RETRACT -> remove the session snapshot
 *
 * Records sharing the same revision are one atomic submission: fold them
 * together (retract first, upsert second) so intermediate states are never read.
 *
 * @param seq        monotonic, gap-free output sequence number
 * @param revision   atomic revision id; all records of one submission share it
 * @param kind       UPSERT or RETRACT
 * @param sessionId  stable session id, e.g. "k#1"
 * @param key        grouping key
 * @param version    snapshot version within that session (retract carries the last version)
 * @param startTs    session start event time
 * @param endTs      session end event time
 * @param count      number of events in the session
 * @param watermark  effective watermark at the time of emission (may be null)
 */
public record Change(
        long seq,
        long revision,
        Kind kind,
        String sessionId,
        String key,
        int version,
        long startTs,
        long endTs,
        int count,
        Long watermark) {

    public enum Kind {
        UPSERT, RETRACT
    }

    public Map<String, Object> toMap() {
        var m = new java.util.LinkedHashMap<String, Object>();
        m.put("seq", seq);
        m.put("revision", revision);
        m.put("kind", kind.name());
        m.put("sessionId", sessionId);
        m.put("key", key);
        m.put("version", version);
        m.put("startTs", startTs);
        m.put("endTs", endTs);
        m.put("count", count);
        if (watermark != null) {
            m.put("watermark", watermark);
        }
        return m;
    }
}
