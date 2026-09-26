package hlc.server;

import hlc.HLCClock;
import hlc.HLCException;
import hlc.HLCFileStore;
import hlc.HLCTimestamp;
import hlc.PhysicalClock;
import hlc.TimeZoneInfo;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Node registry and request handlers for the stateful clock service.
 *
 * <p>This class speaks plain {@code Map<String,Object>} request/response objects (parsed
 * from JSON by {@link hlc.Json}) so it can be unit-tested without any HTTP transport. Each
 * node owns a {@link PhysicalClock.VirtualClock}: callers pin physical time on every
 * operation, which makes skew and backwards-clock steps explicit and reproducible.
 */
public final class ApiService {

    private final Map<String, PhysicalClock.VirtualClock> physicalClocks = new LinkedHashMap<>();
    private final Map<String, HLCClock> clocks = new LinkedHashMap<>();
    private final HLCFileStore store;

    public ApiService(HLCFileStore store) {
        this.store = store;
    }

    // ---------------------------------------------------------------- node management

    public synchronized Map<String, Object> createNode(Map<String, Object> req) {
        String node = hlc.Json.requireString(req, "node");
        if (clocks.containsKey(node)) {
            throw new HLCException("node already exists: " + node);
        }
        long start = req.containsKey("initialPhysicalMicros")
                ? hlc.Json.requireLong(req, "initialPhysicalMicros") : 0L;
        if (start < 0) {
            throw new HLCException("initialPhysicalMicros must be non-negative");
        }
        PhysicalClock.VirtualClock vc = new PhysicalClock.VirtualClock(start);
        clocks.put(node, new HLCClock(vc, HLCTimestamp.ZERO));
        physicalClocks.put(node, vc);
        return nodeView(node);
    }

    public synchronized Map<String, Object> listNodes() {
        java.util.List<Object> nodes = new java.util.ArrayList<>();
        for (String node : clocks.keySet()) {
            nodes.add(nodeView(node));
        }
        return Map.of("nodes", nodes, "tzdb", TimeZoneInfo.version());
    }

    // ---------------------------------------------------------------- clock operations

    public synchronized Map<String, Object> local(Map<String, Object> req) {
        String node = hlc.Json.requireString(req, "node");
        pinPhysical(node, req);
        HLCTimestamp ts = clock(node).tickLocal();
        return Map.of("node", node, "hlc", ts.toString(), "l", ts.l(), "c", ts.c());
    }

    public synchronized Map<String, Object> send(Map<String, Object> req) {
        String node = hlc.Json.requireString(req, "node");
        pinPhysical(node, req);
        HLCTimestamp ts = clock(node).send();
        // The stamped payload that travels with the message.
        Map<String, Object> message = new LinkedHashMap<>();
        message.put("from", node);
        message.put("hlc", ts.toString());
        message.put("l", ts.l());
        message.put("c", ts.c());
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("node", node);
        resp.put("hlc", ts.toString());
        resp.put("l", ts.l());
        resp.put("c", ts.c());
        resp.put("message", message);
        return resp;
    }

    public synchronized Map<String, Object> receive(Map<String, Object> req) {
        String node = hlc.Json.requireString(req, "node");
        pinPhysical(node, req);
        HLCTimestamp messageTs = extractTimestamp(req.get("message"), "message");
        HLCTimestamp ts = clock(node).receive(messageTs);
        return Map.of("node", node, "hlc", ts.toString(), "l", ts.l(), "c", ts.c(),
                "received", messageTs.toString());
    }

    public synchronized Map<String, Object> snapshot(Map<String, Object> req) {
        String node = hlc.Json.requireString(req, "node");
        HLCTimestamp ts = clock(node).peek();
        return Map.of("node", node, "hlc", ts.toString(), "l", ts.l(), "c", ts.c());
    }

    public synchronized Map<String, Object> restore(Map<String, Object> req) {
        String node = hlc.Json.requireString(req, "node");
        HLCTimestamp saved = extractTimestamp(req.get("state"), "state");
        // A node may be restored before it has been created (recovery after restart).
        PhysicalClock.VirtualClock vc =
                physicalClocks.computeIfAbsent(node, n -> new PhysicalClock.VirtualClock(0L));
        clocks.computeIfAbsent(node, n -> new HLCClock(vc)).restore(saved);
        return Map.of("node", node, "hlc", saved.toString(), "l", saved.l(), "c", saved.c(),
                "restored", true);
    }

    // ---------------------------------------------------------------- persistence

    public synchronized Map<String, Object> save() {
        Map<String, HLCTimestamp> snapshot = new LinkedHashMap<>();
        clocks.forEach((n, c) -> snapshot.put(n, c.peek()));
        store.save(snapshot);
        return Map.of("saved", true, "path", store.path().toString(),
                "nodes", snapshot.size(), "tzdb", TimeZoneInfo.version());
    }

    public synchronized Map<String, Object> load() {
        Map<String, HLCTimestamp> saved = store.load();
        java.util.List<Object> restored = new java.util.ArrayList<>();
        for (Map.Entry<String, HLCTimestamp> e : saved.entrySet()) {
            PhysicalClock.VirtualClock vc =
                    physicalClocks.computeIfAbsent(e.getKey(), n -> new PhysicalClock.VirtualClock(0L));
            clocks.computeIfAbsent(e.getKey(), n -> new HLCClock(vc)).restore(e.getValue());
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("node", e.getKey());
            item.put("hlc", e.getValue().toString());
            restored.add(item);
        }
        return Map.of("loaded", true, "path", store.path().toString(), "nodes", restored);
    }

    // ---------------------------------------------------------------- internals

    private void pinPhysical(String node, Map<String, Object> req) {
        if (!req.containsKey("physicalMicros")) {
            throw new HLCException("field 'physicalMicros' is required (virtual clocks are explicit)");
        }
        long pt = hlc.Json.requireLong(req, "physicalMicros");
        if (pt < 0) {
            throw new HLCException("physicalMicros must be non-negative");
        }
        PhysicalClock.VirtualClock vc = physicalClocks.get(node);
        if (vc == null) {
            throw new HLCException("unknown node: " + node + " (create it first)");
        }
        vc.set(pt);
    }

    private HLCClock clock(String node) {
        HLCClock c = clocks.get(node);
        if (c == null) {
            throw new HLCException("unknown node: " + node + " (create it first)");
        }
        return c;
    }

    private Map<String, Object> nodeView(String node) {
        HLCTimestamp ts = clocks.get(node).peek();
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("node", node);
        m.put("physicalMicros", physicalClocks.get(node).nowMicros());
        m.put("hlc", ts.toString());
        m.put("l", ts.l());
        m.put("c", ts.c());
        return m;
    }

    /** Accepts either {@code {"hlc":"<l>:<c>"}}/ {@code "<l>:<c>"} or {@code {"l":..,"c":..}}. */
    static HLCTimestamp extractTimestamp(Object raw, String field) {
        if (raw instanceof String s) {
            return HLCTimestamp.parse(s);
        }
        if (raw instanceof Map<?, ?> m) {
            Object wire = m.get("hlc");
            if (wire instanceof String s) {
                return HLCTimestamp.parse(s);
            }
            Object l = m.get("l");
            Object c = m.get("c");
            if (l instanceof Number ln && c instanceof Number cn) {
                return new HLCTimestamp(ln.longValue(), cn.longValue());
            }
        }
        throw new HLCException("field '" + field
                + "' must be an HLC timestamp string \"<l>:<c>\" or an object {l,c}");
    }

    /** Test hook: place a node clock at an arbitrary state. */
    synchronized void forceState(String node, HLCTimestamp ts) {
        PhysicalClock.VirtualClock vc =
                physicalClocks.computeIfAbsent(node, n -> new PhysicalClock.VirtualClock(ts.l()));
        clocks.computeIfAbsent(node, n -> new HLCClock(vc)).restore(ts);
    }
}
