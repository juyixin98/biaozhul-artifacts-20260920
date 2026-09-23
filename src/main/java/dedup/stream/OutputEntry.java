package dedup.stream;

import dedup.core.DedupResult;
import dedup.core.Event;
import dedup.json.Json;

/** 进入输出缓冲区的一条已发射事件（含去重判定，供 {@code /outputs} 观察）。 */
public record OutputEntry(long seq, Event event, DedupResult result) {

    public Json.Obj toJson() {
        Json.Obj o = new Json.Obj();
        o.put("seq", Json.Num.of(seq));
        o.put("event", event.toJson());
        o.put("decision", result.toJson());
        return o;
    }
}
