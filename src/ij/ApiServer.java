package ij;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * 基于 JDK 内置 com.sun.net.httpserver 的 HTTP 接口。
 * 单线程 executor 串行处理所有请求，引擎内部无需额外锁竞争。
 */
final class ApiServer {

    private final SessionRegistry registry;
    private final HttpServer server;

    ApiServer(SessionRegistry registry, String host, int port) throws IOException {
        this.registry = registry;
        this.server = HttpServer.create(new InetSocketAddress(host, port), 0);
        server.createContext("/", this::handle);
        server.setExecutor(Executors.newSingleThreadExecutor(r -> {
            Thread t = new Thread(r, "ij-http");
            t.setDaemon(true);
            return t;
        }));
    }

    int port() {
        return server.getAddress().getPort();
    }

    void start() {
        server.start();
    }

    void stop() {
        server.stop(1);
    }

    // ---------------- 路由 ----------------

    private void handle(HttpExchange ex) throws IOException {
        try {
            route(ex);
        } catch (ApiError err) {
            writeJson(ex, err.status, Map.of("error", err.getMessage()));
        } catch (Json.JsonException err) {
            writeJson(ex, 400, Map.of("error", "invalid JSON: " + err.getMessage()));
        } catch (IllegalArgumentException err) {
            writeJson(ex, 400, Map.of("error", err.getMessage()));
        } catch (Exception err) {
            writeJson(ex, 500, Map.of("error", "internal error: " + err));
        } finally {
            ex.close();
        }
    }

    private void route(HttpExchange ex) throws IOException {
        String method = ex.getRequestMethod();
        String path = ex.getRequestURI().getPath();
        // 去掉尾部斜号（根除外）。
        if (path.length() > 1 && path.endsWith("/")) {
            path = path.substring(0, path.length() - 1);
        }
        String[] seg = path.split("/");
        // seg[0] 为空串。

        if ("GET".equals(method) && "/health".equals(path)) {
            writeJson(ex, 200, Map.of("status", "ok"));
            return;
        }
        if ("GET".equals(method) && "/sessions".equals(path)) {
            writeJson(ex, 200, Map.of("sessions", registry.list()));
            return;
        }

        if ("POST".equals(method) && "/sessions".equals(path)) {
            createSession(ex);
            return;
        }

        // /sessions/{id}...
        if (seg.length >= 3 && "sessions".equals(seg[1])) {
            String id = decode(seg[2]);
            JoinSession session = registry.get(id);
            if (session == null) {
                throw new ApiError(404, "session not found: " + id);
            }

            if (seg.length == 3 && "GET".equals(method)) {
                writeJson(ex, 200, session.snapshotStats());
                return;
            }
            if (seg.length == 3 && "DELETE".equals(method)) {
                registry.delete(id);
                writeJson(ex, 200, Map.of("deleted", id));
                return;
            }
            if (seg.length == 4 && "stats".equals(seg[3]) && "GET".equals(method)) {
                writeJson(ex, 200, session.snapshotStats());
                return;
            }
            if (seg.length == 4 && "pairs".equals(seg[3]) && "GET".equals(method)) {
                listPairs(ex, session);
                return;
            }
            if (seg.length == 5 && "events".equals(seg[3])
                    && ("left".equals(seg[4]) || "right".equals(seg[4]))
                    && "POST".equals(method)) {
                pushEvents(ex, session, seg[4]);
                return;
            }
            if (seg.length == 5 && "watermarks".equals(seg[3])
                    && ("left".equals(seg[4]) || "right".equals(seg[4]))
                    && "POST".equals(method)) {
                pushWatermark(ex, session, seg[4]);
                return;
            }
            if (seg.length == 4 && "actions".equals(seg[3]) && "POST".equals(method)) {
                runActions(ex, session);
                return;
            }
        }

        throw new ApiError(404, "no route: " + method + " " + path);
    }

    // ---------------- 具体处理器 ----------------

    private void createSession(HttpExchange ex) throws IOException {
        Map<String, Object> body = Json.parseObject(readBody(ex));
        String id = Json.requireString(body, "sessionId");
        long lower = Json.optionalLong(body, "lowerBound", -2L);
        long upper = Json.optionalLong(body, "upperBound", 2L);
        try {
            JoinSession session = registry.create(id, lower, upper);
            writeJson(ex, 201, session.snapshotStats());
        } catch (IllegalStateException err) {
            throw new ApiError(409, err.getMessage());
        }
    }

    private void pushEvents(HttpExchange ex, JoinSession session, String side) throws IOException {
        Object raw = Json.parse(readBody(ex));
        List<Map<String, Object>> events = new ArrayList<>();
        if (raw instanceof List) {
            for (Object item : (List<?>) raw) {
                events.add(Json.asObject(item, "events[]"));
            }
        } else {
            events.add(Json.asObject(raw, "body"));
        }
        List<Object> results = new ArrayList<>();
        for (Map<String, Object> ev : events) {
            String key = Json.requireString(ev, "key");
            long ts = Json.requireLong(ev, "ts");
            String clientId = Json.optionalString(ev, "id");
            Object payload = ev.get("payload");
            EventResult r = session.pushEvent(side, key, ts, clientId, payload);
            results.add(renderEventResult(r));
        }
        writeJson(ex, 200, Map.of("results", results, "stats", session.snapshotStats()));
    }

    private void pushWatermark(HttpExchange ex, JoinSession session, String side) throws IOException {
        Map<String, Object> body = Json.parseObject(readBody(ex));
        long wm = Json.requireLong(body, "watermark");
        int reclaimed = session.advanceWatermark(side, wm);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("side", side);
        resp.put("reclaimedThisCall", reclaimed);
        resp.put("stats", session.snapshotStats());
        writeJson(ex, 200, resp);
    }

    private void runActions(HttpExchange ex, JoinSession session) throws IOException {
        Map<String, Object> body = Json.parseObject(readBody(ex));
        List<Object> actions = Json.asArray(body.get("actions"), "actions");
        boolean includePayload = queryParams(ex).getOrDefault("includePayload", "false").equals("true");
        List<Object> rendered = new ArrayList<>();
        List<Object> newPairsAll = new ArrayList<>();
        for (Object item : actions) {
            Map<String, Object> a = Json.asObject(item, "actions[]");
            String type = Json.requireString(a, "type");
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("type", type);
            switch (type) {
                case "event": {
                    String side = Json.requireString(a, "side");
                    String key = Json.requireString(a, "key");
                    long ts = Json.requireLong(a, "ts");
                    String clientId = Json.optionalString(a, "id");
                    Object payload = a.get("payload");
                    EventResult r = session.pushEvent(side, key, ts, clientId, payload);
                    out.put("eventId", r.eventId);
                    out.put("droppedLate", r.dropped);
                    List<Object> pairList = new ArrayList<>();
                    for (Pair p : r.newPairs) {
                        pairList.add(renderPair(p, includePayload));
                    }
                    out.put("newPairs", pairList);
                    newPairsAll.addAll(pairList);
                    break;
                }
                case "watermark": {
                    String side = Json.requireString(a, "side");
                    long wm = Json.requireLong(a, "watermark");
                    int reclaimed = session.advanceWatermark(side, wm);
                    out.put("side", side);
                    out.put("reclaimedThisCall", reclaimed);
                    break;
                }
                default:
                    throw new ApiError(400, "unknown action type: " + type);
            }
            rendered.add(out);
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("actions", rendered);
        resp.put("newPairs", newPairsAll);
        resp.put("stats", session.snapshotStats());
        writeJson(ex, 200, resp);
    }

    private void listPairs(HttpExchange ex, JoinSession session) throws IOException {
        boolean includePayload = queryParams(ex).getOrDefault("includePayload", "false").equals("true");
        String keyFilter = queryParams(ex).get("key");
        List<Object> list = new ArrayList<>();
        for (Pair p : session.pairs()) {
            if (keyFilter != null && !keyFilter.equals(p.left.key)) {
                continue;
            }
            list.add(renderPair(p, includePayload));
        }
        writeJson(ex, 200, Map.of("pairs", list, "count", list.size()));
    }

    // ---------------- 渲染辅助 ----------------

    private static Map<String, Object> renderEventResult(EventResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("eventId", r.eventId);
        m.put("droppedLate", r.dropped);
        m.put("newPairs", r.newPairs.size());
        return m;
    }

    private static Map<String, Object> renderPair(Pair p, boolean includePayload) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("key", p.left.key);
        m.put("leftId", p.left.id);
        m.put("leftTs", p.left.ts);
        m.put("rightId", p.right.id);
        m.put("rightTs", p.right.ts);
        m.put("tsDiff", p.right.ts - p.left.ts);
        if (includePayload) {
            m.put("leftPayload", p.left.payload);
            m.put("rightPayload", p.right.payload);
        }
        return m;
    }

    // ---------------- HTTP 基础 ----------------

    private static String readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private static void writeJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] data = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(data);
        }
    }

    private static Map<String, String> queryParams(HttpExchange ex) {
        Map<String, String> m = new LinkedHashMap<>();
        String raw = ex.getRequestURI().getRawQuery();
        if (raw == null) {
            return m;
        }
        for (String kv : raw.split("&")) {
            int eq = kv.indexOf('=');
            if (eq > 0) {
                m.put(decode(kv.substring(0, eq)), decode(kv.substring(eq + 1)));
            }
        }
        return m;
    }

    private static String decode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private static final class ApiError extends RuntimeException {
        final int status;

        ApiError(int status, String message) {
            super(message);
            this.status = status;
        }
    }
}
