package com.example.topk.server;

import com.example.topk.engine.GroupedTopKEngine;
import com.example.topk.engine.QueryResult;
import com.example.topk.json.Json;
import com.example.topk.model.QueryRequest;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * JSON HTTP entry point for the grouped TopK engine.
 *
 * Endpoints:
 *   GET  /health -> {"status":"ok"}
 *   POST /query  -> grouped TopK request JSON, response JSON (see samples/request.json)
 *
 * Invalid requests (e.g. negative k) get HTTP 400 with {"error": "..."}.
 */
public final class Main {

    public static void main(String[] args) throws Exception {
        int port = 8080;
        if (args.length > 0) {
            port = Integer.parseInt(args[0]);
        }
        HttpServer server = createServer(port);
        server.start();
        System.out.println("grouped-topk server listening on http://localhost:"
                + server.getAddress().getPort());
        System.out.println("POST a JSON request to /query (see samples/request.json), Ctrl+C to stop");
    }

    /** Exposed for the automated test suite; bind on 0 for an ephemeral port. */
    public static HttpServer createServer(int port) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/query", Main::handleQuery);
        server.createContext("/health", Main::handleHealth);
        // Daemon threads so embedding the server (e.g. in tests) never blocks JVM shutdown.
        server.setExecutor(Executors.newFixedThreadPool(4, r -> {
            Thread t = new Thread(r, "topk-http");
            t.setDaemon(true);
            return t;
        }));
        return server;
    }

    private static void handleHealth(HttpExchange ex) throws IOException {
        respond(ex, 200, "{\"status\":\"ok\"}");
    }

    private static void handleQuery(HttpExchange ex) throws IOException {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            respond(ex, 405, Json.write(Map.of("error", "use POST")));
            return;
        }
        try {
            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            Map<String, Object> requestJson = Json.parseObject(body);
            QueryRequest request = QueryRequest.fromJson(requestJson);
            QueryResult result = new GroupedTopKEngine().execute(request);
            Map<String, Object> response = new LinkedHashMap<>();
            response.put("k", request.k);
            response.put("order", request.order);
            response.put("shards", request.shards);
            response.putAll(GroupedTopKEngine.resultToJson(result));
            respond(ex, 200, Json.write(response));
        } catch (IllegalArgumentException e) {
            String msg = e.getMessage() == null ? e.toString() : e.getMessage();
            respond(ex, 400, Json.write(Map.of("error", msg)));
        } catch (Exception e) {
            respond(ex, 500, Json.write(Map.of("error", "internal error: " + e)));
        }
    }

    private static void respond(HttpExchange ex, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (var os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }
}
