package sessionwindow;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * HTTP facade around {@link SessionAggregator}, using only the JDK-bundled
 * {@code com.sun.net.httpserver.HttpServer}.
 *
 * Routes
 * ------
 * POST /events                 ingest one event (or a JSON array of events)
 * GET  /sessions               current materialized sessions (?key= optional)
 * GET  /changelog              emitted RETRACT/UPSERT messages (?since=seq)
 * GET  /rejected               events rejected by the watermark
 * GET  /watermark              watermark + max observed timestamp
 * GET  /accounting             counts incl. noDoubleCount invariant
 * GET  /state                  full state snapshot
 * GET  /health                 liveness
 */
final class ServerMain {

    private static final Logger LOG = Logger.getLogger(ServerMain.class.getName());

    private final SessionAggregator aggregator;
    private final EventLogStore store;
    private HttpServer server;

    ServerMain(SessionAggregator aggregator, EventLogStore store) {
        this.aggregator = aggregator;
        this.store = store;
    }

    static ServerMain start(int port, long gap, long lateness, Path dataDir) throws IOException {
        EventLogStore store = new EventLogStore(dataDir);
        SessionAggregator agg = new SessionAggregator(gap, lateness);

        // Recovery: replay persisted accepted events so session ids, versions
        // and the watermark are rebuilt deterministically.
        List<SessionAggregator.Event> recovered = EventLogStore.readAll(dataDir);
        for (SessionAggregator.Event e : recovered) {
            agg.ingest(e.eventId(), e.key(), e.timestamp());
        }

        ServerMain app = new ServerMain(agg, store);
        HttpServer http = HttpServer.create(new InetSocketAddress(port), 0);
        http.createContext("/", app::route);
        // Single worker thread: ingest order == socket-handling order, so the
        // demo is fully deterministic. Synchronization on the aggregator keeps
        // it safe even if this is later changed.
        http.setExecutor(Executors.newSingleThreadExecutor());
        http.start();
        app.server = http;
        LOG.info(() -> "Session-window server on http://localhost:" + http.getAddress().getPort()
                + " (gap=" + gap + "ms, allowedLateness=" + lateness + "ms,"
                + " recovered " + recovered.size() + " events)");
        return app;
    }

    void stop() {
        if (server != null) {
            server.stop(0);
        }
        store.close();
    }

    /** Actual listening port (useful when the server was bound to port 0). */
    int port() {
        return server.getAddress().getPort();
    }

    // ------------------------------------------------------------------
    // Routing
    // ------------------------------------------------------------------

    private void route(HttpExchange ex) {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (path) {
                case "/health" -> requireGet(ex, method, this::handleHealth);
                case "/events" -> {
                    if ("POST".equals(method)) {
                        handlePostEvents(ex);
                    } else {
                        sendError(ex, 405, "Use POST /events");
                    }
                }
                case "/sessions" -> requireGet(ex, method, this::handleSessions);
                case "/changelog" -> requireGet(ex, method, this::handleChangelog);
                case "/rejected" -> requireGet(ex, method, this::handleRejected);
                case "/watermark" -> requireGet(ex, method, this::handleWatermark);
                case "/accounting" -> requireGet(ex, method, this::handleAccounting);
                case "/state" -> requireGet(ex, method, this::handleState);
                default -> sendError(ex, 404, "Not found: " + path
                        + ". See POST /events, GET /sessions|/changelog|/rejected"
                        + "|/watermark|/accounting|/state|/health");
            }
        } catch (Exception e) {
            LOG.log(Level.WARNING, "Handler failure", e);
            sendError(ex, 500, "Internal error: " + e.getMessage());
        }
    }

    private void requireGet(HttpExchange ex, String method, java.util.function.Consumer<HttpExchange> h) {
        if (!"GET".equals(method)) {
            sendError(ex, 405, "Use GET");
            return;
        }
        h.accept(ex);
    }

    // ------------------------------------------------------------------
    // Handlers
    // ------------------------------------------------------------------

    private void handleHealth(HttpExchange ex) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("status", "ok");
        sendJson(ex, 200, m);
    }

    @SuppressWarnings("unchecked")
    private void handlePostEvents(HttpExchange ex) throws IOException {
        String body;
        try (InputStream in = ex.getRequestBody()) {
            body = new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
        Object parsed;
        try {
            parsed = Json.parse(body);
        } catch (IllegalArgumentException e) {
            sendError(ex, 400, "Invalid JSON: " + e.getMessage());
            return;
        }

        List<Map<String, Object>> items = new ArrayList<>();
        if (parsed instanceof Map<?, ?> single) {
            items.add((Map<String, Object>) single);
        } else if (parsed instanceof List<?> list) {
            for (Object o : list) {
                if (!(o instanceof Map<?, ?>)) {
                    sendError(ex, 400, "Array items must be JSON objects");
                    return;
                }
                items.add((Map<String, Object>) o);
            }
        } else {
            sendError(ex, 400, "Body must be an object or an array of objects");
            return;
        }

        List<Map<String, Object>> results = new ArrayList<>();
        int httpStatus = 200;
        for (Map<String, Object> item : items) {
            IngestOutcome outcome = ingestOne(item);
            if (outcome.status() > httpStatus) {
                httpStatus = outcome.status();
            }
            results.add(outcome.body());
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("results", results);
        if (results.size() == 1) {
            sendJson(ex, httpStatus, results.get(0));
        } else {
            resp.put("count", results.size());
            sendJson(ex, httpStatus, resp);
        }
    }

    private record IngestOutcome(int status, Map<String, Object> body) {
    }

    private IngestOutcome ingestOne(Map<String, Object> item) {
        String eventId;
        String key;
        long ts;
        try {
            eventId = Json.requireString(item, "eventId");
            key = Json.requireString(item, "key");
            ts = Json.requireLong(item, "timestamp");
        } catch (IllegalArgumentException e) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("accepted", false);
            err.put("reason", e.getMessage());
            return new IngestOutcome(400, err);
        }

        SessionAggregator.IngestResult r;
        try {
            r = aggregator.ingest(eventId, key, ts);
        } catch (SessionAggregator.DuplicateIdException e) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("eventId", eventId);
            err.put("accepted", false);
            err.put("reason", e.getMessage());
            return new IngestOutcome(409, err);
        } catch (RuntimeException e) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("accepted", false);
            err.put("eventId", eventId);
            err.put("reason", e.getMessage());
            return new IngestOutcome(400, err);
        }

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("eventId", eventId);
        resp.put("key", key);
        resp.put("timestamp", ts);
        resp.put("watermark", r.watermark() == Long.MIN_VALUE ? null : r.watermark());

        if (!r.accepted()) {
            resp.put("accepted", false);
            resp.put("rejected", true);
            resp.put("reason", "timestamp " + ts + " is below watermark " + r.watermark());
            return new IngestOutcome(422, resp);
        }

        if (r.duplicate()) {
            // Idempotent replay: nothing changed and nothing is appended again.
            resp.put("accepted", true);
            resp.put("duplicate", true);
            resp.put("durable", true);
            resp.put("changes", List.of());
            return new IngestOutcome(200, resp);
        }

        try {
            store.append(eventId, key, ts);
        } catch (IOException e) {
            // Event is in memory but not durable: report honestly rather than
            // silently accepting data loss.
            LOG.log(Level.SEVERE, "Failed to persist event " + eventId, e);
            resp.put("accepted", true);
            resp.put("durable", false);
            resp.put("reason", "aggregated but persistence failed: " + e.getMessage());
            return new IngestOutcome(500, resp);
        }

        resp.put("accepted", true);
        resp.put("durable", true);
        List<Map<String, Object>> changes = new ArrayList<>();
        for (SessionAggregator.Change c : r.changes()) {
            changes.add(c.toMap());
        }
        resp.put("changes", changes);
        return new IngestOutcome(200, resp);
    }

    private void handleSessions(HttpExchange ex) {
        String keyFilter = queryParam(ex, "key");
        List<Map<String, Object>> list = new ArrayList<>();
        for (SessionAggregator.Session s : aggregator.getSessions()) {
            if (keyFilter == null || keyFilter.equals(s.key)) {
                list.add(s.toMap());
            }
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("sessions", list);
        resp.put("count", list.size());
        sendJson(ex, 200, resp);
    }

    private void handleChangelog(HttpExchange ex) {
        Long since = null;
        String sinceRaw = queryParam(ex, "since");
        if (sinceRaw != null) {
            try {
                since = Long.parseLong(sinceRaw);
            } catch (NumberFormatException e) {
                sendError(ex, 400, "?since must be an integer changelog seq");
                return;
            }
        }
        List<Map<String, Object>> list = new ArrayList<>();
        for (SessionAggregator.Change c : aggregator.getChangelog()) {
            if (since == null || c.seq() > since) {
                list.add(c.toMap());
            }
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("changes", list);
        resp.put("count", list.size());
        sendJson(ex, 200, resp);
    }

    private void handleRejected(HttpExchange ex) {
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("rejected", aggregator.getRejected());
        resp.put("count", aggregator.getRejectedCount());
        sendJson(ex, 200, resp);
    }

    private void handleWatermark(HttpExchange ex) {
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.putAll(aggregator.stateMap());
        sendJson(ex, 200, resp);
    }

    private void handleAccounting(HttpExchange ex) {
        sendJson(ex, 200, aggregator.accountingMap());
    }

    private void handleState(HttpExchange ex) {
        Map<String, Object> resp = aggregator.stateMap();
        List<Map<String, Object>> sessions = new ArrayList<>();
        for (SessionAggregator.Session s : aggregator.getSessions()) {
            sessions.add(s.toMap());
        }
        resp.put("sessions", sessions);
        sendJson(ex, 200, resp);
    }

    // ------------------------------------------------------------------
    // HTTP plumbing
    // ------------------------------------------------------------------

    private static String queryParam(HttpExchange ex, String name) {
        String q = ex.getRequestURI().getRawQuery();
        if (q == null) {
            return null;
        }
        for (String pair : q.split("&")) {
            int i = pair.indexOf('=');
            String k = i < 0 ? pair : pair.substring(0, i);
            if (k.equals(name)) {
                return i < 0 ? "" : urlDecode(pair.substring(i + 1));
            }
        }
        return null;
    }

    private static String urlDecode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private static void sendJson(HttpExchange ex, int status, Object body) {
        byte[] payload = Json.writePretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        try {
            ex.sendResponseHeaders(status, payload.length);
            try (OutputStream out = ex.getResponseBody()) {
                out.write(payload);
            }
        } catch (IOException e) {
            LOG.log(Level.FINE, "Client gone while sending response", e);
        }
    }

    private static void sendError(HttpExchange ex, int status, String message) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("error", true);
        m.put("status", status);
        m.put("message", message);
        sendJson(ex, status, m);
    }

    // ------------------------------------------------------------------
    // Entry point
    // ------------------------------------------------------------------

    public static void main(String[] args) {
        int port = intProp("PORT", "server.port", 8080);
        long gap = longProp("GAP_MS", "gap.ms", 10);
        long lateness = longProp("ALLOWED_LATENESS_MS", "allowed.lateness.ms", 10);
        String dataDir = System.getProperty("data.dir",
                envOr("DATA_DIR", "data"));

        try {
            ServerMain app = start(port, gap, lateness, Path.of(dataDir));
            Runtime.getRuntime().addShutdownHook(new Thread(app::stop, "shutdown"));
        } catch (IOException e) {
            LOG.log(Level.SEVERE, "Failed to start server", e);
            System.exit(1);
        }
    }

    private static int intProp(String env, String prop, int def) {
        String raw = System.getProperty(prop, envOr(env, null));
        return raw == null ? def : Integer.parseInt(raw);
    }

    private static long longProp(String env, String prop, long def) {
        String raw = System.getProperty(prop, envOr(env, null));
        return raw == null ? def : Long.parseLong(raw);
    }

    private static String envOr(String name, String def) {
        String v = System.getenv(name);
        return v == null || v.isEmpty() ? def : v;
    }
}
