package com.example.tjoin.server;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;

/**
 * Dependency-free JSON HTTP service for the two-stream interval join.
 *
 * <p>Uses the JDK built-in {@link HttpServer}; the only runtime dependency
 * is Jackson for JSON. No external message system is involved — events are
 * pushed in over HTTP and processing time is manually injectable.</p>
 *
 * <p>Endpoints (see README and examples/requests.http):</p>
 * <pre>
 *   POST   /api/v1/jobs
 *   GET    /api/v1/jobs
 *   GET    /api/v1/jobs/{id}/status
 *   DELETE /api/v1/jobs/{id}
 *   POST   /api/v1/jobs/{id}/events/{left|right}
 *   POST   /api/v1/jobs/{id}/watermark/{left|right}
 *   POST   /api/v1/jobs/{id}/time
 *   GET    /health
 * </pre>
 */
public final class Main {

    private static final int DEFAULT_PORT = 8080;
    private static final int BACKLOG = 0;

    private final HttpServer server;
    private final JobRegistry registry;

    public Main(int port) throws IOException {
        this.registry = new JobRegistry();
        ObjectMapper mapper = new ObjectMapper();

        HttpServer httpServer = HttpServer.create(new InetSocketAddress(port), BACKLOG);
        JoinHandlers handlers = new JoinHandlers(mapper, registry);

        // Context prefix is /api/v1/ (NOT /api/v1/jobs): JDK HttpServer
        // matches contexts by longest literal path prefix, and a job id
        // such as "j-stall2" would otherwise fork at "/api/v1/" and miss a
        // "/api/v1/jobs" context entirely.
        httpServer.createContext("/api/v1/", exchange -> {
            route(exchange, handlers);
        });
        httpServer.createContext("/health", Main::health);
        httpServer.setExecutor(Executors.newFixedThreadPool(8));
        this.server = httpServer;
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
        if (server.getExecutor() instanceof ThreadPoolExecutor tpe) {
            tpe.shutdownNow();
        }
    }

    public int port() {
        return server.getAddress().getPort();
    }

    /**
     * Route a request whose path starts with /api/v1/jobs. The
     * {@code HttpServer} context API does not do method/path templating, so
     * routing is explicit here. Job ids are opaque and may contain dots or
     * other characters, so matching is positional/prefix based rather than
     * relying on split-array lengths.
     */
    private static void route(HttpExchange exchange, JoinHandlers h) throws IOException {
        String path = exchange.getRequestURI().getPath();
        String method = exchange.getRequestMethod();
        String prefix = "/api/v1/jobs";
        if (!path.startsWith(prefix)) {
            plainError(exchange, 404, "no such route: " + path);
            return;
        }
        String rest = path.substring(prefix.length());
        boolean root = rest.isEmpty() || rest.equals("/");
        // rest is "" | "/" | "/{jobId}" | "/{jobId}/status"
        //      | "/{jobId}/events/{side}" | "/{jobId}/watermark/{side}"
        //      | "/{jobId}/time"
        String[] seg = rest.split("/");
        // root: [""] ; otherwise [ "", jobId, action, side? ]
        try {
            if (root) {
                if ("POST".equals(method)) {
                    h.createJob().handle(exchange);
                } else if ("GET".equals(method)) {
                    h.listJobs().handle(exchange);
                } else {
                    plainError(exchange, 405, "method not allowed");
                }
                return;
            }
            if (seg.length == 2 && "DELETE".equals(method)) {
                h.deleteJob().handle(exchange);
                return;
            }
            if (seg.length >= 3) {
                switch (seg[2]) {
                    case "status" -> h.jobStatus().handle(exchange);
                    case "time" -> h.advanceTime().handle(exchange);
                    case "events" -> h.pushEvents().handle(exchange);
                    case "watermark" -> h.pushWatermark().handle(exchange);
                    default -> plainError(exchange, 404, "no such route: " + path);
                }
                return;
            }
            plainError(exchange, 404, "no such route: " + path);
        } catch (RuntimeException e) {
            plainError(exchange, 500, "internal error: " + e.getMessage());
        }
    }

    private static void health(HttpExchange exchange) throws IOException {
        byte[] body = "{\"ok\":true,\"service\":\"dual-stream-interval-join\"}"
                .getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(200, body.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(body);
        }
    }

    private static void plainError(HttpExchange exchange, int status, String message)
            throws IOException {
        byte[] body = ("{\"ok\":false,\"error\":\""
                + message.replace("\"", "'") + "\"}").getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, body.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(body);
        }
    }

    public static void main(String[] args) throws IOException {
        int port = DEFAULT_PORT;
        for (int i = 0; i < args.length; i++) {
            if ("--port".equals(args[i]) && i + 1 < args.length) {
                port = Integer.parseInt(args[++i]);
            }
        }
        Main service = new Main(port);
        service.start();
        System.out.println("dual-stream-interval-join service listening on http://localhost:"
                + service.port());
        System.out.println("Endpoints under /api/v1/jobs — see README.md");
        Runtime.getRuntime().addShutdownHook(new Thread(service::stop));
    }
}
