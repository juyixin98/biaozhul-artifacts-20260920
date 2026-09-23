package intervaljoin;

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
 * 纯 JDK HTTP 服务（com.sun.net.httpserver.HttpServer）。
 *
 * 所有引擎调用都提交到单线程执行器，保证事件与水位严格串行、结果确定。
 *
 * 路由：
 *   GET  /health
 *   POST /config            {"lowerBound":..,"upperBound":..}
 *   POST /events/left       单条 {"key","ts","id"?} 或批量 {"events":[...]}
 *   POST /events/right      同上
 *   POST /watermark/left    {"watermark":..}
 *   POST /watermark/right   {"watermark":..}
 *   GET  /results
 *   GET  /state
 *   POST /reset             {"lowerBound"?..,"upperBound"?..}
 */
public final class ApiServer {

    private final JoinEngine engine;
    private final List<Pair> results = new ArrayList<>();
    private HttpServer server;

    public ApiServer(JoinEngine engine) {
        this.engine = engine;
    }

    public void start(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/", this::handle);
        // 单线程执行器：引擎非线程安全，串行化所有请求
        server.setExecutor(Executors.newSingleThreadExecutor(r -> {
            Thread t = new Thread(r, "join-engine");
            t.setDaemon(true);
            return t;
        }));
        server.start();
    }

    public void stop() {
        if (server != null) server.stop(0);
    }

    public int getAddressPort() {
        return server == null ? -1 : server.getAddress().getPort();
    }

    private void handle(HttpExchange ex) throws IOException {
        try {
            route(ex);
        } catch (ApiException ae) {
            writeJson(ex, ae.status, errorBody(ae.getMessage()));
        } catch (IllegalArgumentException | IllegalStateException e) {
            // 引擎的参数/状态校验错误属于客户端错误
            writeJson(ex, 400, errorBody(e.getMessage()));
        } catch (Exception e) {
            writeJson(ex, 500, errorBody(e.getClass().getSimpleName() + ": " + e.getMessage()));
        } finally {
            ex.close();
        }
    }

    private void route(HttpExchange ex) throws IOException {
        String path = ex.getRequestURI().getPath();
        String method = ex.getRequestMethod();

        if (path.equals("/health") && method.equals("GET")) {
            writeJson(ex, 200, Map.of("status", "ok"));
            return;
        }
        if (path.equals("/config") && method.equals("POST")) {
            Map<String, Object> body = readBody(ex);
            long lower = requireLong(body, "lowerBound");
            long upper = requireLong(body, "upperBound");
            engine.configure(lower, upper);
            writeJson(ex, 200, configBody());
            return;
        }
        if (path.equals("/events/left") && method.equals("POST")) {
            ingest(ex, "left");
            return;
        }
        if (path.equals("/events/right") && method.equals("POST")) {
            ingest(ex, "right");
            return;
        }
        if (path.equals("/watermark/left") && method.equals("POST")) {
            watermark(ex, true);
            return;
        }
        if (path.equals("/watermark/right") && method.equals("POST")) {
            watermark(ex, false);
            return;
        }
        if (path.equals("/results") && method.equals("GET")) {
            writeJson(ex, 200, resultsBody());
            return;
        }
        if (path.equals("/state") && method.equals("GET")) {
            writeJson(ex, 200, stateBody());
            return;
        }
        if (path.equals("/reset") && method.equals("POST")) {
            engine.reset();
            results.clear();
            Map<String, Object> body;
            String raw = readRaw(ex);
            if (!raw.isEmpty()) {
                body = Json.parseObject(raw);
                if (body.containsKey("lowerBound") || body.containsKey("upperBound")) {
                    long lower = body.containsKey("lowerBound")
                            ? asLong(body.get("lowerBound")) : engine.getLowerBound();
                    long upper = body.containsKey("upperBound")
                            ? asLong(body.get("upperBound")) : engine.getUpperBound();
                    engine.configure(lower, upper);
                }
            }
            writeJson(ex, 200, stateBody());
            return;
        }
        throw new ApiException(404, "no route: " + method + " " + path);
    }

    private void ingest(HttpExchange ex, String side) throws IOException {
        Map<String, Object> body = readBody(ex);
        List<Map<String, Object>> rawEvents = new ArrayList<>();
        if (body.containsKey("events")) {
            for (Object o : asList(body.get("events"), "events")) {
                rawEvents.add(asObject(o, "events[]"));
            }
        } else {
            rawEvents.add(body);
        }
        List<Map<String, Object>> emitted = new ArrayList<>();
        long droppedBefore = "left".equals(side)
                ? engine.getStats().leftLateDropped
                : engine.getStats().rightLateDropped;
        long acceptedBefore = "left".equals(side)
                ? engine.getStats().leftAccepted
                : engine.getStats().rightAccepted;
        for (Map<String, Object> re : rawEvents) {
            String key = requireString(re, "key");
            long ts = requireLong(re, "ts");
            Object id = re.get("id");
            Event e = new Event(side, key, ts, id == null ? null : String.valueOf(id));
            List<Pair> pairs = engine.ingest(e);
            for (Pair p : pairs) {
                results.add(p);
                emitted.add(pairBody(p));
            }
        }
        long droppedAfter = "left".equals(side)
                ? engine.getStats().leftLateDropped
                : engine.getStats().rightLateDropped;
        long acceptedAfter = "left".equals(side)
                ? engine.getStats().leftAccepted
                : engine.getStats().rightAccepted;
        long accepted = acceptedAfter - acceptedBefore;
        long dropped = droppedAfter - droppedBefore;
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("received", rawEvents.size());
        resp.put("accepted", accepted);
        resp.put("lateDropped", dropped);
        resp.put("newPairs", emitted.size());
        resp.put("pairs", emitted);
        writeJson(ex, 200, resp);
    }

    private void watermark(HttpExchange ex, boolean isLeft) throws IOException {
        Map<String, Object> body = readBody(ex);
        long wm = requireLong(body, "watermark");
        long purged = isLeft ? engine.advanceLeftWatermark(wm)
                             : engine.advanceRightWatermark(wm);
        Map<String, Object> resp = stateBody();
        resp.put("purgedThisCall", purged);
        writeJson(ex, 200, resp);
    }

    // ------------------------------------------------------------------
    // 响应体构造
    // ------------------------------------------------------------------

    private Map<String, Object> configBody() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("lowerBound", engine.getLowerBound());
        m.put("upperBound", engine.getUpperBound());
        return m;
    }

    private Map<String, Object> stateBody() {
        Stats s = engine.getStats();
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("config", configBody());
        m.put("leftWatermark", wmValue(engine.getLeftWatermark()));
        m.put("rightWatermark", wmValue(engine.getRightWatermark()));
        long lwm = engine.getLeftWatermark();
        long rwm = engine.getRightWatermark();
        m.put("effectiveWatermark",
                (lwm == Long.MIN_VALUE || rwm == Long.MIN_VALUE)
                        ? null : Math.min(lwm, rwm));
        m.put("retainedEvents", engine.retainedEvents());
        m.put("retainedKeySlots", engine.retainedKeys());
        m.put("totalPairs", results.size());
        m.put("stats", statsBody(s));
        return m;
    }

    private Map<String, Object> statsBody(Stats s) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("leftAccepted", s.leftAccepted);
        m.put("rightAccepted", s.rightAccepted);
        m.put("leftLateDropped", s.leftLateDropped);
        m.put("rightLateDropped", s.rightLateDropped);
        m.put("pairsEmitted", s.pairsEmitted);
        m.put("leftPurged", s.leftPurged);
        m.put("rightPurged", s.rightPurged);
        m.put("leftKeySlotsRemoved", s.leftKeySlotsRemoved);
        m.put("rightKeySlotsRemoved", s.rightKeySlotsRemoved);
        return m;
    }

    private Map<String, Object> resultsBody() {
        List<Object> pairs = new ArrayList<>();
        for (Pair p : results) pairs.add(pairBody(p));
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("totalPairs", pairs.size());
        m.put("pairs", pairs);
        return m;
    }

    private static Map<String, Object> pairBody(Pair p) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("key", p.left.key);
        m.put("leftId", p.left.id);
        m.put("rightId", p.right.id);
        m.put("leftTs", p.left.ts);
        m.put("rightTs", p.right.ts);
        m.put("delta", p.right.ts - p.left.ts);
        return m;
    }

    /** Long.MIN_VALUE 表示“尚未发出水位”，序列化为 null。 */
    private static Object wmValue(long wm) {
        return wm == Long.MIN_VALUE ? null : wm;
    }

    private static Map<String, Object> errorBody(String msg) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("error", msg);
        return m;
    }

    // ------------------------------------------------------------------
    // HTTP 基础
    // ------------------------------------------------------------------

    private static String readRaw(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private static Map<String, Object> readBody(HttpExchange ex) throws IOException {
        String raw = readRaw(ex);
        if (raw.isEmpty()) throw new ApiException(400, "expected JSON request body");
        try {
            return Json.parseObject(raw);
        } catch (RuntimeException e) {
            throw new ApiException(400, e.getMessage());
        }
    }

    private static void writeJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] data = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(data);
        }
    }

    // ------------------------------------------------------------------
    // 参数取值辅助
    // ------------------------------------------------------------------

    private static String requireString(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (!(v instanceof String)) {
            throw new ApiException(400, "missing or non-string field: " + key);
        }
        return (String) v;
    }

    private static long requireLong(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) throw new ApiException(400, "missing field: " + key);
        try {
            return asLong(v);
        } catch (IllegalArgumentException e) {
            throw new ApiException(400, "field " + key + ": " + e.getMessage());
        }
    }

    private static long asLong(Object v) {
        if (v instanceof Number) {
            long l = ((Number) v).longValue();
            if (v instanceof Double && (((Double) v) != l)) {
                throw new IllegalArgumentException("not an integer value");
            }
            return l;
        }
        throw new IllegalArgumentException("expected integer, got " + v);
    }

    @SuppressWarnings("unchecked")
    private static List<Object> asList(Object v, String field) {
        if (!(v instanceof List)) throw new ApiException(400, "field " + field + " must be array");
        return (List<Object>) v;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> asObject(Object v, String field) {
        if (!(v instanceof Map)) throw new ApiException(400, "field " + field + " must be object");
        return (Map<String, Object>) v;
    }

    /** 统一的业务异常，携带 HTTP 状态码。 */
    private static final class ApiException extends RuntimeException {
        private static final long serialVersionUID = 1L;
        final int status;
        ApiException(int status, String msg) {
            super(msg);
            this.status = status;
        }
    }
}
