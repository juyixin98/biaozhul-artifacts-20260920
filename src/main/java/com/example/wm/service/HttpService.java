package com.example.wm.service;

import com.example.wm.json.JsonException;
import com.example.wm.json.JsonWriter;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 纯后端 JSON 服务。无外部消息系统、无第三方依赖。
 *
 * <ul>
 *   <li>{@code POST /simulate}：执行确定性模拟脚本（见 examples/ 与 README）。</li>
 *   <li>{@code GET  /health}：健康检查。</li>
 * </ul>
 *
 * 也可作为命令行批处理器：{@code java ... CliMain run <request.json>}。
 */
public final class HttpService {

    private final SimulationService simulationService = new SimulationService();
    private HttpServer server;

    public void start(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/simulate", this::handleSimulate);
        server.setExecutor(null);
        server.start();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) {
            sendError(ex, 405, "method_not_allowed", "use GET");
            return;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("ok", true);
        body.put("service", "multi-source-watermark");
        sendJson(ex, 200, JsonWriter.pretty(body));
    }

    private void handleSimulate(HttpExchange ex) throws IOException {
        try {
            if (!"POST".equals(ex.getRequestMethod())) {
                sendError(ex, 405, "method_not_allowed", "use POST");
                return;
            }
            String requestJson;
            try (InputStream in = ex.getRequestBody()) {
                requestJson = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            }
            if (requestJson.isBlank()) {
                sendError(ex, 400, "empty_request", "request body must be a JSON simulation script");
                return;
            }
            Map<String, Object> result = simulationService.execute(
                    com.example.wm.json.Json.parseObject(requestJson));
            sendJson(ex, 200, JsonWriter.pretty(result));
        } catch (JsonException | IllegalArgumentException e) {
            sendError(ex, 400, "bad_request", e.getMessage());
        } catch (Exception e) {
            sendError(ex, 500, "internal_error", e.toString());
        }
    }

    private static void sendJson(HttpExchange ex, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(bytes);
        }
    }

    private static void sendError(HttpExchange ex, int status, String code, String message)
            throws IOException {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("ok", false);
        err.put("error", code);
        err.put("message", message);
        sendJson(ex, status, JsonWriter.pretty(err));
    }

    // ------------------------------------------------------------------
    // 入口
    // ------------------------------------------------------------------

    public static void main(String[] args) throws Exception {
        if (args.length > 0 && "run".equals(args[0])) {
            runCli(args);
            return;
        }
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 8080;
        HttpService service = new HttpService();
        service.start(port);
        System.out.println("multi-source-watermark listening on http://0.0.0.0:" + port);
        System.out.println("  POST /simulate  (JSON simulation script)");
        System.out.println("  GET  /health");
        // 阻塞主线程；按 Ctrl+C 退出
        Thread.currentThread().join();
    }

    /** {@code run <request.json|- > [output.json]}：离线批处理，方便脚本化验收。 */
    private static void runCli(String[] args) throws IOException {
        if (args.length < 2) {
            System.err.println("usage: run <request.json|-> [output.json]  ('-' = stdin)");
            System.exit(2);
        }
        String requestJson;
        if ("-".equals(args[1])) {
            requestJson = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
        } else {
            requestJson = Files.readString(Path.of(args[1]));
        }
        String responseJson = new SimulationService().executeJson(requestJson);
        if (args.length >= 3) {
            Files.writeString(Path.of(args[2]), responseJson);
            System.err.println("response written to " + args[2]);
        } else {
            System.out.println(responseJson);
        }
    }
}
