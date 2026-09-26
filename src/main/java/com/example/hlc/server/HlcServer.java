package com.example.hlc.server;

import com.example.hlc.core.ClockDriftException;
import com.example.hlc.core.HlcTimestamp;
import com.example.hlc.core.HybridLogicalClock;
import com.example.hlc.core.LogicalOverflowException;
import com.example.hlc.meta.TzdbInfo;
import com.example.hlc.persist.HlcStateStore;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.InetSocketAddress;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * JSON-over-HTTP frontend for a {@link HybridLogicalClock}, built on the JDK
 * {@link HttpServer} (no web framework). Endpoints:
 *
 * <ul>
 *   <li>{@code GET  /api/meta}     — runtime metadata incl. tzdb version</li>
 *   <li>{@code GET  /api/now}      — current timestamp, does not advance the clock</li>
 *   <li>{@code POST /api/send}     — local/send event: tick and return a fresh timestamp</li>
 *   <li>{@code POST /api/receive}  — receive event: merge a remote timestamp, return fresh local one</li>
 * </ul>
 *
 * All responses use the envelope {@code {"ok": boolean, "data": ...} } or
 * {@code {"ok": false, "error": "..."}}.
 */
public final class HlcServer {

    private final HybridLogicalClock hlc;
    private final HlcStateStore store;
    private final ObjectMapper mapper;
    private final HttpServer server;

    public HlcServer(HybridLogicalClock hlc, HlcStateStore store, ObjectMapper mapper, int port) {
        this.hlc = hlc;
        this.store = store;
        this.mapper = mapper;
        try {
            this.server = HttpServer.create(new InetSocketAddress(port), 0);
        } catch (IOException e) {
            throw new UncheckedIOException("failed to bind HTTP server on port " + port, e);
        }
        server.createContext("/api/meta", this::handleMeta);
        server.createContext("/api/now", this::handleNow);
        server.createContext("/api/send", this::handleSend);
        server.createContext("/api/receive", this::handleReceive);
        server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
    }

    public int port() {
        return server.getAddress().getPort();
    }

    private void handleMeta(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("nodeId", hlc.nodeId());
        meta.put("javaVersion", System.getProperty("java.version"));
        meta.put("tzdbVersion", TzdbInfo.tzdbVersion());
        meta.put("systemZone", TzdbInfo.systemZone());
        meta.put("maxLogical", hlc.maxLogical());
        meta.put("overflowPolicy", hlc.overflowPolicy().name());
        meta.put("maxDriftMillis", hlc.maxDriftMillis());
        respond(exchange, 200, ok(meta));
    }

    private void handleNow(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }
        respond(exchange, 200, ok(hlc.current()));
    }

    private void handleSend(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }
        try {
            HlcTimestamp ts = hlc.tick();
            store.save(ts);
            respond(exchange, 200, ok(ts));
        } catch (LogicalOverflowException e) {
            respond(exchange, 409, error(e.getMessage()));
        }
    }

    private void handleReceive(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }
        JsonNode body;
        try {
            body = mapper.readTree(exchange.getRequestBody());
        } catch (IOException e) {
            respond(exchange, 400, error("request body is not valid JSON: " + e.getMessage()));
            return;
        }
        JsonNode tsNode = body == null ? null : body.get("timestamp");
        if (tsNode == null || tsNode.isNull()) {
            respond(exchange, 400, error("missing required field 'timestamp'"));
            return;
        }
        HlcTimestamp remote;
        try {
            remote = mapper.treeToValue(tsNode, HlcTimestamp.class);
        } catch (Exception e) {
            respond(exchange, 400, error("field 'timestamp' is not a valid HLC timestamp: " + e.getMessage()));
            return;
        }
        try {
            HlcTimestamp local = hlc.receive(remote);
            store.save(local);
            respond(exchange, 200, ok(local));
        } catch (ClockDriftException e) {
            respond(exchange, 422, error(e.getMessage()));
        } catch (LogicalOverflowException e) {
            respond(exchange, 409, error(e.getMessage()));
        }
    }

    private boolean requireMethod(HttpExchange exchange, String method) throws IOException {
        if (exchange.getRequestMethod().equalsIgnoreCase(method)) {
            return true;
        }
        respond(exchange, 405, error("method " + exchange.getRequestMethod() + " not allowed, use " + method));
        return false;
    }

    private ObjectNode ok(Object data) {
        ObjectNode envelope = mapper.createObjectNode();
        envelope.put("ok", true);
        envelope.set("data", mapper.valueToTree(data));
        return envelope;
    }

    private ObjectNode error(String message) {
        ObjectNode envelope = mapper.createObjectNode();
        envelope.put("ok", false);
        envelope.put("error", message);
        return envelope;
    }

    private void respond(HttpExchange exchange, int status, ObjectNode body) throws IOException {
        byte[] bytes = mapper.writeValueAsBytes(body);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        try (var out = exchange.getResponseBody()) {
            out.write(bytes);
        }
        exchange.close();
    }
}
