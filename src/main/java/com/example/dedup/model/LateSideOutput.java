package com.example.dedup.model;

import com.example.dedup.json.Json;

/** Side output: an accepted-but-late event the window operator could not place. */
public record LateSideOutput(Event event, long watermark, String reason) {

    public Json.Value toJson() {
        Json.JsonObject o = Json.obj();
        o.members().put("event", event.toJson());
        o.members().put("watermark", Json.num(watermark));
        o.members().put("reason", Json.str(reason));
        return o;
    }
}
