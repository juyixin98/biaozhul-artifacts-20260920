package hlc.server;

import hlc.CausalityEngine;
import hlc.HLCException;
import hlc.HLCTimestamp;
import hlc.Json;
import hlc.TimeZoneInfo;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Stateless handler for fixed-data causality scenarios:
 * it runs a declared interleaving of events through {@link CausalityEngine} and reports
 * per-event timestamps, soundness, and timestamp-ordered-but-concurrent event pairs.
 */
public final class ScenarioService {

    private ScenarioService() {
    }

    public static Map<String, Object> simulate(Map<String, Object> req) {
        Object eventsRaw = req.get("events");
        if (!(eventsRaw instanceof List<?> eventsList)) {
            throw new HLCException("field 'events' must be an array");
        }
        if (eventsList.isEmpty()) {
            throw new HLCException("field 'events' must not be empty");
        }

        List<CausalityEngine.EventSpec> specs = new ArrayList<>();
        for (Object o : eventsList) {
            if (!(o instanceof Map<?, ?> em)) {
                throw new HLCException("each event must be an object");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> event = (Map<String, Object>) em;
            String label = Json.requireString(event, "label");
            String node = Json.requireString(event, "node");
            String kindText = Json.requireString(event, "type").toUpperCase(java.util.Locale.ROOT);
            CausalityEngine.Kind kind;
            try {
                kind = CausalityEngine.Kind.valueOf(kindText);
            } catch (IllegalArgumentException e) {
                throw new HLCException("event " + label + " has unknown type '" + kindText
                        + "'; expected LOCAL, SEND or RECV");
            }
            long pt = Json.requireLong(event, "physicalMicros");
            String sendRef = event.containsKey("from") ? String.valueOf(event.get("from")) : null;
            specs.add(new CausalityEngine.EventSpec(label, node, kind, pt, sendRef));
        }

        CausalityEngine.Report report = CausalityEngine.run(specs);

        List<Object> timeline = new ArrayList<>();
        for (CausalityEngine.Result r : report.results()) {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("step", r.step());
            item.put("label", r.label());
            item.put("node", r.node());
            item.put("type", r.kind().name());
            item.put("physicalMicros", r.physicalMicros());
            item.put("hlc", r.timestamp().toString());
            item.put("l", r.timestamp().l());
            item.put("c", r.timestamp().c());
            timeline.add(item);
        }

        List<Object> concurrent = new ArrayList<>();
        report.concurrentButOrdered().forEach(p -> concurrent.add(pairView(p)));

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("scenario", Json.optionalString(req, "name", "unnamed"));
        resp.put("tzdb", TimeZoneInfo.version());
        resp.put("timeline", timeline);
        resp.put("soundnessHolds", report.soundnessHolds());
        resp.put("violations", report.violations().stream().map(ScenarioService::pairView).toList());
        resp.put("concurrentButTimestampOrdered", concurrent);
        resp.put("interpretation",
                "happens-before(a,b) => ts(a) < ts(b) is sound when violations is empty; "
                        + "pairs in concurrentButTimestampOrdered prove ts(a) < ts(b) does NOT imply "
                        + "happens-before(a,b)");
        return resp;
    }

    private static Map<String, Object> pairView(CausalityEngine.Pair p) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("earlier", p.earlier());
        m.put("later", p.later());
        m.put("earlierHlc", ts(p.earlierTs()));
        m.put("laterHlc", ts(p.laterTs()));
        m.put("happensBefore", p.happensBefore());
        m.put("note", p.reason());
        return m;
    }

    private static Map<String, Object> ts(HLCTimestamp t) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("hlc", t.toString());
        m.put("l", t.l());
        m.put("c", t.c());
        return m;
    }
}
