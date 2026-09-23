package orderedevents.web;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;

import orderedevents.json.Json;
import orderedevents.json.JsonException;
import orderedevents.json.JsonWriter;
import orderedevents.model.ResultEntry;
import orderedevents.service.ApiException;
import orderedevents.service.EventService;
import orderedevents.service.ServerConfig;

/**
 * Single router for the API.
 *
 * <pre>
 *  GET    /health
 *  GET    /partitions
 *  POST   /partitions/{p}/events
 *  GET    /partitions/{p}/events/{id}
 *  POST   /partitions/{p}/events/{id}/cancel
 *  GET    /partitions/{p}/results?afterSeq=&waitMillis=
 *  GET    /partitions/{p}/status
 * </pre>
 */
final class ApiHandler implements HttpHandler {

    private static final int MAX_BODY = 256 * 1024;

    private final EventService service;
    private final ServerConfig config;

    ApiHandler(EventService service, ServerConfig config) {
        this.service = service;
        this.config = config;
    }

    @Override
    public void handle(HttpExchange ex) {
        try {
            route(ex);
        } catch (ApiException e) {
            writeError(ex, e.status(), e.getMessage());
        } catch (JsonException e) {
            writeError(ex, 400, "invalid JSON: " + e.getMessage());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            writeError(ex, 500, "interrupted");
        } catch (Exception e) {
            writeError(ex, 500, "internal error: " + e);
        } finally {
            ex.close();
        }
    }

    private void route(HttpExchange ex) throws IOException, InterruptedException {
        String method = ex.getRequestMethod();
        List<String> path = splitPath(ex.getRequestURI().getRawPath());

        if (path.isEmpty()) {
            if ("GET".equals(method)) {
                writeJson(ex, 200, Map.of(
                        "service", "ordered-events",
                        "endpoints", List.of(
                                "GET /health",
                                "GET /partitions",
                                "POST /partitions/{partition}/events",
                                "GET /partitions/{partition}/events/{id}",
                                "POST /partitions/{partition}/events/{id}/cancel",
                                "GET /partitions/{partition}/results?afterSeq=&waitMillis=",
                                "GET /partitions/{partition}/status")));
                return;
            }
            throw ApiException.badRequest("unsupported method at root");
        }

        if (path.size() == 1 && "health".equals(path.get(0))) {
            requireGet(method);
            writeJson(ex, 200, Map.of("status", "ok"));
            return;
        }

        if (path.size() == 1 && "partitions".equals(path.get(0))) {
            requireGet(method);
            writeJson(ex, 200, Map.of("partitions", service.listPartitions()));
            return;
        }

        if (path.size() == 2 && "partitions".equals(path.get(0))) {
            // GET on .../{p} -> alias for status
            requireGet(method);
            writeJson(ex, 200, service.partitionStatus(decode(path.get(1))));
            return;
        }

        if (path.size() == 3 && "partitions".equals(path.get(0)) && "events".equals(path.get(2))) {
            if (!"POST".equals(method)) {
                throw new ApiException(405, "use POST /partitions/{partition}/events with a body");
            }
            String partition = decode(path.get(1));
            Map<String, Object> body = readJsonObject(ex);
            writeJson(ex, 202, service.submit(partition, body));
            return;
        }

        if (path.size() == 3 && "partitions".equals(path.get(0))
                && ("results".equals(path.get(2)) || "status".equals(path.get(2)))) {
            requireGet(method);
            String partition = decode(path.get(1));
            if ("results".equals(path.get(2))) {
                handleResults(ex, partition);
            } else {
                writeJson(ex, 200, service.partitionStatus(partition));
            }
            return;
        }

        if (path.size() == 4 && "partitions".equals(path.get(0)) && "events".equals(path.get(2))) {
            String partition = decode(path.get(1));
            String id = decode(path.get(3));
            requireGet(method);
            writeJson(ex, 200, service.getEvent(partition, id));
            return;
        }

        if (path.size() == 5 && "partitions".equals(path.get(0)) && "events".equals(path.get(2))
                && "cancel".equals(path.get(4))) {
            if (!"POST".equals(method)) {
                throw new ApiException(405, "cancel requires POST");
            }
            String partition = decode(path.get(1));
            String id = decode(path.get(3));
            writeJson(ex, 200, service.cancel(partition, id));
            return;
        }

        writeError(ex, 404, "no such route: " + String.join("/", path));
    }

    private void handleResults(HttpExchange ex, String partition)
            throws InterruptedException, IOException {
        Map<String, String> q = queryParams(ex);
        Long afterSeq = q.containsKey("afterSeq") ? parseLong(q.get("afterSeq"), "afterSeq") : null;
        Long waitForSeq = q.containsKey("waitForSeq") ? parseLong(q.get("waitForSeq"), "waitForSeq") : null;
        Long waitMillis = q.containsKey("waitMillis") ? parseLong(q.get("waitMillis"), "waitMillis") : null;
        List<ResultEntry> entries = service.getResults(partition, afterSeq, waitForSeq, waitMillis);

        List<Map<String, Object>> items = new ArrayList<>();
        long lastSeq = afterSeq == null ? -1L : afterSeq;
        for (ResultEntry e : entries) {
            items.add(e.toMap());
            lastSeq = Math.max(lastSeq, e.seq());
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("partition", partition);
        resp.put("results", items);
        resp.put("count", items.size());
        resp.put("lastSeq", lastSeq);
        writeJson(ex, 200, resp);
    }

    // ------------------------------------------------------------ plumbing

    private static void requireGet(String method) {
        if (!"GET".equals(method)) {
            throw new ApiException(405, "method " + method + " not allowed here");
        }
    }

    private static List<String> splitPath(String raw) {
        List<String> out = new ArrayList<>();
        for (String seg : raw.split("/")) {
            if (!seg.isEmpty()) {
                out.add(seg);
            }
        }
        return out;
    }

    private static String decode(String s) {
        return URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private static Map<String, String> queryParams(HttpExchange ex) {
        Map<String, String> out = new LinkedHashMap<>();
        String q = ex.getRequestURI().getRawQuery();
        if (q == null || q.isEmpty()) {
            return out;
        }
        for (String pair : q.split("&")) {
            int eq = pair.indexOf('=');
            String k = eq < 0 ? pair : pair.substring(0, eq);
            String v = eq < 0 ? "" : pair.substring(eq + 1);
            out.put(decode(k), decode(v.replace('+', ' ')));
        }
        return out;
    }

    private static long parseLong(String v, String field) {
        try {
            return Long.parseLong(v);
        } catch (NumberFormatException e) {
            throw ApiException.badRequest("query parameter '" + field + "' must be an integer");
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> readJsonObject(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            byte[] data = in.readNBytes(MAX_BODY + 1);
            if (data.length > MAX_BODY) {
                throw ApiException.badRequest("request body too large");
            }
            if (data.length == 0) {
                return new LinkedHashMap<>();
            }
            Object parsed = Json.parse(new String(data, StandardCharsets.UTF_8));
            if (!(parsed instanceof Map)) {
                throw ApiException.badRequest("expected a JSON object body");
            }
            return (Map<String, Object>) parsed;
        }
    }

    private void writeJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] data = JsonWriter.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(data);
        }
    }

    private void writeError(HttpExchange ex, int status, String message) {
        try {
            Map<String, Object> body = Map.of("error", message, "status", status);
            writeJson(ex, status, body);
        } catch (IOException ignored) {
            // best effort; exchange is closed by the caller's finally
        }
    }
}
