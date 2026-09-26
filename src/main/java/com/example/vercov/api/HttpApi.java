package com.example.vercov.api;

import com.example.vercov.engine.ConflictException;
import com.example.vercov.meta.TzdbInfo;
import com.example.vercov.model.IntervalRule;
import com.example.vercov.model.Version;
import com.example.vercov.store.VersionStore;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * JSON-over-HTTP front end. Endpoints:
 * <ul>
 *   <li>{@code GET    /api/meta}            — runtime + tzdb info</li>
 *   <li>{@code GET    /api/versions}        — list registered versions</li>
 *   <li>{@code POST   /api/versions}        — register a version</li>
 *   <li>{@code DELETE /api/versions/{id}}   — remove a version (coverage recomputes)</li>
 *   <li>{@code GET    /api/coverage?from=&to=} — effective non-overlapping segments</li>
 *   <li>{@code GET    /api/point?at=}       — effective segment at one point</li>
 * </ul>
 */
public class HttpApi {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private final VersionStore store;
    private final HttpServer server;

    public HttpApi(VersionStore store, int port) throws IOException {
        this.store = store;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/api/meta", ex -> handle(ex, this::meta));
        server.createContext("/api/versions", ex -> handle(ex, this::versions));
        server.createContext("/api/versions/", ex -> handle(ex, this::versionById));
        server.createContext("/api/coverage", ex -> handle(ex, this::coverage));
        server.createContext("/api/point", ex -> handle(ex, this::point));
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

    @FunctionalInterface
    private interface Route {
        JsonNode apply(HttpExchange exchange) throws IOException;
    }

    private void handle(HttpExchange exchange, Route route) throws IOException {
        try {
            JsonNode body = route.apply(exchange);
            respond(exchange, 200, body);
        } catch (BadRequestException e) {
            respond(exchange, 400, error("bad_request", e.getMessage()));
        } catch (ConflictException e) {
            respond(exchange, 409, error("conflict", e.getMessage()));
        } catch (VersionStore.DuplicateVersionException e) {
            respond(exchange, 409, error("duplicate_version", e.getMessage()));
        } catch (NotFoundException e) {
            respond(exchange, 404, error("not_found", e.getMessage()));
        } catch (IllegalArgumentException e) {
            respond(exchange, 400, error("bad_request", e.getMessage()));
        } catch (Exception e) {
            respond(exchange, 500, error("internal", e.getClass().getSimpleName()));
        } finally {
            exchange.close();
        }
    }

    private JsonNode meta(HttpExchange exchange) {
        requireMethod(exchange, "GET");
        ObjectNode node = MAPPER.createObjectNode();
        node.put("service", "version-interval-coverage");
        node.put("axis", "long (integer positions; epoch seconds for time rules)");
        node.put("intervalSemantics", "half-open [start, end)");
        node.put("tzdbVersion", TzdbInfo.tzdbVersion());
        node.put("availableZoneCount", TzdbInfo.availableZoneCount());
        node.put("javaVersion", System.getProperty("java.version"));
        return node;
    }

    private JsonNode versions(HttpExchange exchange) throws IOException {
        return switch (exchange.getRequestMethod()) {
            case "GET" -> {
                ArrayNode array = MAPPER.createArrayNode();
                for (Version version : store.list()) {
                    array.add(toJson(version));
                }
                yield array;
            }
            case "POST" -> toJson(store.add(parseVersion(readBody(exchange))));
            default -> throw new BadRequestException("unsupported method " + exchange.getRequestMethod());
        };
    }

    private JsonNode versionById(HttpExchange exchange) {
        requireMethod(exchange, "DELETE");
        String id = exchange.getRequestURI().getPath().substring("/api/versions/".length());
        if (id.isBlank() || id.contains("/")) {
            throw new BadRequestException("invalid version id in path");
        }
        if (!store.remove(id)) {
            throw new NotFoundException("no such version: " + id);
        }
        ObjectNode node = MAPPER.createObjectNode();
        node.put("deleted", id);
        return node;
    }

    private JsonNode coverage(HttpExchange exchange) {
        requireMethod(exchange, "GET");
        Map<String, String> query = queryParams(exchange);
        long from = parseLongParam(query, "from");
        long to = parseLongParam(query, "to");
        ArrayNode array = MAPPER.createArrayNode();
        store.coverage(from, to).forEach(segment -> array.add(toJson(segment)));
        return array;
    }

    private JsonNode point(HttpExchange exchange) {
        requireMethod(exchange, "GET");
        long at = parseLongParam(queryParams(exchange), "at");
        return store.point(at)
                .map(segment -> (JsonNode) toJson(segment))
                .orElseGet(MAPPER::nullNode);
    }

    private Version parseVersion(JsonNode body) {
        if (body == null || !body.isObject()) {
            throw new BadRequestException("request body must be a JSON object");
        }
        String id = requiredText(body, "id");
        if (!body.hasNonNull("priority") || !body.get("priority").isIntegralNumber()) {
            throw new BadRequestException("field 'priority' must be an integer");
        }
        int priority = body.get("priority").intValue();
        if (!body.hasNonNull("intervals") || !body.get("intervals").isArray()) {
            throw new BadRequestException("field 'intervals' must be an array");
        }
        List<IntervalRule> intervals = new ArrayList<>();
        for (JsonNode item : body.get("intervals")) {
            if (!item.hasNonNull("start") || !item.get("start").isIntegralNumber()
                    || !item.hasNonNull("end") || !item.get("end").isIntegralNumber()) {
                throw new BadRequestException("each interval needs integer 'start' and 'end'");
            }
            String label = item.hasNonNull("label") ? item.get("label").asText() : null;
            intervals.add(new IntervalRule(item.get("start").longValue(), item.get("end").longValue(), label));
        }
        return new Version(id, priority, intervals);
    }

    private ObjectNode toJson(Version version) {
        ObjectNode node = MAPPER.createObjectNode();
        node.put("id", version.id());
        node.put("priority", version.priority());
        ArrayNode rules = node.putArray("intervals");
        for (IntervalRule rule : version.intervals()) {
            ObjectNode r = rules.addObject();
            r.put("start", rule.start());
            r.put("end", rule.end());
            if (rule.label() != null) {
                r.put("label", rule.label());
            }
        }
        return node;
    }

    private ObjectNode toJson(com.example.vercov.model.EffectiveSegment segment) {
        ObjectNode node = MAPPER.createObjectNode();
        node.put("start", segment.start());
        node.put("end", segment.end());
        node.put("versionId", segment.versionId());
        node.put("priority", segment.priority());
        if (segment.label() != null) {
            node.put("label", segment.label());
        }
        return node;
    }

    private static JsonNode readBody(HttpExchange exchange) throws IOException {
        byte[] raw = exchange.getRequestBody().readAllBytes();
        if (raw.length == 0) {
            return null;
        }
        try {
            return MAPPER.readTree(new String(raw, StandardCharsets.UTF_8));
        } catch (IOException e) {
            throw new BadRequestException("request body is not valid JSON");
        }
    }

    private static void requireMethod(HttpExchange exchange, String method) {
        if (!method.equals(exchange.getRequestMethod())) {
            throw new BadRequestException("expected " + method + ", got " + exchange.getRequestMethod());
        }
    }

    private static String requiredText(JsonNode body, String field) {
        if (!body.hasNonNull(field) || !body.get(field).isTextual() || body.get(field).asText().isBlank()) {
            throw new BadRequestException("field '" + field + "' must be a non-empty string");
        }
        return body.get(field).asText();
    }

    private static long parseLongParam(Map<String, String> query, String name) {
        String raw = query.get(name);
        if (raw == null) {
            throw new BadRequestException("missing query parameter: " + name);
        }
        try {
            return Long.parseLong(raw);
        } catch (NumberFormatException e) {
            throw new BadRequestException("query parameter '" + name + "' must be an integer, got: " + raw);
        }
    }

    private static Map<String, String> queryParams(HttpExchange exchange) {
        Map<String, String> params = new HashMap<>();
        String query = exchange.getRequestURI().getRawQuery();
        if (query != null) {
            for (String pair : query.split("&")) {
                int eq = pair.indexOf('=');
                if (eq > 0) {
                    params.put(pair.substring(0, eq), pair.substring(eq + 1));
                }
            }
        }
        return params;
    }

    private static ObjectNode error(String code, String message) {
        ObjectNode node = MAPPER.createObjectNode();
        node.put("error", code);
        node.put("message", message);
        return node;
    }

    private static void respond(HttpExchange exchange, int status, JsonNode body) throws IOException {
        byte[] payload = MAPPER.writerWithDefaultPrettyPrinter().writeValueAsBytes(body);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, payload.length);
        exchange.getResponseBody().write(payload);
    }

    public static class BadRequestException extends RuntimeException {
        public BadRequestException(String message) {
            super(message);
        }
    }

    public static class NotFoundException extends RuntimeException {
        public NotFoundException(String message) {
            super(message);
        }
    }
}
