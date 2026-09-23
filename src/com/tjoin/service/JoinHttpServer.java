package com.tjoin.service;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * 基于 JDK 内置 {@link HttpServer} 的极简 JSON HTTP 服务（无任何第三方依赖）。
 *
 * <ul>
 *   <li>{@code GET  /health} — 健康检查；</li>
 *   <li>{@code POST /join}   — 运行一次区间连接模拟（请求体见 samples/）；</li>
 * </ul>
 *
 * 每个请求独立创建算子实例（无共享状态），单线程即可确定性处理。
 */
public final class JoinHttpServer implements AutoCloseable {

    private final HttpServer server;
    private final int port;

    public JoinHttpServer(int port) throws IOException {
        this.port = port;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        this.server.createContext("/health", this::handleHealth);
        this.server.createContext("/join", this::handleJoin);
        this.server.setExecutor(Executors.newSingleThreadExecutor(r -> {
            Thread t = new Thread(r, "tjoin-http");
            t.setDaemon(true);
            return t;
        }));
    }

    public int getPort() {
        return server.getAddress().getPort();
    }

    public void start() {
        server.start();
    }

    @Override
    public void close() {
        server.stop(0);
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) {
            sendError(ex, 405, "method not allowed");
            return;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("ok", true);
        body.put("service", "tjoin-interval-join");
        body.put("port", getPort());
        sendJson(ex, 200, body);
    }

    private void handleJoin(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            sendError(ex, 405, "method not allowed");
            return;
        }
        String requestBody;
        try (InputStream in = ex.getRequestBody()) {
            requestBody = new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
        if (requestBody.isBlank()) {
            sendError(ex, 400, "empty request body");
            return;
        }
        try {
            Object parsed = Json.parse(requestBody);
            if (!(parsed instanceof Map)) {
                sendError(ex, 400, "request must be a JSON object");
                return;
            }
            @SuppressWarnings("unchecked")
            Map<String, Object> request = (Map<String, Object>) parsed;
            Map<String, Object> response = JoinSimulation.run(request);
            sendJson(ex, 200, response);
        } catch (IllegalArgumentException je) {
            sendError(ex, 400, "invalid JSON: " + je.getMessage());
        } catch (JoinSimulation.BadRequestException be) {
            sendError(ex, 400, be.getMessage());
        } catch (JoinSimulation.CapacityException ce) {
            sendError(ex, 422, ce.getMessage());
        } catch (RuntimeException re) {
            sendError(ex, 500, "internal error: " + re);
        }
    }

    private static void sendJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = Json.writePretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(payload);
        }
    }

    private static void sendError(HttpExchange ex, int status, String message) throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("ok", false);
        body.put("error", message);
        body.put("status", status);
        sendJson(ex, status, body);
    }
}
