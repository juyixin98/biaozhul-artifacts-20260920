package com.migration.planner.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.migration.planner.model.MigrationEdge;
import com.migration.planner.model.MigrationPlan;
import com.migration.planner.model.PlanRequest;
import com.migration.planner.plan.PlanException;
import com.migration.planner.tz.TzdbInfo;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * JSON-over-HTTP API built on the JDK's embedded HTTP server (no web framework).
 *
 * <ul>
 *   <li>POST /plan   — body: PlanRequest JSON; 200 with plan, 422 for UNKNOWN_VERSION/NO_PATH</li>
 *   <li>GET  /health — service status and tzdb version</li>
 *   <li>GET  /graph  — the loaded graph fixture</li>
 * </ul>
 */
public final class HttpApiServer implements AutoCloseable {

    private final HttpServer server;

    private HttpApiServer(HttpServer server) {
        this.server = server;
    }

    public static HttpApiServer start(PlanService service, ObjectMapper mapper, int port) {
        try {
            HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
            server.createContext("/plan", exchange -> handlePlan(service, mapper, exchange));
            server.createContext("/health", exchange -> handleHealth(service, mapper, exchange));
            server.createContext("/graph", exchange -> handleGraph(service, mapper, exchange));
            server.setExecutor(Executors.newFixedThreadPool(4));
            server.start();
            return new HttpApiServer(server);
        } catch (IOException e) {
            throw new UncheckedIOException("failed to start HTTP server", e);
        }
    }

    public int port() {
        return server.getAddress().getPort();
    }

    @Override
    public void close() {
        server.stop(0);
    }

    private static void handlePlan(PlanService service, ObjectMapper mapper, HttpExchange exchange)
            throws IOException {
        if (!"POST".equalsIgnoreCase(exchange.getRequestMethod())) {
            send(mapper, exchange, 405, errorBody(mapper, "INVALID_REQUEST", "use POST", Map.of()));
            return;
        }
        try {
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            PlanRequest request = mapper.readValue(body, PlanRequest.class);
            MigrationPlan plan = service.plan(request);
            send(mapper, exchange, 200, mapper.valueToTree(plan));
        } catch (PlanException e) {
            int status = e.code() == PlanException.Code.INVALID_REQUEST ? 400 : 422;
            send(mapper, exchange, status, errorBody(mapper, e.code().name(), e.getMessage(), e.details()));
        } catch (com.fasterxml.jackson.core.JsonProcessingException e) {
            send(mapper, exchange, 400,
                    errorBody(mapper, "INVALID_REQUEST", "malformed JSON: " + e.getOriginalMessage(), Map.of()));
        }
    }

    private static void handleHealth(PlanService service, ObjectMapper mapper, HttpExchange exchange)
            throws IOException {
        ObjectNode body = mapper.createObjectNode();
        body.put("status", "ok");
        body.put("graphId", service.graph().graphId());
        body.put("graphVersion", service.graph().graphVersion());
        body.put("nodeCount", service.graph().nodes().size());
        body.put("edgeCount", service.graph().edges().size());
        body.put("tzdbVersion", TzdbInfo.currentTzdbVersion());
        send(mapper, exchange, 200, body);
    }

    private static void handleGraph(PlanService service, ObjectMapper mapper, HttpExchange exchange)
            throws IOException {
        ObjectNode body = mapper.createObjectNode();
        body.put("graphId", service.graph().graphId());
        body.put("graphVersion", service.graph().graphVersion());
        body.set("nodes", mapper.valueToTree(service.graph().nodes()));
        body.set("edges", mapper.valueToTree(service.graph().edges().stream().map(edge -> Map.of(
                "id", edge.id(),
                "from", edge.from(),
                "to", edge.to(),
                "cost", edge.cost(),
                "reversible", edge.reversible(),
                "description", edge.description(),
                "preconditions", edge.preconditions())).toList()));
        body.set("warnings", mapper.valueToTree(service.graph().warnings()));
        send(mapper, exchange, 200, body);
    }

    private static ObjectNode errorBody(ObjectMapper mapper, String code, String message,
                                        Map<String, Object> details) {
        ObjectNode body = mapper.createObjectNode();
        body.put("ok", false);
        ObjectNode error = body.putObject("error");
        error.put("code", code);
        error.put("message", message);
        error.set("details", mapper.valueToTree(details));
        return body;
    }

    private static void send(ObjectMapper mapper, HttpExchange exchange, int status,
                             com.fasterxml.jackson.databind.JsonNode body) throws IOException {
        byte[] bytes = mapper.writeValueAsBytes(body);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        exchange.getResponseBody().write(bytes);
        exchange.close();
    }
}
