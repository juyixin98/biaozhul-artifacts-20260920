package com.example.vic.http;

import com.example.vic.domain.Interval;
import com.example.vic.domain.RuleDef;
import com.example.vic.domain.Segment;
import com.example.vic.domain.VersionDef;
import com.example.vic.engine.CoverageEngine;
import com.example.vic.engine.MetaInfo;
import com.example.vic.store.ConflictException;
import com.example.vic.store.NotFoundException;
import com.example.vic.store.RuleStore;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.atomic.AtomicReference;

/**
 * JSON-over-HTTP backend for the version-interval coverage engine.
 * State is held in an immutable {@link RuleStore} inside an AtomicReference;
 * every write swaps in a new store (compare-and-set), never mutating in place.
 */
public class ApiServer {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final AtomicReference<RuleStore> store = new AtomicReference<>(RuleStore.empty());
    private final HttpServer server;

    public ApiServer(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        register("/api/meta", this::handleMeta);
        register("/api/state", this::handleState);
        register("/api/versions", this::handleVersions);
        register("/api/rules", this::handleRules);
        register("/api/compute", this::handleCompute);
        register("/api/query", this::handleQuery);
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

    // ---- handlers -------------------------------------------------------

    private void handleMeta(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }
        respond(exchange, 200, MAPPER.createObjectNode()
                .put("tzdbVersion", MetaInfo.tzdbVersion())
                .put("javaVersion", MetaInfo.javaVersion())
                .put("axis", "long (half-open intervals [start, end))"));
    }

    private void handleState(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "GET")) {
            return;
        }
        respond(exchange, 200, MAPPER.valueToTree(store.get()));
    }

    private void handleVersions(HttpExchange exchange) throws IOException {
        String path = exchange.getRequestURI().getPath();
        String suffix = path.substring("/api/versions".length());
        if (suffix.isEmpty() && "PUT".equals(exchange.getRequestMethod())) {
            JsonNode body = readBody(exchange);
            VersionDef version = new VersionDef(requiredText(body, "id"), requiredInt(body, "priority"));
            store.getAndUpdate(s -> s.withVersion(version));
            respond(exchange, 200, MAPPER.valueToTree(version));
            return;
        }
        if (suffix.startsWith("/") && "DELETE".equals(exchange.getRequestMethod())) {
            String versionId = suffix.substring(1);
            updateOrThrow(s -> s.withoutVersion(versionId));
            respond(exchange, 200, MAPPER.createObjectNode().put("deleted", versionId));
            return;
        }
        respondError(exchange, 405, "unsupported operation on /api/versions");
    }

    private void handleRules(HttpExchange exchange) throws IOException {
        String path = exchange.getRequestURI().getPath();
        String suffix = path.substring("/api/rules".length());
        if (suffix.isEmpty() && "PUT".equals(exchange.getRequestMethod())) {
            JsonNode body = readBody(exchange);
            RuleDef rule = new RuleDef(
                    requiredText(body, "id"),
                    requiredText(body, "versionId"),
                    new Interval(requiredLong(body, "start"), requiredLong(body, "end")),
                    body.hasNonNull("label") ? body.get("label").asText() : null);
            updateOrThrow(s -> s.withRule(rule));
            respond(exchange, 200, MAPPER.valueToTree(rule));
            return;
        }
        if (suffix.startsWith("/") && "DELETE".equals(exchange.getRequestMethod())) {
            String ruleId = suffix.substring(1);
            updateOrThrow(s -> s.withoutRule(ruleId));
            respond(exchange, 200, MAPPER.createObjectNode().put("deleted", ruleId));
            return;
        }
        respondError(exchange, 405, "unsupported operation on /api/rules");
    }

    private void handleCompute(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }
        List<Segment> segments = CoverageEngine.compute(store.get());
        ObjectNode data = MAPPER.createObjectNode();
        data.putPOJO("segments", segments);
        respond(exchange, 200, data);
    }

    private void handleQuery(HttpExchange exchange) throws IOException {
        if (!requireMethod(exchange, "POST")) {
            return;
        }
        JsonNode body = readBody(exchange);
        long point = requiredLong(body, "point");
        Optional<RuleDef> effective = CoverageEngine.effectiveAt(store.get(), point);
        ObjectNode data = MAPPER.createObjectNode().put("point", point);
        if (effective.isPresent()) {
            RuleDef rule = effective.get();
            ObjectNode node = data.putObject("effective");
            node.put("ruleId", rule.id());
            node.put("versionId", rule.versionId());
            node.put("priority", store.get().version(rule.versionId()).orElseThrow().priority());
            if (rule.label() != null) {
                node.put("label", rule.label());
            }
        } else {
            data.putNull("effective");
        }
        respond(exchange, 200, data);
    }

    // ---- helpers --------------------------------------------------------

    private void updateOrThrow(java.util.function.UnaryOperator<RuleStore> update) {
        AtomicReference<RuntimeException> failure = new AtomicReference<>();
        store.updateAndGet(s -> {
            try {
                return update.apply(s);
            } catch (ConflictException | NotFoundException e) {
                failure.set(e);
                return s;
            }
        });
        if (failure.get() != null) {
            throw failure.get();
        }
    }

    private boolean requireMethod(HttpExchange exchange, String method) throws IOException {
        if (method.equals(exchange.getRequestMethod())) {
            return true;
        }
        respondError(exchange, 405, "method not allowed, expected " + method);
        return false;
    }

    private JsonNode readBody(HttpExchange exchange) throws IOException {
        byte[] raw = exchange.getRequestBody().readAllBytes();
        if (raw.length == 0) {
            return MAPPER.createObjectNode();
        }
        try {
            return MAPPER.readTree(new String(raw, StandardCharsets.UTF_8));
        } catch (JsonProcessingException e) {
            throw new IllegalArgumentException("malformed JSON body: " + e.getOriginalMessage());
        }
    }

    private static String requiredText(JsonNode body, String field) {
        if (!body.hasNonNull(field) || !body.get(field).isTextual()) {
            throw new IllegalArgumentException("missing or invalid field: " + field);
        }
        return body.get(field).asText();
    }

    private static int requiredInt(JsonNode body, String field) {
        if (!body.hasNonNull(field) || !body.get(field).canConvertToInt()) {
            throw new IllegalArgumentException("missing or invalid field: " + field);
        }
        return body.get(field).asInt();
    }

    private static long requiredLong(JsonNode body, String field) {
        if (!body.hasNonNull(field) || !body.get(field).isIntegralNumber()) {
            throw new IllegalArgumentException("missing or invalid field: " + field);
        }
        return body.get(field).asLong();
    }

    private void respond(HttpExchange exchange, int status, JsonNode data) throws IOException {
        ObjectNode envelope = MAPPER.createObjectNode();
        envelope.put("success", status < 400);
        envelope.set("data", data);
        envelope.putNull("error");
        send(exchange, status, envelope);
    }

    private void respondError(HttpExchange exchange, int status, String message) throws IOException {
        ObjectNode envelope = MAPPER.createObjectNode();
        envelope.put("success", false);
        envelope.putNull("data");
        envelope.put("error", message);
        send(exchange, status, envelope);
    }

    private void send(HttpExchange exchange, int status, JsonNode body) throws IOException {
        byte[] raw = MAPPER.writeValueAsBytes(body);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, raw.length);
        exchange.getResponseBody().write(raw);
        exchange.close();
    }

    /** Routes top-level failures to the right HTTP status. */
    void dispatchSafely(HttpExchange exchange, ThrowingHandler handler) throws IOException {
        try {
            handler.handle(exchange);
        } catch (ConflictException e) {
            respondError(exchange, 409, e.getMessage());
        } catch (NotFoundException e) {
            respondError(exchange, 404, e.getMessage());
        } catch (IllegalArgumentException e) {
            respondError(exchange, 400, e.getMessage());
        }
    }

    @FunctionalInterface
    interface ThrowingHandler {
        void handle(HttpExchange exchange) throws IOException;
    }

    /** Entry point wiring: wraps each handler with uniform error mapping. */
    public static ApiServer create(int port) throws IOException {
        return new ApiServer(port);
    }

    // visible for tests
    RuleStore currentStore() {
        return store.get();
    }

    void resetStore() {
        store.set(RuleStore.empty());
    }

    /** Binds a context path to a handler with error mapping applied. */
    void register(String path, ThrowingHandler handler) {
        server.createContext(path, exchange -> dispatchSafely(exchange, handler));
    }

    /** Exposed for Main. */
    public Map<String, Object> describe() {
        return Map.of("port", port(), "endpoints", List.of(
                "GET /api/meta", "GET /api/state",
                "PUT /api/versions", "DELETE /api/versions/{id}",
                "PUT /api/rules", "DELETE /api/rules/{id}",
                "POST /api/compute", "POST /api/query"));
    }
}
