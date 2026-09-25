package approxheavy.server;

import approxheavy.cms.CountMinSketch;
import approxheavy.cms.IncompatibleSketchException;
import approxheavy.core.Clock;
import approxheavy.core.Scheduler;
import approxheavy.json.Json;
import approxheavy.stream.EventStreamProcessor;
import approxheavy.stream.WindowSummary;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * JSON-over-HTTP front end on the JDK's built-in {@link HttpServer} — no web
 * framework, no external messaging system.
 *
 * <table>
 *   <caption>Routes</caption>
 *   <tr><td>GET  /v1/health</td><td>liveness</td></tr>
 *   <tr><td>PUT  /v1/streams/{name}</td><td>create a stream</td></tr>
 *   <tr><td>GET  /v1/streams</td><td>list streams</td></tr>
 *   <tr><td>POST /v1/streams/{name}/events</td><td>ingest one or many events</td></tr>
 *   <tr><td>GET  /v1/streams/{name}/count?key=...&amp;window=current|last</td>
 *       <td>point estimate + declared error bound</td></tr>
 *   <tr><td>GET  /v1/streams/{name}/topk?k=..&amp;window=..</td><td>approximate heavy hitters</td></tr>
 *   <tr><td>POST /v1/streams/{name}/flush</td><td>seal the active window</td></tr>
 *   <tr><td>GET  /v1/streams/{name}/windows</td><td>list sealed windows</td></tr>
 *   <tr><td>POST /v1/merge</td><td>merge two sketch JSON documents</td></tr>
 * </table>
 */
public final class ApiServer implements AutoCloseable {
    private final HttpServer server;
    private final StreamRegistry registry;
    private final Clock clock;
    private final java.util.concurrent.ExecutorService executor;

    public ApiServer(int port, StreamRegistry registry, Clock clock) throws IOException {
        this.registry = registry;
        this.clock = clock;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", this::route);
        this.executor = java.util.concurrent.Executors.newFixedThreadPool(8);
        server.setExecutor(executor);
    }

    public void start() {
        server.start();
    }

    public int port() {
        return server.getAddress().getPort();
    }

    @Override
    public void close() {
        server.stop(0);
        executor.shutdownNow();
    }

    // ---------------------------------------------------------------- routing

    private void route(HttpExchange exchange) {
        try {
            String path = exchange.getRequestURI().getPath();
            String method = exchange.getRequestMethod();
            String[] parts = path.split("/");
            // parts: ["", "v1", ...]

            if ("GET".equals(method) && path.equals("/v1/health")) {
                respond(exchange, 200, mapOf("status", "ok", "timeMillis", clock.millis()));
                return;
            }

            if ("GET".equals(method) && path.equals("/v1/streams")) {
                listStreams(exchange);
                return;
            }

            if ("PUT".equals(method) && parts.length == 4 && parts[1].equals("v1")
                    && parts[2].equals("streams")) {
                createStream(exchange, parts[3]);
                return;
            }

            if (parts.length == 5 && parts[1].equals("v1") && parts[2].equals("streams")) {
                String name = parts[3];
                String action = parts[4];
                switch (action) {
                    case "events":
                        if ("POST".equals(method)) {
                            ingest(exchange, name);
                        }
                        break;
                    case "count":
                        if ("GET".equals(method)) {
                            pointQuery(exchange, name);
                        }
                        break;
                    case "topk":
                        if ("GET".equals(method)) {
                            topK(exchange, name);
                        }
                        break;
                    case "flush":
                        if ("POST".equals(method)) {
                            flush(exchange, name);
                        }
                        break;
                    case "windows":
                        if ("GET".equals(method)) {
                            windows(exchange, name);
                        }
                        break;
                    default:
                        break;
                }
            }

            if ("POST".equals(method) && path.equals("/v1/merge")) {
                merge(exchange);
                return;
            }

            respond(exchange, 404, mapOf("error", "no such route", "path", path));
        } catch (NotFoundException e) {
            respond(exchange, 404, mapOf("error", e.getMessage()));
        } catch (ConflictException e) {
            respond(exchange, 409, mapOf("error", e.getMessage()));
        } catch (IllegalArgumentException | JsonRuntimeException e) {
            respond(exchange, 400, mapOf("error", e.getMessage()));
        } catch (Exception e) {
            respond(exchange, 500, mapOf("error", "internal error: " + e.getMessage()));
        }
    }

    // --------------------------------------------------------------- handlers

    private void createStream(HttpExchange exchange, String name) throws IOException {
        Map<String, Object> body = readJsonObject(exchange);
        StreamRegistry.Config config = new StreamRegistry.Config();
        config.width = Json.optInt(body, "width", config.width);
        config.depth = Json.optInt(body, "depth", config.depth);
        config.seed = Json.optLng(body, "seed", config.seed);
        config.candidateCapacity = Json.optInt(body, "candidateCapacity", config.candidateCapacity);
        config.windowMillis = Json.optLng(body, "windowMillis", config.windowMillis);
        config.retainedWindows = Json.optInt(body, "retainedWindows", config.retainedWindows);

        EventStreamProcessor processor;
        try {
            processor = registry.create(name, config);
        } catch (IllegalArgumentException e) {
            throw new ConflictException(e.getMessage());
        }
        respond(exchange, 201, streamDescriptor(name, processor));
    }

    private void listStreams(HttpExchange exchange) {
        List<Object> list = new ArrayList<>();
        for (Map.Entry<String, EventStreamProcessor> e : registry.all().entrySet()) {
            list.add(streamDescriptor(e.getKey(), e.getValue()));
        }
        respond(exchange, 200, mapOf("streams", list));
    }

    private void ingest(HttpExchange exchange, String name) throws IOException {
        EventStreamProcessor processor = registry.require(name);
        Map<String, Object> body = readJsonObject(exchange);
        long defaultTimestamp = Json.optLng(body, "timestampMillis", clock.millis());
        long accepted = 0;
        long late = 0;

        if (body.containsKey("events")) {
            List<Object> events = Json.list(body.get("events"));
            for (Object item : events) {
                Map<String, Object> event = Json.object(item);
                String key = Json.str(event, "key");
                long count = Json.optLng(event, "count", 1L);
                long ts = Json.optLng(event, "timestampMillis", defaultTimestamp);
                if (processor.ingest(key, count, ts) == EventStreamProcessor.IngestStatus.ACCEPTED) {
                    accepted++;
                } else {
                    late++;
                }
            }
        } else {
            String key = Json.str(body, "key");
            long count = Json.optLng(body, "count", 1L);
            long ts = defaultTimestamp;
            if (processor.ingest(key, count, ts) == EventStreamProcessor.IngestStatus.ACCEPTED) {
                accepted++;
            } else {
                late++;
            }
        }
        respond(exchange, 202, mapOf(
                "accepted", accepted,
                "late", late,
                "windowStart", processor.currentWindowStart()));
    }

    private void pointQuery(HttpExchange exchange, String name) {
        EventStreamProcessor processor = registry.require(name);
        Map<String, String> query = parseQuery(exchange);
        String key = query.get("key");
        if (key == null) {
            throw new IllegalArgumentException("query parameter 'key' is required");
        }
        CountMinSketch sketch = selectSketch(processor, query.get("window"));
        long estimate = sketch.estimate(key);
        respond(exchange, 200, mapOf(
                "key", key,
                "estimatedCount", estimate,
                "totalCount", sketch.totalCount(),
                "errorUpperBound", sketch.errorUpperBound(),
                "guarantee",
                "estimatedCount >= trueCount always; overestimate <= errorUpperBound with probability 1-2^-depth",
                "note",
                "bounded candidates need not contain this key; see /topk for approximate heavy hitters"));
    }

    private void topK(HttpExchange exchange, String name) {
        EventStreamProcessor processor = registry.require(name);
        Map<String, String> query = parseQuery(exchange);
        int k = Integer.parseInt(query.getOrDefault("k", "10"));
        boolean lastWindow = "last".equals(query.get("window"));

        List<Map.Entry<String, Long>> entries;
        CountMinSketch sketch;
        if (lastWindow) {
            WindowSummary summary = processor.lastWindow();
            if (summary == null) {
                throw new NotFoundException("no sealed window yet; ingest and POST /flush first");
            }
            entries = summary.candidates().topK(k);
            sketch = summary.sketch();
        } else {
            entries = processor.currentTopK(k);
            sketch = processor.currentSketch();
        }

        List<Object> items = new ArrayList<>();
        for (Map.Entry<String, Long> e : entries) {
            items.add(mapOf("key", e.getKey(), "estimatedCount", e.getValue()));
        }
        respond(exchange, 200, mapOf(
                "items", items,
                "candidateSetSize", entries.size(),
                "candidateCoverageGuarantee", "none: a true top-K key may have been evicted",
                "totalCount", sketch.totalCount(),
                "errorUpperBound", sketch.errorUpperBound()));
    }

    private void flush(HttpExchange exchange, String name) {
        EventStreamProcessor processor = registry.require(name);
        processor.flush();
        respond(exchange, 200, mapOf(
                "flushed", true,
                "newWindowStart", processor.currentWindowStart(),
                "sealedWindowCount", processor.sealedWindows().size()));
    }

    private void windows(HttpExchange exchange, String name) {
        EventStreamProcessor processor = registry.require(name);
        List<Object> list = new ArrayList<>();
        for (WindowSummary s : processor.sealedWindows()) {
            list.add(mapOf(
                    "startMillis", s.startMillis(),
                    "endMillis", s.endMillis(),
                    "totalCount", s.sketch().totalCount(),
                    "candidateCount", s.candidates().size(),
                    "sketch", sketchDescriptor(s.sketch())));
        }
        respond(exchange, 200, mapOf(
                "windows", list,
                "currentWindowStart", processor.currentWindowStart()));
    }

    /** Merge two arbitrary sketch documents; mismatched seed/width/depth -> 409. */
    private void merge(HttpExchange exchange) throws IOException {
        Map<String, Object> body = readJsonObject(exchange);
        CountMinSketch a;
        CountMinSketch b;
        try {
            a = CountMinSketch.fromJson(Json.write(body.get("a")));
            b = CountMinSketch.fromJson(Json.write(body.get("b")));
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException("invalid sketch document: " + e.getMessage());
        }
        try {
            a.mergeWith(b);
        } catch (IncompatibleSketchException e) {
            throw new ConflictException(e.getMessage());
        }
        respond(exchange, 200, mapOf(
                "merged", Json.parse(a.toJson()),
                "errorUpperBound", a.errorUpperBound()));
    }

    // -------------------------------------------------------------- utilities

    private CountMinSketch selectSketch(EventStreamProcessor processor, String windowSelector) {
        if ("last".equals(windowSelector)) {
            WindowSummary summary = processor.lastWindow();
            if (summary == null) {
                throw new NotFoundException("no sealed window yet; ingest and POST /flush first");
            }
            return summary.sketch();
        }
        return processor.currentSketch();
    }

    private Map<String, Object> streamDescriptor(String name, EventStreamProcessor p) {
        return mapOf(
                "name", name,
                "width", p.sketchWidth(),
                "depth", p.sketchDepth(),
                "seed", p.seed(),
                "candidateCapacity", p.candidateCapacity(),
                "windowMillis", p.windowMillis(),
                "currentWindowStart", p.currentWindowStart(),
                "currentTotalCount", p.currentSketch().totalCount());
    }

    private Map<String, Object> sketchDescriptor(CountMinSketch s) {
        return mapOf(
                "width", s.width(),
                "depth", s.depth(),
                "seed", s.seed(),
                "totalCount", s.totalCount(),
                "errorUpperBound", s.errorUpperBound());
    }

    private static Map<String, String> parseQuery(HttpExchange exchange) {
        Map<String, String> result = new LinkedHashMap<>();
        String raw = exchange.getRequestURI().getRawQuery();
        if (raw == null) {
            return result;
        }
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            if (eq < 0) {
                result.put(urlDecode(pair), "");
            } else {
                result.put(urlDecode(pair.substring(0, eq)), urlDecode(pair.substring(eq + 1)));
            }
        }
        return result;
    }

    private static String urlDecode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private Map<String, Object> readJsonObject(HttpExchange exchange) throws IOException {
        byte[] payload = exchange.getRequestBody().readAllBytes();
        if (payload.length == 0) {
            return new LinkedHashMap<>();
        }
        try {
            return Json.parseObject(new String(payload, StandardCharsets.UTF_8));
        } catch (RuntimeException e) {
            throw new JsonRuntimeException("malformed JSON body: " + e.getMessage());
        }
    }

    private void respond(HttpExchange exchange, int status, Object payload) {
        byte[] bytes = Json.write(payload).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        try {
            exchange.sendResponseHeaders(status, bytes.length);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(bytes);
            }
        } catch (IOException ignored) {
            // Client gone; nothing useful to do.
        }
    }

    private static Map<String, Object> mapOf(Object... kv) {
        Map<String, Object> map = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            map.put((String) kv[i], kv[i + 1]);
        }
        return map;
    }

    /** Marker so malformed JSON maps to 400 rather than 500. */
    private static final class JsonRuntimeException extends RuntimeException {
        JsonRuntimeException(String message) {
            super(message);
        }
    }
}
