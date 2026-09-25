package sessions.service;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Map;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import sessions.json.Json;
import sessions.json.JsonWriter;

/**
 * 基于 JDK 内置 HttpServer 的 JSON 服务，无外部依赖。
 *
 * <ul>
 *   <li>{@code GET /health} -&gt; {@code {"status":"ok"}}</li>
 *   <li>{@code POST /sessions/run} -&gt; 运行一个会话窗口请求，返回 changelog、
 *       finalResults 与状态统计。服务本身无状态：每个请求独立计算。</li>
 * </ul>
 */
public final class SessionHttpServer {

    private final HttpServer server;

    private SessionHttpServer(HttpServer server) {
        this.server = server;
    }

    public static SessionHttpServer start(int port) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        SessionHttpServer wrapper = new SessionHttpServer(server);
        server.createContext("/health", wrapper::handleHealth);
        server.createContext("/sessions/run", wrapper::handleRun);
        server.setExecutor(null);
        server.start();
        return wrapper;
    }

    public int port() {
        return server.getAddress().getPort();
    }

    public void stop() {
        server.stop(0);
    }

    private void handleHealth(HttpExchange exchange) throws IOException {
        writeJson(exchange, 200, Map.of("status", "ok"));
    }

    private void handleRun(HttpExchange exchange) throws IOException {
        try {
            if (!"POST".equalsIgnoreCase(exchange.getRequestMethod())) {
                writeJson(exchange, 405, Map.of("error", "method not allowed; use POST"));
                return;
            }
            String body = new String(exchange.getRequestBody().readAllBytes(),
                    StandardCharsets.UTF_8);
            Object parsed;
            try {
                parsed = Json.parse(body);
            } catch (RuntimeException ex) {
                writeJson(exchange, 400, Map.of("error", "invalid JSON: " + ex.getMessage()));
                return;
            }
            if (!(parsed instanceof Map<?, ?>)) {
                writeJson(exchange, 400, Map.of("error", "request body must be a JSON object"));
                return;
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> request = (Map<String, Object>) parsed;
            Map<String, Object> response = SessionRequestRunner.run(request);
            writeJson(exchange, 200, response);
        } catch (BadRequestException ex) {
            writeJson(exchange, 400, Map.of("error", ex.getMessage()));
        } catch (Exception ex) {
            writeJson(exchange, 500, Map.of("error",
                    ex.getClass().getSimpleName() + ": " + ex.getMessage()));
        }
    }

    private static void writeJson(HttpExchange exchange, int status, Object payload)
            throws IOException {
        byte[] bytes = JsonWriter.writePretty(payload).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(bytes);
        }
    }
}
