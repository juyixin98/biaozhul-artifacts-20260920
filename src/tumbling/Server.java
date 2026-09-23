package tumbling;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * 基于 JDK 内置 com.sun.net.httpserver.HttpServer 的 REST 服务。
 * 单线程处理请求，保证按到达顺序串行、确定地推进引擎状态。
 */
public final class Server implements AutoCloseable {

    private final HttpServer http;
    private final WindowEngine engine;

    public Server(int port, long windowSize, long allowedLateness) throws IOException {
        this.engine = new WindowEngine(windowSize, allowedLateness);
        this.http = HttpServer.create(new InetSocketAddress(port), 0);
        http.setExecutor(Executors.newSingleThreadExecutor(r -> {
            Thread t = new Thread(r, "window-engine");
            t.setDaemon(true);
            return t;
        }));
        http.createContext("/", this::route);
    }

    public WindowEngine engine() { return engine; }

    public int getAddressPort() { return http.getAddress().getPort(); }

    public void start() { http.start(); }

    @Override
    public void close() { http.stop(0); }

    // ============================ 路由 ============================

    private void route(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (path) {
                case "/health":
                    require(method, "GET");
                    send(ex, 200, Map.of("status", "ok"));
                    return;
                case "/config":
                    require(method, "GET");
                    send(ex, 200, engine.configView());
                    return;
                case "/events":
                    require(method, "POST");
                    handleIngest(ex);
                    return;
                case "/watermarks":
                    require(method, "POST");
                    handleWatermark(ex);
                    return;
                case "/partitions/idle":
                    require(method, "POST");
                    handleIdle(ex);
                    return;
                case "/partitions":
                    require(method, "GET");
                    handlePartitions(ex);
                    return;
                case "/windows":
                    require(method, "GET");
                    handleWindows(ex);
                    return;
                case "/side-output":
                    require(method, "GET");
                    send(ex, 200, Map.of("sideOutput", engine.sideOutputView()));
                    return;
                case "/duplicates":
                    require(method, "GET");
                    send(ex, 200, Map.of("duplicates", engine.duplicatesView()));
                    return;
                case "/emissions":
                    require(method, "GET");
                    send(ex, 200, Map.of("emissions", engine.emissionsView()));
                    return;
                case "/snapshot":
                    require(method, "GET");
                    send(ex, 200, engine.snapshot());
                    return;
                case "/reset":
                    require(method, "POST");
                    handleReset(ex);
                    return;
                default:
                    sendError(ex, 404, "not_found", "未知路径: " + path);
            }
        } catch (ApiException e) {
            sendError(ex, e.status, e.code, e.getMessage());
        } catch (IllegalArgumentException e) {
            sendError(ex, 400, "bad_request", e.getMessage());
        } catch (Exception e) {
            sendError(ex, 500, "internal_error", e.toString());
        }
    }

    private static void require(String actual, String expected) {
        if (!expected.equals(actual)) {
            throw new ApiException(405, "method_not_allowed", "仅支持 " + expected + "，收到 " + actual);
        }
    }

    // ============================ 处理器 ============================

    private void handleIngest(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonBody(ex);
        if (body.containsKey("events")) {
            // 批量：先逐条解析（任一非法在此抛 400，不触碰引擎），再原子执行。
            List<Object> events = Json.arr(body, "events");
            List<WindowEngine.EventInput> inputs = new java.util.ArrayList<>();
            for (Object o : events) {
                Map<String, Object> ev = asObject(o, "events 数组元素");
                inputs.add(new WindowEngine.EventInput(
                        Json.str(ev, "partition"), Json.str(ev, "eventId"), Json.lng(ev, "eventTime")));
            }
            List<Object> results = new java.util.ArrayList<>(engine.ingestBatch(inputs));
            send(ex, 200, Map.of("results", results, "count", results.size()));
        } else {
            send(ex, 200, oneIngest(body));
        }
    }

    private Map<String, Object> oneIngest(Map<String, Object> ev) {
        String partition = Json.str(ev, "partition");
        String eventId = Json.str(ev, "eventId");
        long eventTime = Json.lng(ev, "eventTime");
        return engine.ingest(partition, eventId, eventTime);
    }

    private void handleWatermark(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonBody(ex);
        if (body.containsKey("watermarks")) {
            // 批量：先逐条解析（任一非法在此抛 400，不触碰引擎），再原子执行。
            List<Object> wms = Json.arr(body, "watermarks");
            List<WindowEngine.WatermarkInput> inputs = new java.util.ArrayList<>();
            for (Object o : wms) {
                Map<String, Object> w = asObject(o, "watermarks 数组元素");
                inputs.add(new WindowEngine.WatermarkInput(Json.str(w, "partition"), Json.lng(w, "watermark")));
            }
            List<Object> results = new java.util.ArrayList<>(engine.advanceWatermarks(inputs));
            send(ex, 200, Map.of("results", results, "count", results.size()));
        } else {
            String partition = Json.str(body, "partition");
            long watermark = Json.lng(body, "watermark");
            send(ex, 200, engine.advanceWatermark(partition, watermark));
        }
    }

    private void handleIdle(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonBody(ex);
        String partition = Json.str(body, "partition");
        boolean idle = Json.bool(body, "idle", true);
        send(ex, 200, engine.setIdle(partition, idle));
    }

    private void handlePartitions(HttpExchange ex) throws IOException {
        Map<String, Object> snap = engine.snapshot();
        send(ex, 200, Map.of(
                "globalWatermark", snap.get("globalWatermark"),
                "partitions", snap.get("partitions")));
    }

    private void handleWindows(HttpExchange ex) throws IOException {
        String partition = queryParam(ex, "partition");
        if (partition == null) {
            // 全量：逐分区合并统一形状的视图（每条都带 partition 归属与 state）。
            List<Object> all = new java.util.ArrayList<>();
            for (Object o : (List<?>) engine.snapshot().get("partitions")) {
                @SuppressWarnings("unchecked")
                Map<String, Object> p = (Map<String, Object>) o;
                all.addAll(engine.windowsView((String) p.get("partition")));
            }
            send(ex, 200, Map.of("windows", all));
        } else {
            send(ex, 200, Map.of("partition", partition, "windows", engine.windowsView(partition)));
        }
    }

    private void handleReset(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonBody(ex); // 允许空体 {}
        Long windowSize = body.containsKey("windowSize") ? Json.lng(body, "windowSize") : null;
        Long allowedLateness = body.containsKey("allowedLateness") ? Json.lng(body, "allowedLateness") : null;
        engine.reset(windowSize, allowedLateness);
        send(ex, 200, Map.of("reset", true, "config", engine.configView()));
    }

    // ============================ HTTP 辅助 ============================

    @SuppressWarnings("unchecked")
    private static Map<String, Object> asObject(Object o, String what) {
        if (!(o instanceof Map)) throw new IllegalArgumentException(what + " 必须是 JSON 对象");
        return (Map<String, Object>) o;
    }

    private static Map<String, Object> readJsonBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            String raw = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            if (raw.isBlank()) return new java.util.LinkedHashMap<>();
            Object parsed = Json.parse(raw);
            if (!(parsed instanceof Map)) throw new IllegalArgumentException("请求体必须是 JSON 对象");
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) parsed;
            return m;
        }
    }

    private static String queryParam(HttpExchange ex, String name) {
        String q = ex.getRequestURI().getRawQuery();
        if (q == null) return null;
        for (String pair : q.split("&")) {
            int i = pair.indexOf('=');
            String k = i < 0 ? pair : pair.substring(0, i);
            if (k.equals(name)) {
                String v = i < 0 ? "" : pair.substring(i + 1);
                return java.net.URLDecoder.decode(v, StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    private static void send(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = Json.writePretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(payload);
        }
    }

    private static void sendError(HttpExchange ex, int status, String code, String message) throws IOException {
        send(ex, status, Map.of("error", code, "message", message == null ? "" : message));
    }

    private static final class ApiException extends RuntimeException {
        final int status;
        final String code;
        ApiException(int status, String code, String message) {
            super(message);
            this.status = status;
            this.code = code;
        }
    }
}
