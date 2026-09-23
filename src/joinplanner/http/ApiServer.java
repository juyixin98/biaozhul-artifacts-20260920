package joinplanner.http;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import joinplanner.json.BadInputException;
import joinplanner.json.J;
import joinplanner.json.JsonException;
import joinplanner.json.JsonParser;
import joinplanner.json.JsonWriter;
import joinplanner.model.Spec;
import joinplanner.model.SpecParser;
import joinplanner.plan.EnumerateService;
import joinplanner.plan.PlanService;
import joinplanner.sim.SimulationService;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Pure-JDK HTTP front end ({@code com.sun.net.httpserver}). Routes:
 * <ul>
 *   <li>{@code GET  /health}                       — liveness probe.</li>
 *   <li>{@code POST /plan}                         — DP optimal join plan.</li>
 *   <li>{@code POST /enumerate}                    — exhaustive orders + DP cross-check.</li>
 *   <li>{@code POST /simulate}                     — data generation, real joins, plan regret.</li>
 * </ul>
 */
public final class ApiServer {

    private final int port;
    private HttpServer server;
    private ExecutorService executor;

    public ApiServer(int port) {
        this.port = port;
    }

    public void start() throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/plan", this::handlePlan);
        server.createContext("/enumerate", this::handleEnumerate);
        server.createContext("/simulate", this::handleSimulate);
        // Daemon threads so an embedded caller (or a forgotten stop()) never traps the JVM;
        // stop() also shuts the pool down explicitly.
        executor = Executors.newFixedThreadPool(4, r -> {
            Thread th = new Thread(r, "joinplanner-http");
            th.setDaemon(true);
            return th;
        });
        server.setExecutor(executor);
        server.start();
    }

    public int actualPort() {
        return server.getAddress().getPort();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
        if (executor != null) {
            executor.shutdownNow();
        }
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) {
            sendError(ex, 405, "METHOD_NOT_ALLOWED", "Use GET");
            return;
        }
        Map<String, Object> body = J.newObj();
        body.put("status", "ok");
        body.put("service", "join-order-planner");
        body.put("maxTables", SpecParser.MAX_TABLES);
        sendJson(ex, 200, body);
    }

    private void handlePlan(HttpExchange ex) throws IOException {
        dispatch(ex, body -> {
            Spec spec = SpecParser.parse(body);
            return new PlanService().plan(spec);
        });
    }

    private void handleEnumerate(HttpExchange ex) throws IOException {
        dispatch(ex, body -> {
            Spec spec = SpecParser.parse(body);
            return new EnumerateService().enumerate(spec);
        });
    }

    private void handleSimulate(HttpExchange ex) throws IOException {
        dispatch(ex, body -> new SimulationService().run(body));
    }

    private interface Handler {
        Map<String, Object> handle(Map<String, Object> body);
    }

    private void dispatch(HttpExchange ex, Handler handler) throws IOException {
        try {
            if (!"POST".equals(ex.getRequestMethod())) {
                sendError(ex, 405, "METHOD_NOT_ALLOWED", "Use POST with a JSON body");
                return;
            }
            String text = readBody(ex);
            Object parsed;
            try {
                parsed = JsonParser.parse(text);
            } catch (JsonException e) {
                sendError(ex, 400, "MALFORMED_JSON", e.getMessage());
                return;
            }
            Map<String, Object> body = J.obj(parsed, "request body");
            Map<String, Object> result = handler.handle(body);
            sendJson(ex, 200, result);
        } catch (PlanService.DisconnectedException e) {
            sendJsonRaw(ex, 422, JsonWriter.writePretty(e.body));
        } catch (BadInputException e) {
            sendError(ex, 400, "BAD_REQUEST", e.getMessage());
        } catch (Exception e) {
            sendError(ex, 500, "INTERNAL_ERROR", safeMessage(e));
        }
    }

    private String safeMessage(Exception e) {
        String msg = e.getClass().getSimpleName() + ": " + e.getMessage();
        return msg.length() > 500 ? msg.substring(0, 500) : msg;
    }

    private String readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        if (bytes.length > 16 * 1024 * 1024) {
            throw new BadInputException("Request body too large (limit 16MB)");
        }
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private void sendJson(HttpExchange ex, int status, Map<String, Object> body) throws IOException {
        sendJsonRaw(ex, status, JsonWriter.writePretty(body));
    }

    private void sendJsonRaw(HttpExchange ex, int status, String json) throws IOException {
        byte[] data = json.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(data);
        }
    }

    private void sendError(HttpExchange ex, int status, String code, String message) throws IOException {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("error", code);
        err.put("message", message);
        sendJsonRaw(ex, status, JsonWriter.writePretty(err));
    }
}
