package com.example.stablepager;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * The JDK {@code com.sun.net.httpserver} HTTP application: routing, request
 * validation and JSON (de)serialization on top of {@link PaginationService} and
 * {@link MvccStore}. No servlet container or third-party library is involved.
 */
public final class WebRuntime implements AutoCloseable {

    private static final int MAX_BODY_BYTES = 64 * 1024;
    private static final long DEFAULT_TTL_MS = 60_000;

    private final HttpServer server;
    private final MvccStore store;
    private final ExecutorService pool;

    WebRuntime(HttpServer server, MvccStore store, ExecutorService pool) {
        this.server = server;
        this.store = store;
        this.pool = pool;
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public MvccStore store() {
        return store;
    }

    @Override
    public void close() {
        server.stop(0);
        pool.shutdownNow();
        try {
            pool.awaitTermination(2, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        store.shutdown();
    }

    /** Starts the service. Pass {@code port == 0} to pick an ephemeral port (used by tests). */
    public static WebRuntime start(int port, long snapshotTtlMillis, String macSecret) throws IOException {
        MvccStore store = new MvccStore(snapshotTtlMillis);
        CursorCodec codec = new CursorCodec(macSecret);
        PaginationService service = new PaginationService(store, codec);
        ItemController controller = new ItemController(store, service);

        HttpServer server = HttpServer.create(new InetSocketAddress("0.0.0.0", port), 0);
        server.createContext("/", exchange -> {
            try {
                dispatch(controller, exchange);
            } catch (ApiException e) {
                writeError(exchange, e);
            } catch (Exception e) {
                writeError(exchange,
                        new ApiException(500, "INTERNAL", "internal error: " + e.getClass().getSimpleName()));
            }
        });
        ExecutorService pool = Executors.newFixedThreadPool(8, r -> {
            Thread t = new Thread(r, "stablepager-http");
            t.setDaemon(true);
            return t;
        });
        server.setExecutor(pool);
        server.start();
        return new WebRuntime(server, store, pool);
    }

    private static void dispatch(ItemController controller, HttpExchange exchange) throws IOException {
        String method = exchange.getRequestMethod();
        String path = exchange.getRequestURI().getPath();

        if ("GET".equals(method) && "/healthz".equals(path)) {
            writeJson(exchange, 200, Map.of("status", "ok"));
            return;
        }
        if ("/api/items".equals(path)) {
            switch (method) {
                case "GET" -> {
                    Map<String, String> q = splitQuery(exchange.getRequestURI().getRawQuery());
                    writeJson(exchange, 200, controller.list(q));
                    return;
                }
                case "POST" -> {
                    Map<String, Object> body = readJsonBody(exchange);
                    writeJson(exchange, 201, controller.create(body));
                    return;
                }
                default -> throw methodNotAllowed(method, "GET, POST");
            }
        }
        if (path.startsWith("/api/items/")) {
            String id = path.substring("/api/items/".length());
            if (id.isEmpty() || id.contains("/")) {
                throw new ApiException(404, "NOT_FOUND", "unknown path " + path);
            }
            switch (method) {
                case "GET" -> {
                    writeJson(exchange, 200, Map.of("item", controller.get(id)));
                    return;
                }
                case "PATCH" -> {
                    Map<String, Object> body = readJsonBody(exchange);
                    writeJson(exchange, 200, Map.of("item", controller.patch(id, body)));
                    return;
                }
                case "DELETE" -> {
                    controller.delete(id);
                    writeJson(exchange, 200, Map.of("deleted", id));
                    return;
                }
                default -> throw methodNotAllowed(method, "GET, PATCH, DELETE");
            }
        }
        throw new ApiException(404, "NOT_FOUND", "unknown path " + path);
    }

    private static ApiException methodNotAllowed(String method, String allowed) {
        return new ApiException(405, "METHOD_NOT_ALLOWED", method + " is not allowed here; supported: " + allowed);
    }

    // ------------------------------------------------------------------
    // HTTP plumbing
    // ------------------------------------------------------------------

    static Map<String, String> splitQuery(String raw) {
        Map<String, String> out = new LinkedHashMap<>();
        if (raw == null || raw.isEmpty()) {
            return out;
        }
        for (String pair : raw.split("&")) {
            if (pair.isEmpty()) {
                continue;
            }
            int eq = pair.indexOf('=');
            String key = eq < 0 ? pair : pair.substring(0, eq);
            String value = eq < 0 ? "" : pair.substring(eq + 1);
            out.put(urlDecode(key), urlDecode(value));
        }
        return out;
    }

    private static String urlDecode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> readJsonBody(HttpExchange exchange) throws IOException {
        try (InputStream in = exchange.getRequestBody()) {
            byte[] data = in.readNBytes(MAX_BODY_BYTES + 1);
            if (data.length > MAX_BODY_BYTES) {
                throw new ApiException(413, "BODY_TOO_LARGE",
                        "request body must not exceed " + MAX_BODY_BYTES + " bytes");
            }
            if (data.length == 0) {
                throw new ApiException(400, "BAD_JSON", "request body is required and must be JSON");
            }
            return Json.parseObject(new String(data, StandardCharsets.UTF_8));
        }
    }

    private static void writeJson(HttpExchange exchange, int status, Object payload) throws IOException {
        byte[] bytes = Json.stringify(payload).getBytes(StandardCharsets.UTF_8);
        Headers headers = exchange.getResponseHeaders();
        headers.set("Content-Type", "application/json; charset=utf-8");
        headers.set("Cache-Control", "no-store");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(bytes);
        }
    }

    private static void writeError(HttpExchange exchange, ApiException error) {
        try {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("error", error.code());
            body.put("message", error.getMessage());
            if (!error.details().isEmpty()) {
                body.put("details", error.details());
            }
            byte[] bytes = Json.stringify(body).getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
            exchange.sendResponseHeaders(error.status(), bytes.length);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(bytes);
            }
        } catch (IOException ignored) {
            exchange.close();
        }
    }

    /** Default TTL exposed for the README/tests. */
    public static long defaultTtlMillis() {
        return DEFAULT_TTL_MS;
    }
}
