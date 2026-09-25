package streammatch.service;

import streammatch.json.Json;
import streammatch.model.EngineConfig;
import streammatch.model.EngineMode;
import streammatch.model.LatePolicy;
import streammatch.model.MatchPolicy;

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
 * 基于 JDK 内置 {@link HttpServer} 的 JSON HTTP 服务（零外部依赖）。
 *
 * <table>
 *   <caption>路由</caption>
 *   <tr><th>方法+路径</th><th>作用</th></tr>
 *   <tr><td>GET  /health</td><td>健康检查</td></tr>
 *   <tr><td>GET  /config</td><td>查看当前配置</td></tr>
 *   <tr><td>POST /config</td><td>修改配置（mode 不可变）</td></tr>
 *   <tr><td>POST /events</td><td>提交一批事件，返回增量匹配/超时/打断/迟到结果</td></tr>
 *   <tr><td>GET  /matches</td><td>查看累计匹配（/state 的别名，便于 curl 直取）</td></tr>
 *   <tr><td>GET  /state</td><td>完整状态快照</td></tr>
 *   <tr><td>POST /watermark</td><td>事件时间模式手动推进 watermark</td></tr>
 *   <tr><td>POST /replay</td><td>确定性重放并与暴力参考实现对照</td></tr>
 *   <tr><td>POST /reset</td><td>清空状态（可带新配置字段）</td></tr>
 * </table>
 */
public final class ApiServer {

    private final HttpServer server;
    private final MatchingService service;

    public ApiServer(MatchingService service, int port, String bindHost) throws IOException {
        this.service = service;
        this.server = HttpServer.create(new InetSocketAddress(bindHost, port), 0);
        this.server.setExecutor(Executors.newFixedThreadPool(8));
        register();
    }

    public int getPort() {
        return server.getAddress().getPort();
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
    }

    private void register() {
        server.createContext("/health", ex -> dispatch(ex, this::handleHealth));
        server.createContext("/config", ex -> dispatch(ex, this::handleConfig));
        server.createContext("/events", ex -> dispatch(ex, this::handleEvents));
        server.createContext("/matches", ex -> dispatch(ex, this::handleMatches));
        server.createContext("/state", ex -> dispatch(ex, this::handleState));
        server.createContext("/watermark", ex -> dispatch(ex, this::handleWatermark));
        server.createContext("/replay", ex -> dispatch(ex, this::handleReplay));
        server.createContext("/reset", ex -> dispatch(ex, this::handleReset));
    }

    @FunctionalInterface
    private interface Handler {
        Object handle(String method, Map<String, Object> body) throws Exception;
    }

    private void dispatch(HttpExchange ex, Handler h) {
        try {
            String method = ex.getRequestMethod();
            Map<String, Object> body = Map.of();
            if ("POST".equals(method) || "PUT".equals(method)) {
                String text = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
                if (!text.isBlank()) {
                    try {
                        Object parsed = Json.parse(text);
                        if (!(parsed instanceof Map<?, ?>)) {
                            throw ApiException.badRequest("request body must be a JSON object");
                        }
                        @SuppressWarnings("unchecked")
                        Map<String, Object> m = (Map<String, Object>) parsed;
                        body = m;
                    } catch (Json.JsonException je) {
                        throw ApiException.badRequest("malformed JSON: " + je.getMessage());
                    }
                }
            }
            Object result = h.handle(method, body);
            writeJson(ex, 200, result == null ? Map.of() : result);
        } catch (ApiException ae) {
            writeJson(ex, ae.status(), error(ae.code(), ae.getMessage()));
        } catch (Exception other) {
            writeJson(ex, 500, error("INTERNAL_ERROR", String.valueOf(other.getMessage())));
        } finally {
            ex.close();
        }
    }

    private Object handleHealth(String method, Map<String, Object> body) {
        requireGet(method);
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("status", "UP");
        m.put("service", "stream-match");
        return m;
    }

    private Object handleConfig(String method, Map<String, Object> body) {
        if ("GET".equals(method)) {
            return service.configView();
        }
        if ("POST".equals(method)) {
            return service.reconfigure(body);
        }
        throw ApiException.badRequest("method not allowed: " + method);
    }

    private Object handleEvents(String method, Map<String, Object> body) {
        requirePost(method);
        return service.ingest(body);
    }

    private Object handleMatches(String method, Map<String, Object> body) {
        requireGet(method);
        return service.state().get("matches");
    }

    private Object handleState(String method, Map<String, Object> body) {
        requireGet(method);
        return service.state();
    }

    private Object handleWatermark(String method, Map<String, Object> body) {
        requirePost(method);
        return service.advanceWatermark(body);
    }

    private Object handleReplay(String method, Map<String, Object> body) {
        requirePost(method);
        return service.replay(body);
    }

    private Object handleReset(String method, Map<String, Object> body) {
        requirePost(method);
        return service.reset(body);
    }

    private static void requireGet(String method) {
        if (!"GET".equals(method)) {
            throw ApiException.badRequest("method not allowed: " + method);
        }
    }

    private static void requirePost(String method) {
        if (!"POST".equals(method)) {
            throw ApiException.badRequest("method not allowed: " + method);
        }
    }

    private static Map<String, Object> error(String code, String message) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("error", code);
        m.put("message", message);
        return m;
    }

    private static void writeJson(HttpExchange ex, int status, Object payload) {
        byte[] bytes = Json.writePretty(payload).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        try {
            ex.sendResponseHeaders(status, bytes.length);
            try (OutputStream os = ex.getResponseBody()) {
                os.write(bytes);
            }
        } catch (IOException ioe) {
            // 客户端断开等：无法再写响应
        }
    }

    // ---------------------------------------------------------------- main

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(System.getenv().getOrDefault("PORT", "8080"));
        String host = System.getenv().getOrDefault("BIND", "127.0.0.1");
        EngineMode mode = EngineMode.valueOf(
                System.getenv().getOrDefault("MODE", "EVENT_TIME"));
        long window = Long.parseLong(System.getenv().getOrDefault("WINDOW_MILLIS", "1000"));
        long lateness = Long.parseLong(System.getenv().getOrDefault("ALLOWED_LATENESS_MILLIS", "0"));
        MatchPolicy policy = MatchPolicy.valueOf(
                System.getenv().getOrDefault("MATCH_POLICY", "ALL_CANDIDATES"));
        LatePolicy latePolicy = LatePolicy.valueOf(
                System.getenv().getOrDefault("LATE_POLICY", "DROP"));

        EngineConfig cfg = new EngineConfig(mode, window, policy, lateness, latePolicy);
        MatchingService svc = MatchingService.createWithWallClock(cfg);
        ApiServer api = new ApiServer(svc, port, host);
        api.start();

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            api.stop();
            svc.close();
        }));
        System.out.println("stream-match listening on http://" + host + ":" + api.getPort()
                + "  mode=" + mode + " windowMillis=" + window + " matchPolicy=" + policy
                + " allowedLatenessMillis=" + lateness + " latePolicy=" + latePolicy);
    }
}
