package tvl.server;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import tvl.api.JsonApi;

/**
 * 基于 JDK 内置 {@link HttpServer} 的极简 HTTP 入口（无第三方依赖）。
 *
 * 路由：
 *   POST /query   —— 请求体为一个 JSON 请求对象，返回 JSON 响应
 *   GET  /health  —— 健康检查
 *
 * 注意：仅用于单机/本地演示，未实现鉴权、压缩等生产能力。
 */
public final class ApiServer {

    private final JsonApi api;
    private final int port;
    private HttpServer server;

    public ApiServer(int port) {
        this(new JsonApi(), port);
    }

    public ApiServer(JsonApi api, int port) {
        this.api = api;
        this.port = port;
    }

    public void start() throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/query", this::handleQuery);
        server.createContext("/health", this::handleHealth);
        server.setExecutor(null);
        server.start();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
    }

    private void handleQuery(HttpExchange exchange) throws IOException {
        try {
            if (!"POST".equalsIgnoreCase(exchange.getRequestMethod())) {
                writeJson(exchange, 405,
                        "{\"ok\":false,\"error\":\"INVALID_REQUEST\",\"message\":\"use POST\"}");
                return;
            }
            String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            String response = api.handleJson(body);
            int status = response.contains("\"ok\":false") ? 400 : 200;
            writeJson(exchange, status, response);
        } finally {
            exchange.close();
        }
    }

    private void handleHealth(HttpExchange exchange) throws IOException {
        try {
            writeJson(exchange, 200, "{\"ok\":true,\"service\":\"tvl-query-engine\"}");
        } finally {
            exchange.close();
        }
    }

    private static void writeJson(HttpExchange exchange, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(bytes);
        }
    }
}
