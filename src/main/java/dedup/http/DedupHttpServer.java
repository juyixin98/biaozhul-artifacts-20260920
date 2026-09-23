package dedup.http;

import dedup.ApiException;
import dedup.DedupService;
import dedup.ErrorCode;
import dedup.Json;
import dedup.Snapshot;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;

/**
 * HTTP front end built only on the JDK ({@code com.sun.net.httpserver}).
 *
 * Routes (all bodies are JSON):
 *   GET    /health
 *   PUT    /partitions/{key}
 *   GET    /partitions
 *   GET    /partitions/{key}
 *   DELETE /partitions/{key}
 *   POST   /partitions/{key}/events
 *   POST   /partitions/{key}/watermark
 *   POST   /partitions/{key}/migration/export
 *   POST   /partitions/{key}/migration/import
 *   POST   /partitions/{key}/migration/abort
 *   POST   /partitions/{key}/migration/complete
 *
 * The routing version (epoch) may be supplied either as the optional
 * {@code epoch} JSON field or the {@code X-Routing-Epoch} header.
 */
public final class DedupHttpServer {

    private final HttpServer server;
    private final int port;

    public DedupHttpServer(int port, DedupService service) throws IOException {
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        this.port = server.getAddress().getPort();
        ThreadPoolExecutor pool = (ThreadPoolExecutor) Executors.newFixedThreadPool(8, r -> {
            Thread t = new Thread(r, "dedup-http");
            t.setDaemon(true);
            return t;
        });
        server.setExecutor(pool);
        server.createContext("/", new RootHandler(service));
    }

    public int port() {
        return port;
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
    }

    // ------------------------------------------------------------------

    static final class RootHandler implements HttpHandler {
        private final DedupService service;

        RootHandler(DedupService service) {
            this.service = service;
        }

        @Override
        public void handle(HttpExchange ex) {
            try {
                route(ex);
            } catch (ApiException e) {
                sendQuiet(ex, e.code().httpStatus(), errorBody(e.code().name(), e.getMessage()));
            } catch (Json.JsonException e) {
                sendQuiet(ex, 400, errorBody(ErrorCode.INVALID_BODY.name(), e.getMessage()));
            } catch (Exception e) {
                sendQuiet(ex, 500, errorBody(ErrorCode.INTERNAL.name(),
                        e.getClass().getSimpleName() + ": " + e.getMessage()));
            } finally {
                ex.close();
            }
        }

        private void route(HttpExchange ex) throws IOException {
            String method = ex.getRequestMethod();
            String path = ex.getRequestURI().getPath();

            if (path.equals("/health")) {
                requireMethod(method, "GET");
                Map<String, Object> body = new LinkedHashMap<>();
                body.put("status", "ok");
                body.put("service", "event-id-dedup");
                respond(ex, 200, body);
                return;
            }

            if (path.equals("/partitions")) {
                requireMethod(method, "GET");
                Map<String, Object> body = new LinkedHashMap<>();
                body.put("partitions", service.listPartitions());
                respond(ex, 200, body);
                return;
            }

            // /partitions/{key}/migration/{action}
            String[] parts = splitPath(path);
            if (parts.length >= 2 && parts[0].equals("partitions")) {
                String key = parts[1];
                if (parts.length == 2) {
                    switch (method) {
                        case "PUT" -> {
                            Map<String, Object> req = readJson(ex);
                            long epoch = Json.optLong(req, "epoch", 0L);
                            long retention = Json.reqLong(req, "retentionMillis");
                            respond(ex, 201, service.createPartition(key, epoch, retention));
                        }
                        case "GET" -> respond(ex, 200, service.status(key));
                        case "DELETE" -> {
                            Long epoch = optionalEpoch(ex, null);
                            service.deletePartition(key, epoch);
                            respond(ex, 200, ok("partition deleted"));
                        }
                        default -> throw new ApiException(ErrorCode.METHOD_NOT_ALLOWED,
                                "Unsupported method " + method);
                    }
                    return;
                }
                if (parts.length == 3 && parts[2].equals("events")) {
                    requireMethod(method, "POST");
                    Map<String, Object> req = readJson(ex);
                    String eventId = Json.reqString(req, "eventId");
                    long eventTime = Json.reqLong(req, "eventTime");
                    Long epoch = optionalEpoch(ex, req.get("epoch"));
                    respond(ex, 200, service.checkEvent(key, epoch, eventId, eventTime));
                    return;
                }
                if (parts.length == 3 && parts[2].equals("watermark")) {
                    requireMethod(method, "POST");
                    Map<String, Object> req = readJson(ex);
                    long watermark = Json.reqLong(req, "watermark");
                    Long epoch = optionalEpoch(ex, req.get("epoch"));
                    respond(ex, 200, service.advanceWatermark(key, epoch, watermark));
                    return;
                }
                if (parts.length == 4 && parts[2].equals("migration")) {
                    handleMigration(ex, method, key, parts[3]);
                    return;
                }
            }

            throw new ApiException(ErrorCode.NOT_FOUND, "No route for " + method + " " + path);
        }

        private void handleMigration(HttpExchange ex, String method, String key, String action)
                throws IOException {
            requireMethod(method, "POST");
            switch (action) {
                case "export" -> {
                    Map<String, Object> req = readJsonLenient(ex);
                    Long epoch = optionalEpoch(ex, req == null ? null : req.get("epoch"));
                    // service returns {partition, snapshot} already.
                    respond(ex, 200, service.exportSnapshot(key, epoch));
                }
                case "import" -> {
                    Map<String, Object> req = readJson(ex);
                    Snapshot snapshot = DedupService.snapshotFromBody(req);
                    long newEpoch = Json.reqLong(req, "newEpoch");
                    Long epoch = optionalEpoch(ex, req.get("epoch"));
                    respond(ex, 200,
                            service.importSnapshot(key, epoch, snapshot, newEpoch));
                }
                case "abort" -> {
                    Map<String, Object> req = readJsonLenient(ex);
                    Long epoch = optionalEpoch(ex, req == null ? null : req.get("epoch"));
                    respond(ex, 200, service.abortMigration(key, epoch));
                }
                case "complete" -> {
                    Map<String, Object> req = readJson(ex);
                    long epoch = Json.reqLong(req, "epoch");
                    respond(ex, 200, service.completeMigration(key, epoch));
                }
                default -> throw new ApiException(ErrorCode.NOT_FOUND,
                        "Unknown migration action '" + action + "'");
            }
        }
    }

    // ------------------------------------------------------------------
    // HTTP helpers
    // ------------------------------------------------------------------

    private static String[] splitPath(String path) {
        String trimmed = path.startsWith("/") ? path.substring(1) : path;
        return trimmed.isEmpty() ? new String[0] : trimmed.split("/");
    }

    private static void requireMethod(String actual, String expected) {
        if (!actual.equals(expected)) {
            throw new ApiException(ErrorCode.METHOD_NOT_ALLOWED,
                    "Method " + actual + " not allowed; expected " + expected);
        }
    }

    private static Map<String, Object> readJson(HttpExchange ex) throws IOException {
        String text = readBody(ex);
        if (text.isEmpty()) {
            throw new ApiException(ErrorCode.INVALID_BODY, "Request body must be JSON");
        }
        Object parsed;
        try {
            parsed = Json.parse(text);
        } catch (Json.JsonException e) {
            throw new ApiException(ErrorCode.INVALID_BODY, "Malformed JSON: " + e.getMessage());
        }
        if (!(parsed instanceof Map<?, ?>)) {
            throw new ApiException(ErrorCode.INVALID_BODY, "Request body must be a JSON object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> map = (Map<String, Object>) parsed;
        return map;
    }

    /** Like readJson but allows an empty body (returns null). */
    private static Map<String, Object> readJsonLenient(HttpExchange ex) throws IOException {
        String text = readBody(ex);
        if (text.isEmpty()) {
            return null;
        }
        try {
            Object parsed = Json.parse(text);
            if (!(parsed instanceof Map<?, ?>)) {
                throw new ApiException(ErrorCode.INVALID_BODY, "Request body must be a JSON object");
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> map = (Map<String, Object>) parsed;
            return map;
        } catch (Json.JsonException e) {
            throw new ApiException(ErrorCode.INVALID_BODY, "Malformed JSON: " + e.getMessage());
        }
    }

    private static String readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        return new String(bytes, StandardCharsets.UTF_8);
    }

    /**
     * Epoch may arrive via header {@code X-Routing-Epoch} or JSON field
     * {@code epoch}; the header wins when both are present.
     */
    private static Long optionalEpoch(HttpExchange ex, Object jsonEpoch) {
        Headers headers = ex.getRequestHeaders();
        List<String> headerValues = headers.get("X-Routing-Epoch");
        if (headerValues != null && !headerValues.isEmpty()) {
            try {
                return Long.parseLong(headerValues.get(0).trim());
            } catch (NumberFormatException e) {
                throw new ApiException(ErrorCode.INVALID_BODY,
                        "X-Routing-Epoch header must be an integer");
            }
        }
        return jsonEpoch == null ? null : Json.asLong(jsonEpoch, "epoch");
    }

    private static Map<String, Object> errorBody(String code, String message) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", code);
        body.put("message", message);
        return body;
    }

    private static Map<String, Object> ok(String message) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("ok", true);
        body.put("message", message);
        return body;
    }

    static void respond(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = Json.writePretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(payload);
        }
    }

    /** Error-response path: if the client already went away there is nothing more to do. */
    private static void sendQuiet(HttpExchange ex, int status, Object body) {
        try {
            respond(ex, status, body);
        } catch (IOException ignored) {
            // best-effort error response
        }
    }
}
