package com.example.quantiles.service;

import com.example.quantiles.json.Json;
import com.example.quantiles.json.JsonException;
import com.example.quantiles.json.JsonParser;
import com.example.quantiles.json.JsonWriter;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * 基于 JDK 内置 {@link HttpServer} 的 JSON HTTP 服务（零外部依赖、无消息中间件）。
 *
 * <ul>
 *   <li>{@code GET  /health} —— 健康检查；</li>
 *   <li>{@code POST /quantiles} —— 离线滑动窗口精确分位数计算，请求/响应均为 JSON。</li>
 * </ul>
 */
public final class QuantileHttpServer {

    private final HttpServer server;
    private final BatchQuantileService service = new BatchQuantileService();

    public QuantileHttpServer(int port) throws IOException {
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/quantiles", this::handleQuantiles);
        server.setExecutor(Executors.newFixedThreadPool(4));
    }

    public int getPort() {
        return server.getAddress().getPort();
    }

    public void start() {
        server.start();
    }

    public void stop(int delaySeconds) {
        server.stop(delaySeconds);
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        if (!"GET".equalsIgnoreCase(ex.getRequestMethod())) {
            sendError(ex, 405, "仅支持 GET");
            return;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "ok");
        body.put("service", "sliding-window-exact-quantiles");
        sendJson(ex, 200, body);
    }

    private void handleQuantiles(HttpExchange ex) throws IOException {
        try {
            if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
                sendError(ex, 405, "仅支持 POST");
                return;
            }
            String requestText = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            if (requestText.isBlank()) {
                sendError(ex, 400, "请求体为空");
                return;
            }
            Object parsed;
            try {
                parsed = JsonParser.parse(requestText);
            } catch (JsonException e) {
                sendError(ex, 400, "JSON 解析失败: " + e.getMessage());
                return;
            }
            Map<String, Object> request = Json.asObject(parsed);
            Map<String, Object> response = service.handle(request);
            sendJson(ex, 200, response);
        } catch (BatchQuantileService.BadRequestException e) {
            sendError(ex, 400, e.getMessage());
        } catch (Exception e) {
            sendError(ex, 500, "内部错误: " + e);
        }
    }

    private static void sendJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = JsonWriter.writePretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(payload);
        }
    }

    private static void sendError(HttpExchange ex, int status, String message) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", true);
        body.put("status", status);
        body.put("message", message);
        sendJson(ex, status, body);
    }
}
