package joinplanner.web;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import joinplanner.core.PlanService;
import joinplanner.json.JsonWriter;
import joinplanner.sim.SimService;

/**
 * JDK-only HTTP server ({@link HttpServer}) exposing the planning service.
 *
 * <ul>
 *   <li>POST /api/plan      — join-order optimization with explanations</li>
 *   <li>POST /api/simulate  — synthetic-data estimation vs reality comparison</li>
 *   <li>GET  /health        — liveness probe</li>
 * </ul>
 */
public final class Server {

    private final int port;
    private HttpServer http;

    public Server(int port) {
        this.port = port;
    }

    public void start() throws IOException {
        http = HttpServer.create(new InetSocketAddress(port), 0);
        http.createContext("/api/plan", this::handlePlan);
        http.createContext("/api/simulate", this::handleSimulate);
        http.createContext("/health", this::handleHealth);
        http.createContext("/", this::handleRoot);
        http.setExecutor(java.util.concurrent.Executors.newFixedThreadPool(8));
        http.start();
    }

    public void stop() {
        if (http != null) {
            http.stop(0);
        }
    }

    public int boundPort() {
        return http == null ? port : http.getAddress().getPort();
    }

    private void handlePlan(HttpExchange ex) throws IOException {
        dispatch(ex, PlanService.INSTANCE::handle);
    }

    private void handleSimulate(HttpExchange ex) throws IOException {
        dispatch(ex, SimService.INSTANCE::handle);
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        if (!"GET".equalsIgnoreCase(ex.getRequestMethod())) {
            sendJson(ex, 405, Map.of("error", "METHOD_NOT_ALLOWED", "message", "use GET"));
            return;
        }
        sendJson(ex, 200, Map.of("status", "ok", "service", "join-order-planner",
                "maxTables", 8));
    }

    private void handleRoot(HttpExchange ex) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("service", "join-order-planner");
        m.put("endpoints", Map.of(
                "POST /api/plan", "optimize inner-join order for up to 8 tables",
                "POST /api/simulate", "estimate vs reality on skewed data",
                "GET /health", "liveness probe"));
        sendJson(ex, 200, m);
    }

    private interface ServiceHandler {
        ServiceResult handle(String body) throws Exception;
    }

    private void dispatch(HttpExchange ex, ServiceHandler handler) throws IOException {
        try {
            if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
                sendJson(ex, 405, Map.of("error", "METHOD_NOT_ALLOWED",
                        "message", "use POST with a JSON body"));
                return;
            }
            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            if (body.isBlank()) {
                sendJson(ex, 400, Map.of("error", "EMPTY_BODY",
                        "message", "expected a JSON request body"));
                return;
            }
            ServiceResult result = handler.handle(body);
            sendJson(ex, result.status(), result.body());
        } catch (Exception e) {
            sendJson(ex, 500, Map.of("error", "INTERNAL",
                    "message", String.valueOf(e.getMessage())));
        }
    }

    private void sendJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = JsonWriter.writePretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(payload);
        }
    }
}
