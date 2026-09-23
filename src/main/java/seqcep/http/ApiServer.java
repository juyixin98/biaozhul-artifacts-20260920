package seqcep.http;

import seqcep.engine.Event;
import seqcep.engine.Match;
import seqcep.engine.MatchEngine;
import seqcep.json.Json;
import seqcep.json.JsonException;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * HTTP API backed by the JDK's built-in {@link HttpServer}. No third-party libraries.
 *
 * <table>
 *   <caption>Endpoints</caption>
 *   <tr><td>{@code GET  /healthz}</td><td>liveness probe</td></tr>
 *   <tr><td>{@code POST /v1/events}</td><td>ingest one event or a batch (see README)</td></tr>
 *   <tr><td>{@code GET  /v1/matches}</td><td>list matches; optional {@code entity}, {@code limit}, {@code offset}</td></tr>
 *   <tr><td>{@code GET  /v1/state}</td><td>partial-state snapshot for verification</td></tr>
 *   <tr><td>{@code DELETE /v1/state}</td><td>reset engine and rotate the WAL</td></tr>
 * </table>
 */
public final class ApiServer {

    private static final int MAX_BODY_BYTES = 4 * 1024 * 1024;

    private final MatchEngine engine;
    private final HttpServer server;

    public ApiServer(MatchEngine engine, int port) throws IOException {
        this.engine = engine;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        this.server.createContext("/healthz", this::health);
        this.server.createContext("/v1/events", this::events);
        this.server.createContext("/v1/matches", this::matches);
        this.server.createContext("/v1/state", this::state);
        this.server.createContext("/", this::notFound);
        this.server.setExecutor(java.util.concurrent.Executors.newFixedThreadPool(8));
    }

    public void start() {
        server.start();
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public void stop() {
        server.stop(0);
    }

    // ----------------------------------------------------------- handlers

    private void health(HttpExchange ex) throws IOException {
        if (!requireMethod(ex, "GET")) return;
        writeJson(ex, 200, Json.obj("status", "ok", "engineId", engine.engineId()));
    }

    private void events(HttpExchange ex) throws IOException {
        if (!requireMethod(ex, "POST")) return;
        byte[] body;
        try {
            body = readBody(ex);
        } catch (BadRequestException e) {
            writeError(ex, 400, e.getMessage());
            return;
        }

        final List<Map<String, Object>> rawEvents;
        try {
            Object parsed = Json.parse(new String(body, StandardCharsets.UTF_8));
            rawEvents = extractEventObjects(parsed);
        } catch (JsonException e) {
            writeError(ex, 400, "Malformed JSON: " + e.getMessage());
            return;
        }

        if (rawEvents.isEmpty()) {
            writeError(ex, 400, "Provide an event object or a non-empty \"events\" array");
            return;
        }

        List<Event> accepted = new ArrayList<>(rawEvents.size());
        try {
            // Ingestion order = order inside the request/array. Each event is fsynced
            // individually, so a failure mid-batch leaves an explicit error and a
            // recoverable prefix — never a silent partial success.
            for (Map<String, Object> raw : rawEvents) {
                String type = Json.requireString(raw, "type");
                String entityId = Json.requireString(raw, "entity");
                long ts = Json.requireLong(raw, "ts");
                accepted.add(engine.ingest(type, entityId, ts));
            }
        } catch (JsonException e) {
            writeError(ex, 400, e.getMessage());
            return;
        } catch (RuntimeException e) {
            writeError(ex, 500, "Ingest failed: " + e.getMessage());
            return;
        }

        List<Map<String, Object>> ingested = new ArrayList<>(accepted.size());
        for (Event e : accepted) {
            ingested.add(Json.obj("seq", e.seq(), "type", e.type(),
                    "entity", e.entityId(), "ts", e.timestamp()));
        }

        // Report the post-request match totals for every entity touched by the batch.
        Map<String, Long> matchCountByEntity = new LinkedHashMap<>();
        for (Event e : accepted) {
            matchCountByEntity.put(e.entityId(), (long) engine.matchCount(e.entityId()));
        }

        Map<String, Object> resp = Json.obj(
                "accepted", (long) accepted.size(),
                "events", ingested,
                "totalMatches", (long) engine.matchCount(null),
                "matchesByEntity", matchCountByEntity);
        writeJson(ex, 200, resp);
    }

    private void matches(HttpExchange ex) throws IOException {
        if (!requireMethod(ex, "GET")) return;
        Map<String, String> q = parseQuery(ex.getRequestURI());
        String entity = q.get("entity");

        List<Match> all = engine.matches(entity);
        int total = all.size();

        int offset = 0;
        Integer limit = null;
        try {
            if (q.containsKey("offset")) {
                offset = Integer.parseInt(q.get("offset"));
                if (offset < 0) throw new NumberFormatException();
            }
            if (q.containsKey("limit")) {
                limit = Integer.parseInt(q.get("limit"));
                if (limit < 0) throw new NumberFormatException();
            }
        } catch (NumberFormatException e) {
            writeError(ex, 400, "offset/limit must be non-negative integers");
            return;
        }

        // Without an explicit limit the whole result set is returned — never silently
        // truncated. With a limit we surface total + hasMore so the truncation is explicit
        // and navigable.
        int end = limit == null ? total : Math.min(total, offset + limit);
        int safeOffset = Math.min(offset, total);
        List<Match> page = (safeOffset > end)
                ? List.of()
                : all.subList(safeOffset, Math.max(safeOffset, end));

        List<Map<String, Object>> items = new ArrayList<>();
        for (Match m : page) {
            items.add(Json.obj(
                    "entity", m.entityId(),
                    "a", Json.obj("seq", m.seqA(), "ts", m.tsA()),
                    "b", Json.obj("seq", m.seqB(), "ts", m.tsB()),
                    "c", Json.obj("seq", m.seqC(), "ts", m.tsC()),
                    "durationMs", m.tsC() - m.tsA()));
        }

        Map<String, Object> resp = Json.obj(
                "total", (long) total,
                "offset", (long) safeOffset,
                "limit", limit == null ? null : limit.longValue(),
                "returned", (long) items.size(),
                "hasMore", limit != null && end < total,
                "matches", items);
        writeJson(ex, 200, resp);
    }

    private void state(HttpExchange ex) throws IOException {
        String method = ex.getRequestMethod();
        if (method.equals("GET")) {
            writeJson(ex, 200, engine.snapshot());
            return;
        }
        if (method.equals("DELETE")) {
            engine.reset();
            writeJson(ex, 200, Json.obj("status", "reset"));
            return;
        }
        writeError(ex, 405, "Use GET or DELETE on /v1/state");
    }

    private void notFound(HttpExchange ex) throws IOException {
        writeError(ex, 404, "Unknown route: " + ex.getRequestMethod() + " " + ex.getRequestURI().getPath());
    }

    // ----------------------------------------------------------- helpers

    /** Accepts {@code {...one event...}}, {@code {"events":[...]}} or a bare JSON array. */
    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> extractEventObjects(Object parsed) {
        List<Map<String, Object>> out = new ArrayList<>();
        if (parsed instanceof Map<?, ?> map) {
            Object batch = map.get("events");
            if (batch instanceof List<?> list) {
                for (Object o : list) {
                    if (!(o instanceof Map<?, ?>)) {
                        throw new JsonException("Every element of \"events\" must be an object");
                    }
                    out.add((Map<String, Object>) o);
                }
            } else if (batch != null) {
                throw new JsonException("Field \"events\" must be an array");
            } else {
                out.add((Map<String, Object>) map);
            }
        } else if (parsed instanceof List<?> list) {
            for (Object o : list) {
                if (!(o instanceof Map<?, ?>)) {
                    throw new JsonException("Every array element must be an event object");
                }
                out.add((Map<String, Object>) o);
            }
        } else {
            throw new JsonException("Request body must be an event object or an array of events");
        }
        return out;
    }

    private static byte[] readBody(HttpExchange ex) throws IOException, BadRequestException {
        String lenHeader = ex.getRequestHeaders().getFirst("Content-Length");
        if (lenHeader != null) {
            try {
                if (Long.parseLong(lenHeader) > MAX_BODY_BYTES) {
                    throw new BadRequestException("Request body too large (limit " + MAX_BODY_BYTES + " bytes)");
                }
            } catch (NumberFormatException e) {
                throw new BadRequestException("Invalid Content-Length header");
            }
        }
        byte[] bytes = ex.getRequestBody().readNBytes(MAX_BODY_BYTES + 1);
        if (bytes.length > MAX_BODY_BYTES) {
            throw new BadRequestException("Request body too large (limit " + MAX_BODY_BYTES + " bytes)");
        }
        return bytes;
    }

    private boolean requireMethod(HttpExchange ex, String expected) throws IOException {
        if (!ex.getRequestMethod().equals(expected)) {
            writeError(ex, 405, expected + " required");
            return false;
        }
        return true;
    }

    private static Map<String, String> parseQuery(URI uri) {
        Map<String, String> params = new LinkedHashMap<>();
        String raw = uri.getRawQuery();
        if (raw == null || raw.isEmpty()) return params;
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String key = eq < 0 ? pair : pair.substring(0, eq);
            String val = eq < 0 ? "" : pair.substring(eq + 1);
            params.put(urlDecode(key), urlDecode(val));
        }
        return params;
    }

    private static String urlDecode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private static void writeError(HttpExchange ex, int code, String message) throws IOException {
        writeJson(ex, code, Json.obj("error", message, "status", code));
    }

    private static void writeJson(HttpExchange ex, int code, Object value) throws IOException {
        byte[] bytes = Json.pretty(value).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    private static final class BadRequestException extends Exception {
        BadRequestException(String m) { super(m); }
    }
}
