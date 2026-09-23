package approx.server;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import approx.cms.SketchIncompatibleException;
import approx.cms.SketchSnapshot;
import approx.json.Json;
import approx.json.JsonException;
import approx.stream.EngineConfig;
import approx.stream.Event;
import approx.stream.FrequentItemsEngine;
import approx.stream.WindowResult;
import approx.time.Clock;
import approx.time.TaskScheduler;

/**
 * JSON/HTTP facade over {@link FrequentItemsEngine} using only the JDK's
 * built-in {@code com.sun.net.httpserver.HttpServer} (no external frameworks).
 *
 * <p>Routes:
 * <pre>
 * POST   /v1/engines                       create engine
 * GET    /v1/engines                       list engines
 * GET    /v1/engines/{id}                  engine stats
 * DELETE /v1/engines/{id}                  remove engine
 * POST   /v1/engines/{id}/events           add events
 * GET    /v1/engines/{id}/topk?k=          candidate top-K (coverage NOT guaranteed)
 * GET    /v1/engines/{id}/items/{item}     point estimate + declared error bound
 * GET    /v1/engines/{id}/sketch           serialized sketch snapshot
 * POST   /v1/engines/{id}/merge            merge a compatible snapshot
 * POST   /v1/engines/{id}/flush            force the active window to close
 * GET    /v1/engines/{id}/windows          closed-window summaries
 * GET    /v1/engines/{id}/windows/{n}      one closed-window result
 * POST   /v1/admin/advance-time            (manual time only) advance virtual clock
 * GET    /healthz                          liveness
 * </pre>
 */
public final class HttpEventServer implements AutoCloseable {

    private final HttpServer server;
    private final Clock clock;
    private final TaskScheduler scheduler;
    private final boolean manualTime;
    private final Map<String, FrequentItemsEngine> engines = new ConcurrentHashMap<>();
    private final Map<String, EngineConfig> configs = new ConcurrentHashMap<>();

    private HttpEventServer(HttpServer server, Clock clock, TaskScheduler scheduler, boolean manualTime) {
        this.server = server;
        this.clock = clock;
        this.scheduler = scheduler;
        this.manualTime = manualTime;
    }

    /** Create and start the server. {@code env} supplies both clock and scheduler. */
    public static HttpEventServer start(int port, Clock clock, TaskScheduler scheduler, boolean manualTime)
            throws IOException {
        HttpServer http = HttpServer.create(new java.net.InetSocketAddress(port), 0);
        HttpEventServer svc = new HttpEventServer(http, clock, scheduler, manualTime);
        svc.registerRoutes();
        // The JDK default executor dispatches handlers directly on the single
        // selector thread; give handlers their own fixed pool so request bodies
        // are always fully read without depending on selector-delivery timing.
        http.setExecutor(java.util.concurrent.Executors.newFixedThreadPool(8, r -> {
            Thread t = new Thread(r, "approx-http");
            t.setDaemon(true);
            return t;
        }));
        http.start();
        return svc;
    }

    public int port() {
        return server.getAddress().getPort();
    }

    @Override
    public void close() {
        server.stop(0);
        for (FrequentItemsEngine engine : engines.values()) {
            engine.close();
        }
    }

    private void registerRoutes() {
        server.createContext("/", this::dispatch);
    }

    // ---------------------------------------------------------------- routing

    private void dispatch(HttpExchange ex) throws IOException {
        try {
            String method = ex.getRequestMethod();
            String path = ex.getRequestURI().getPath();
            if (path.equals("/healthz") && method.equals("GET")) {
                sendJson(ex, 200, mapOf("status", "ok", "timeMillis", clock.nowMillis()));
                return;
            }
            if (path.equals("/v1/engines") && method.equals("POST")) {
                createEngine(ex);
                return;
            }
            if (path.equals("/v1/engines") && method.equals("GET")) {
                listEngines(ex);
                return;
            }
            if (path.startsWith("/v1/engines/")) {
                routeEngine(ex, method, path);
                return;
            }
            if (path.equals("/v1/admin/advance-time") && method.equals("POST")) {
                advanceTime(ex);
                return;
            }
            sendError(ex, 404, "not_found", "no route for " + method + " " + path);
        } catch (SketchIncompatibleException sie) {
            sendError(ex, 409, "sketch_incompatible", sie.getMessage());
        } catch (JsonException je) {
            sendError(ex, 400, "invalid_json", je.getMessage());
        } catch (IllegalArgumentException iae) {
            sendError(ex, 400, "invalid_request", iae.getMessage());
        } catch (RuntimeException re) {
            sendError(ex, 500, "internal_error", re.toString());
        }
    }

    private void routeEngine(HttpExchange ex, String method, String path) throws IOException {
        String rest = path.substring("/v1/engines/".length());
        // stripTrailingSlash: "/v1/engines/svc/" and "/.../svc" are the same resource
        if (rest.endsWith("/") && rest.length() > 1) {
            rest = rest.substring(0, rest.length() - 1);
        }
        String[] parts = rest.split("/");
        String engineId = parts[0];
        FrequentItemsEngine engine = engines.get(engineId);
        String action = parts.length >= 2 ? parts[1] : null;

        if (action == null) {
            if (engine == null) {
                sendError(ex, 404, "engine_not_found", engineId);
                return;
            }
            if (method.equals("GET")) {
                sendJson(ex, 200, engine.stats());
            } else if (method.equals("DELETE")) {
                engines.remove(engineId);
                configs.remove(engineId);
                engine.close();
                sendJson(ex, 200, mapOf("deleted", engineId));
            } else {
                sendError(ex, 405, "method_not_allowed", method);
            }
            return;
        }

        if (engine == null) {
            sendError(ex, 404, "engine_not_found", engineId);
            return;
        }

        switch (action) {
            case "events":
                if (method.equals("POST")) {
                    postEvents(ex, engine);
                } else {
                    sendError(ex, 405, "method_not_allowed", method);
                }
                break;
            case "topk":
                if (method.equals("GET")) {
                    getTopK(ex, engine);
                } else {
                    sendError(ex, 405, "method_not_allowed", method);
                }
                break;
            case "items":
                if (method.equals("GET") && parts.length == 3) {
                    // Item is the path segment after /items/ and is percent-encoded.
                    String item = java.net.URLDecoder.decode(parts[2], StandardCharsets.UTF_8);
                    sendJson(ex, 200, engine.queryItem(item));
                } else {
                    sendError(ex, 404, "not_found", path);
                }
                break;
            case "sketch":
                if (method.equals("GET")) {
                    sendJson(ex, 200, engine.activeSnapshot().toMap());
                } else {
                    sendError(ex, 405, "method_not_allowed", method);
                }
                break;
            case "merge":
                if (method.equals("POST")) {
                    postMerge(ex, engine);
                } else {
                    sendError(ex, 405, "method_not_allowed", method);
                }
                break;
            case "flush":
                if (method.equals("POST")) {
                    WindowResult r = engine.flush();
                    sendJson(ex, 200, windowToMap(r, false));
                } else {
                    sendError(ex, 405, "method_not_allowed", method);
                }
                break;
            case "windows":
                if (method.equals("GET") && parts.length == 2) {
                    listWindows(ex, engine);
                } else if (method.equals("GET") && parts.length == 3) {
                    getWindow(ex, engine, parts[2]);
                } else {
                    sendError(ex, 404, "not_found", path);
                }
                break;
            default:
                sendError(ex, 404, "not_found", path);
        }
    }

    // ---------------------------------------------------------------- handlers

    private void createEngine(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonBody(ex);
        String id = asString(body, "id", "engine-" + (engines.size() + 1));
        if (engines.containsKey(id)) {
            throw new IllegalArgumentException("engine already exists: " + id);
        }
        EngineConfig.Builder b = EngineConfig.builder();
        if (body.containsKey("width") || body.containsKey("depth")) {
            int width = asInt(body, "width", 64);
            int depth = asInt(body, "depth", 5);
            b.sketchDimensions(width, depth);
        } else if (body.containsKey("epsilon")) {
            double epsilon = asDouble(body, "epsilon");
            double delta = asDouble(body, "delta", 0.01);
            b = EngineConfig.withErrorTarget(epsilon, delta);
        }
        if (body.containsKey("seed")) {
            b.seed(asLong(body, "seed"));
        }
        b.candidateCapacity(asInt(body, "candidateCapacity", 10));
        b.windowMillis(asLong(body, "windowMillis", 0L));
        b.trackExact(asBool(body, "trackExact", false));
        EngineConfig config = b.build();

        FrequentItemsEngine engine = new FrequentItemsEngine(id, config, clock, scheduler);
        engine.start();
        engines.put(id, engine);
        configs.put(id, config);
        sendJson(ex, 201, mapOf("created", id, "config", configMap(id, config)));
    }

    private void listEngines(HttpExchange ex) throws IOException {
        List<Object> list = new ArrayList<>();
        for (Map.Entry<String, FrequentItemsEngine> e : engines.entrySet()) {
            list.add(e.getValue().stats());
        }
        sendJson(ex, 200, mapOf("engines", list));
    }

    private void postEvents(HttpExchange ex, FrequentItemsEngine engine) throws IOException {
        Map<String, Object> body = readJsonBody(ex);
        Object eventsObj = body.get("events");
        if (!(eventsObj instanceof List)) {
            throw new IllegalArgumentException("body.events must be an array");
        }
        List<Event<String>> events = new ArrayList<>();
        long defaultTs = asLong(body, "timestampMillis", clock.nowMillis());
        for (Object o : (List<?>) eventsObj) {
            if (o instanceof String) {
                events.add(new Event<>((String) o, defaultTs));
            } else if (o instanceof Map) {
                @SuppressWarnings("unchecked")
                Map<String, Object> em = (Map<String, Object>) o;
                String item = asString(em, "item", null);
                if (item == null) {
                    throw new IllegalArgumentException("each event needs an 'item' string");
                }
                long ts = asLong(em, "timestampMillis", defaultTs);
                long count = asLong(em, "count", 1L);
                if (count <= 0) {
                    throw new IllegalArgumentException("event.count must be positive");
                }
                for (long i = 0; i < count; i++) {
                    events.add(new Event<>(item, ts));
                }
            } else {
                throw new IllegalArgumentException("events must be strings or objects");
            }
        }
        FrequentItemsEngine.AddResult r = engine.addEvents(events);
        sendJson(ex, 200, mapOf(
                "engine", engine.id(),
                "accepted", r.accepted,
                "droppedLate", r.droppedLate,
                "activeWindowTotal", engine.stats().get("activeWindowTotal"),
                "activeCandidates", engine.stats().get("activeCandidates")));
    }

    private void getTopK(HttpExchange ex, FrequentItemsEngine engine) throws IOException {
        int k = queryParamInt(ex, "k", engine.config().candidateCapacity);
        if (k <= 0) {
            throw new IllegalArgumentException("k must be positive");
        }
        var rows = engine.currentTopK(k);
        List<Object> items = new ArrayList<>();
        for (Map.Entry<String, Long> e : rows) {
            items.add(mapOf("item", e.getKey(), "estimate", e.getValue()));
        }
        long bound = (long) engine.stats().get("activeErrorUpperBound");
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("engine", engine.id());
        resp.put("k", k);
        resp.put("items", items);
        resp.put("estimateErrorUpperBound", bound);
        resp.put("coverageGuarantee",
                "none: items are a projection of the bounded candidate set; "
                        + "a true top-K item may have been evicted. Point counts carry the bound above.");
        sendJson(ex, 200, resp);
    }

    private void postMerge(HttpExchange ex, FrequentItemsEngine engine) throws IOException {
        Map<String, Object> body = readJsonBody(ex);
        Object sketchObj = body.get("sketch");
        if (sketchObj == null) {
            throw new IllegalArgumentException("body.sketch is required");
        }
        SketchSnapshot snapshot = SketchSnapshot.fromMap(sketchObj);
        // Surface compatibility as 409 before mutating anything.
        if (snapshot.width != engine.config().width || snapshot.depth != engine.config().depth) {
            throw new SketchIncompatibleException("sketch layout mismatch: engine="
                    + engine.config().width + "x" + engine.config().depth
                    + " incoming=" + snapshot.width + "x" + snapshot.depth);
        }
        if (snapshot.seed != engine.config().seed) {
            throw new SketchIncompatibleException("sketch seed mismatch: engine="
                    + engine.config().seed + " incoming=" + snapshot.seed
                    + " (different seeds hash items to different buckets)");
        }
        engine.mergeSketch(snapshot);
        sendJson(ex, 200, mapOf(
                "engine", engine.id(),
                "merged", true,
                "activeWindowTotal", engine.stats().get("activeWindowTotal"),
                "estimateErrorUpperBound", engine.stats().get("activeErrorUpperBound")));
    }

    private void listWindows(HttpExchange ex, FrequentItemsEngine engine) throws IOException {
        List<WindowResult> all = engine.closedWindows();
        List<Object> summaries = new ArrayList<>();
        for (int i = 0; i < all.size(); i++) {
            WindowResult w = all.get(i);
            summaries.add(mapOf(
                    "windowIndex", i,
                    "windowStartMillis", w.windowStartMillis,
                    "windowEndMillis", w.windowEndMillis,
                    "totalCount", w.totalCount,
                    "distinctCandidates", w.distinctCandidates));
        }
        sendJson(ex, 200, mapOf("engine", engine.id(), "windows", summaries));
    }

    private void getWindow(HttpExchange ex, FrequentItemsEngine engine, String indexText) throws IOException {
        int idx;
        try {
            idx = Integer.parseInt(indexText);
        } catch (NumberFormatException nfe) {
            throw new IllegalArgumentException("window index must be an integer");
        }
        List<WindowResult> all = engine.closedWindows();
        if (idx < 0 || idx >= all.size()) {
            sendError(ex, 404, "window_not_found",
                    "window " + idx + " (have " + all.size() + " closed windows)");
            return;
        }
        boolean includeSketch = "true".equalsIgnoreCase(firstQueryParam(ex, "sketch"));
        sendJson(ex, 200, windowToMap(all.get(idx), includeSketch));
    }

    private void advanceTime(HttpExchange ex) throws IOException {
        if (!manualTime) {
            sendError(ex, 403, "manual_time_disabled",
                    "server was started with real system time; virtual clock unavailable");
            return;
        }
        Map<String, Object> body = readJsonBody(ex);
        long millis = asLong(body, "millis", 0L);
        if (millis < 0) {
            throw new IllegalArgumentException("millis must be >= 0");
        }
        // The concrete scheduler for manual mode is a ManualEnvironment; drive it via reflection-free
        // interface: we accept a small functional hook supplied at construction instead.
        if (timeAdvancer == null) {
            sendError(ex, 500, "manual_time_disabled", "no time advancer wired");
            return;
        }
        timeAdvancer.accept(millis);
        sendJson(ex, 200, mapOf("nowMillis", clock.nowMillis()));
    }

    private java.util.function.LongConsumer timeAdvancer;

    /** Wire the virtual-clock advancer (only meaningful in manual-time mode). */
    public void setTimeAdvancer(java.util.function.LongConsumer advancer) {
        this.timeAdvancer = advancer;
    }

    // ---------------------------------------------------------------- helpers

    private Map<String, Object> windowToMap(WindowResult w, boolean includeSketch) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("windowStartMillis", w.windowStartMillis);
        m.put("windowEndMillis", w.windowEndMillis);
        m.put("totalCount", w.totalCount);
        m.put("distinctCandidates", w.distinctCandidates);
        m.put("estimateErrorUpperBound", w.errorUpperBound);
        m.put("candidateTopK", entriesToMaps(w.candidateTopK));
        if (w.exactTopK != null) {
            m.put("exactTopK", entriesToMaps(w.exactTopK));
        }
        m.put("coverageGuarantee",
                "candidateTopK is not guaranteed to cover the true top-K; use exactTopK/trackExact for ground truth.");
        if (includeSketch) {
            m.put("sketch", w.sketch.toMap());
        }
        return m;
    }

    private static List<Object> entriesToMaps(List<Map.Entry<String, Long>> entries) {
        List<Object> out = new ArrayList<>();
        for (Map.Entry<String, Long> e : entries) {
            out.add(mapOf("item", e.getKey(), "estimate", e.getValue()));
        }
        return out;
    }

    private Map<String, Object> configMap(String id, EngineConfig c) {
        return mapOf(
                "id", id,
                "width", c.width,
                "depth", c.depth,
                "seed", c.seed,
                "candidateCapacity", c.candidateCapacity,
                "windowMillis", c.windowMillis,
                "trackExact", c.trackExact);
    }

    private static Map<String, Object> mapOf(Object... kv) {
        if ((kv.length & 1) != 0) {
            throw new IllegalArgumentException("mapOf needs key/value pairs");
        }
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    private static Map<String, Object> asObject(Object o) {
        if (!(o instanceof Map)) {
            throw new IllegalArgumentException("request body must be a JSON object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) o;
        return m;
    }

    private static String asString(Map<String, Object> m, String key, String dflt) {
        Object v = m.get(key);
        if (v == null) {
            return dflt;
        }
        if (!(v instanceof String)) {
            throw new IllegalArgumentException(key + " must be a string");
        }
        return (String) v;
    }

    private static int asInt(Map<String, Object> m, String key, int dflt) {
        Object v = m.get(key);
        if (v == null) {
            return dflt;
        }
        if (!(v instanceof Number)) {
            throw new IllegalArgumentException(key + " must be an integer");
        }
        return ((Number) v).intValue();
    }

    private static long asLong(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (!(v instanceof Number)) {
            throw new IllegalArgumentException(key + " must be an integer");
        }
        return ((Number) v).longValue();
    }

    private static long asLong(Map<String, Object> m, String key, long dflt) {
        Object v = m.get(key);
        return v == null ? dflt : asLong(m, key);
    }

    private static double asDouble(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (!(v instanceof Number)) {
            throw new IllegalArgumentException(key + " must be a number");
        }
        return ((Number) v).doubleValue();
    }

    private static double asDouble(Map<String, Object> m, String key, double dflt) {
        Object v = m.get(key);
        return v == null ? dflt : asDouble(m, key);
    }

    private static boolean asBool(Map<String, Object> m, String key, boolean dflt) {
        Object v = m.get(key);
        if (v == null) {
            return dflt;
        }
        if (!(v instanceof Boolean)) {
            throw new IllegalArgumentException(key + " must be a boolean");
        }
        return (Boolean) v;
    }

    private static int queryParamInt(HttpExchange ex, String key, int dflt) {
        String raw = ex.getRequestURI().getQuery();
        if (raw == null) {
            return dflt;
        }
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String k = eq < 0 ? pair : pair.substring(0, eq);
            if (k.equals(key)) {
                try {
                    return Integer.parseInt(eq < 0 ? "" : pair.substring(eq + 1));
                } catch (NumberFormatException nfe) {
                    throw new IllegalArgumentException("query parameter " + key + " must be an integer");
                }
            }
        }
        return dflt;
    }

    private static String firstQueryParam(HttpExchange ex, String key) {
        String raw = ex.getRequestURI().getQuery();
        if (raw == null) {
            return null;
        }
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String k = eq < 0 ? pair : pair.substring(0, eq);
            if (k.equals(key)) {
                return eq < 0 ? "" : pair.substring(eq + 1);
            }
        }
        return null;
    }

    /** Read the request body and parse it as a JSON object. */
    @SuppressWarnings("unchecked")
    private static Map<String, Object> readJsonBody(HttpExchange ex) throws IOException {
        Object parsed = Json.parse(readBody(ex));
        if (!(parsed instanceof Map)) {
            throw new IllegalArgumentException("request body must be a JSON object");
        }
        return (Map<String, Object>) parsed;
    }

    private static String readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private static void sendJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(payload);
        }
    }

    private static void sendError(HttpExchange ex, int status, String code, String message) throws IOException {
        sendJson(ex, status, mapOf("error", code, "message", message, "status", status));
    }
}
