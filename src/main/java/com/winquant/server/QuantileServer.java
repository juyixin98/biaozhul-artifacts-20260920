package com.winquant.server;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import com.winquant.Clock;
import com.winquant.SlidingWindowQuantile;
import com.winquant.json.Json;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.OptionalDouble;
import java.util.concurrent.Executors;

/**
 * 滑动窗口精确分位数的 JSON HTTP 服务（基于 JDK 内置 HttpServer，无外部依赖）。
 *
 * <ul>
 *   <li>{@code POST /events} — 提交事件：{"timestamp":1,"value":5} 或 {"events":[{...},...]}；
 *       响应 {"accepted":k,"late":m}</li>
 *   <li>{@code GET /quantile?q=0.5} — 查询当前窗口精确分位数；
 *       响应 {"q":0.5,"count":n,"value":x}，窗口为空时 value 为 null</li>
 *   <li>{@code GET /median} — 等价于 /quantile?q=0.5</li>
 *   <li>{@code GET /stats} — 窗口状态：now、windowStart、count、lateEvents</li>
 *   <li>{@code POST /advance {"now":t}} — 仅当服务以 ManualClock 启动时可用，推进时钟</li>
 *   <li>{@code GET /health} — {"status":"ok"}</li>
 * </ul>
 */
public final class QuantileServer {

    private final HttpServer server;
    private final SlidingWindowQuantile window;
    private final Clock clock;
    private final Object lock = new Object();

    public QuantileServer(int port, long windowSize, Clock clock) throws IOException {
        this.window = new SlidingWindowQuantile(windowSize, clock);
        this.clock = clock;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/events", this::handleEvents);
        server.createContext("/quantile", this::handleQuantile);
        server.createContext("/median", this::handleQuantile);
        server.createContext("/stats", this::handleStats);
        server.createContext("/advance", this::handleAdvance);
        server.createContext("/health", this::handleHealth);
        server.setExecutor(Executors.newFixedThreadPool(4));
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
    }

    public int port() {
        return server.getAddress().getPort();
    }

    // ---------- handlers ----------

    private void handleEvents(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            send(ex, 405, errorBody("method not allowed"));
            return;
        }
        try {
            Map<String, Object> body = Json.parseObject(readBody(ex));
            int accepted = 0;
            int late = 0;
            synchronized (lock) {
                Object batch = body.get("events");
                if (batch instanceof List) {
                    for (Object item : (List<?>) batch) {
                        if (addEvent(asMap(item))) {
                            accepted++;
                        } else {
                            late++;
                        }
                    }
                } else {
                    if (addEvent(body)) {
                        accepted++;
                    } else {
                        late++;
                    }
                }
            }
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("accepted", accepted);
            resp.put("late", late);
            send(ex, 200, Json.write(resp));
        } catch (IllegalArgumentException e) {
            send(ex, 400, errorBody(e.getMessage()));
        }
    }

    private boolean addEvent(Map<String, Object> event) {
        long ts = asLong(event.get("timestamp"), "timestamp");
        long value = asLong(event.get("value"), "value");
        return window.add(ts, value);
    }

    private void handleQuantile(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) {
            send(ex, 405, errorBody("method not allowed"));
            return;
        }
        double q = 0.5;
        String query = ex.getRequestURI().getRawQuery();
        if (query != null) {
            for (String pair : query.split("&")) {
                int eq = pair.indexOf('=');
                if (eq > 0 && pair.substring(0, eq).equals("q")) {
                    try {
                        q = Double.parseDouble(pair.substring(eq + 1));
                    } catch (NumberFormatException e) {
                        send(ex, 400, errorBody("bad q: " + pair.substring(eq + 1)));
                        return;
                    }
                }
            }
        }
        if (q < 0.0 || q > 1.0 || Double.isNaN(q)) {
            send(ex, 400, errorBody("q must be in [0,1]"));
            return;
        }
        OptionalDouble value;
        long count;
        synchronized (lock) {
            value = window.quantile(q);
            count = window.count();
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("q", q);
        resp.put("count", count);
        resp.put("value", value.isPresent() ? value.getAsDouble() : null);
        send(ex, 200, Json.write(resp));
    }

    private void handleStats(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) {
            send(ex, 405, errorBody("method not allowed"));
            return;
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        synchronized (lock) {
            resp.put("now", window.now());
            resp.put("windowStart", window.windowStart());
            resp.put("count", window.count());
            resp.put("lateEvents", window.lateEventCount());
        }
        send(ex, 200, Json.write(resp));
    }

    private void handleAdvance(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            send(ex, 405, errorBody("method not allowed"));
            return;
        }
        if (!(clock instanceof com.winquant.ManualClock)) {
            send(ex, 409, errorBody("clock is not manual; /advance is disabled"));
            return;
        }
        try {
            Map<String, Object> body = Json.parseObject(readBody(ex));
            long t = asLong(body.get("now"), "now");
            synchronized (lock) {
                ((com.winquant.ManualClock) clock).advanceTo(t);
                window.refresh();
            }
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("now", t);
            send(ex, 200, Json.write(resp));
        } catch (IllegalArgumentException e) {
            send(ex, 400, errorBody(e.getMessage()));
        }
    }

    private void handleHealth(HttpExchange ex) throws IOException {
        send(ex, 200, "{\"status\":\"ok\"}");
    }

    // ---------- 工具 ----------

    @SuppressWarnings("unchecked")
    private static Map<String, Object> asMap(Object o) {
        if (!(o instanceof Map)) {
            throw new IllegalArgumentException("event must be a JSON object");
        }
        return (Map<String, Object>) o;
    }

    private static long asLong(Object o, String field) {
        if (!(o instanceof Number)) {
            throw new IllegalArgumentException("missing or non-numeric field: " + field);
        }
        double d = ((Number) o).doubleValue();
        if (d != Math.rint(d)) {
            throw new IllegalArgumentException("field must be an integer: " + field);
        }
        return ((Number) o).longValue();
    }

    private static String errorBody(String msg) {
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("error", msg == null ? "bad request" : msg);
        return Json.write(resp);
    }

    private static String readBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static void send(HttpExchange ex, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(bytes);
        }
    }
}
