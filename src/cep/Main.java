package cep;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;

/**
 * 纯 JDK 的 HTTP 服务（com.sun.net.httpserver.HttpServer，JDK 自带，无第三方依赖）。
 *
 * 路由：
 *   GET  /health                         存活检查
 *   POST /events                         写入事件（单个对象或数组）
 *   GET  /matches?entityId=..            查询匹配（可选实体过滤）
 *   GET  /state?entityId=..              查询部分匹配状态（可选实体过滤）
 *   POST /reset                          清空内存状态与预写日志
 *   POST /test/crash                     仅 --test-endpoints 时可用：模拟崩溃
 *
 * 写入与崩溃的顺序保证：先 fsync 预写日志，再修改内存状态，最后返回 200。
 * 因此凡是客户端看到 200 的批次，重启后一定仍在。
 */
public final class Main {

    private static final int MAX_BODY_BYTES = 16 * 1024 * 1024;

    private final Engine engine;
    private final EventLog eventLog;
    private final boolean testEndpoints;
    private HttpServer server;

    Main(Engine engine, EventLog eventLog, boolean testEndpoints) {
        this.engine = engine;
        this.eventLog = eventLog;
        this.testEndpoints = testEndpoints;
    }

    /** 启动并返回实际监听端口（传 0 时由系统分配，测试用）。 */
    int start(int port) {
        try {
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
            server.createContext("/", this::dispatch);
            ThreadFactory tf = r -> {
                Thread t = new Thread(r, "cep-http");
                t.setDaemon(true);
                return t;
            };
            server.setExecutor(Executors.newFixedThreadPool(8, tf));
            server.start();
            return server.getAddress().getPort();
        } catch (IOException e) {
            throw new UncheckedIOException("HTTP 服务启动失败", e);
        }
    }

    void stop(int delaySeconds) {
        if (server != null) {
            server.stop(delaySeconds);
        }
    }

    // ------------------------------------------------------------ 路由分发

    private void dispatch(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (path) {
                case "/health":
                    handleHealth(ex, method);
                    break;
                case "/events":
                    handleEvents(ex, method);
                    break;
                case "/matches":
                    handleMatches(ex, method);
                    break;
                case "/state":
                    handleState(ex, method);
                    break;
                case "/reset":
                    handleReset(ex, method);
                    break;
                case "/test/crash":
                    handleTestCrash(ex, method);
                    break;
                default:
                    sendError(ex, 404, "not_found", "未知路径: " + path);
            }
        } catch (Exception e) {
            sendError(ex, 500, "internal_error", e.toString());
        }
    }

    private void handleHealth(HttpExchange ex, String method) throws IOException {
        if (!"GET".equals(method)) {
            sendError(ex, 405, "method_not_allowed", "仅支持 GET");
            return;
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "ok");
        body.put("totalMatches", engine.totalMatches());
        body.put("nextSeq", engine.nextSeq());
        sendJson(ex, 200, body);
    }

    private void handleEvents(HttpExchange ex, String method) throws IOException {
        if (!"POST".equals(method)) {
            sendError(ex, 405, "method_not_allowed", "仅支持 POST");
            return;
        }
        String raw = readBody(ex);
        final Object parsed;
        try {
            parsed = Json.parse(raw);
        } catch (Json.JsonException e) {
            sendError(ex, 400, "invalid_json", e.getMessage());
            return;
        }

        List<Map<String, Object>> items = new ArrayList<>();
        if (parsed instanceof List) {
            for (Object o : (List<?>) parsed) {
                requireEventObject(o);
                @SuppressWarnings("unchecked")
                Map<String, Object> m = (Map<String, Object>) o;
                items.add(m);
            }
        } else if (parsed instanceof Map) {
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) parsed;
            items.add(m);
        } else {
            sendError(ex, 400, "invalid_request",
                    "请求体必须是事件对象或事件数组，例如 {\"type\":\"A\",\"entityId\":\"e1\",\"timestamp\":0}");
            return;
        }
        if (items.isEmpty()) {
            sendError(ex, 400, "invalid_request", "事件数组不能为空");
            return;
        }

        List<Event> events = new ArrayList<>(items.size());
        for (Map<String, Object> m : items) {
            try {
                events.add(toEvent(m));
            } catch (IllegalArgumentException e) {
                sendError(ex, 400, "invalid_event", e.getMessage());
                return;
            }
        }

        // 两阶段写入：先在引擎状态副本上预检（晚到/组合上限），通过后把批次
        // 追加到预写日志并 fsync，最后才提交内存状态。因此返回 200 的批次
        // 一定已经落盘，崩溃后可完整重放。
        final Engine.IngestResult result;
        try {
            result = ingestWithLog(events);
        } catch (Engine.LateEventException e) {
            sendError(ex, 409, "late_event", e.getMessage());
            return;
        } catch (Engine.CombinationLimitException e) {
            sendError(ex, 422, "combination_limit", e.getMessage());
            return;
        } catch (UncheckedIOException e) {
            sendError(ex, 500, "wal_write_failed",
                    "预写日志写入失败，本批次未生效: " + e.getMessage());
            return;
        }

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("accepted", result.accepted.size());
        body.put("created", result.created.size());
        body.put("totalMatches", result.totalMatches);
        List<Object> acceptedViews = new ArrayList<>(result.accepted.size());
        for (Event ev : result.accepted) {
            acceptedViews.add(eventView(ev));
        }
        body.put("events", acceptedViews);
        body.put("newMatches", matchList(result.created));
        sendJson(ex, 200, body);
    }

    /**
     * 两阶段写入：
     * 1. 引擎在状态副本上预检，准备“序号已分配的事件”与“将产生的匹配”；
     * 2. 预写日志追加并 fsync；
     * 3. 提交到引擎内存状态。
     */
    private Engine.IngestResult ingestWithLog(List<Event> events) {
        Engine.PreparedBatch prepared = engine.ingestDryRun(events);
        eventLog.appendBatch(prepared.assigned);
        return engine.commit(prepared);
    }

    private void handleMatches(HttpExchange ex, String method) throws IOException {
        if (!"GET".equals(method)) {
            sendError(ex, 405, "method_not_allowed", "仅支持 GET");
            return;
        }
        String entityId = queryParam(ex, "entityId");
        List<Match> list = (entityId == null)
                ? engine.queryMatches()
                : engine.queryMatches(entityId);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", list.size());
        body.put("matches", matchList(list));
        sendJson(ex, 200, body);
    }

    private void handleState(HttpExchange ex, String method) throws IOException {
        if (!"GET".equals(method)) {
            sendError(ex, 405, "method_not_allowed", "仅支持 GET");
            return;
        }
        String entityId = queryParam(ex, "entityId");
        Map<String, Object> body = new LinkedHashMap<>();
        if (entityId != null) {
            body.put("entities", List.of(stateView(engine.viewState(entityId))));
        } else {
            List<Object> all = new ArrayList<>();
            for (Engine.EntityStateView v : engine.viewAllStates()) {
                all.add(stateView(v));
            }
            body.put("entities", all);
        }
        sendJson(ex, 200, body);
    }

    private void handleReset(HttpExchange ex, String method) throws IOException {
        if (!"POST".equals(method)) {
            sendError(ex, 405, "method_not_allowed", "仅支持 POST");
            return;
        }
        engine.resetAll();
        eventLog.reset();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "reset");
        sendJson(ex, 200, body);
    }

    private void handleTestCrash(HttpExchange ex, String method) throws IOException {
        if (!"POST".equals(method)) {
            sendError(ex, 405, "method_not_allowed", "仅支持 POST");
            return;
        }
        if (!testEndpoints) {
            sendError(ex, 404, "not_found",
                    "测试端点未开启（需要启动参数 --test-endpoints）");
            return;
        }
        sendJson(ex, 200, Map.of("status", "crashing"));
        // 在独立线程里硬退出，给响应一点时间刷出；halt 不执行 shutdown hook，
        // 模拟真实的 kill -9 / 断电（已 fsync 的日志不受影响）。
        Thread crasher = new Thread(() -> {
            try {
                Thread.sleep(200);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
            }
            System.err.println("[test-crash] 模拟硬崩溃 Runtime.getRuntime().halt(1)");
            Runtime.getRuntime().halt(1);
        }, "test-crash");
        crasher.setDaemon(true);
        crasher.start();
    }

    // ------------------------------------------------------------ 编解码辅助

    private static Event toEvent(Map<String, Object> m) {
        Object type = m.get("type");
        Object entityId = m.get("entityId");
        Object ts = m.get("timestamp");
        if (!(type instanceof String) || ((String) type).isEmpty()) {
            throw new IllegalArgumentException("字段 type 必须是非空字符串");
        }
        if (!(entityId instanceof String) || ((String) entityId).isEmpty()) {
            throw new IllegalArgumentException("字段 entityId 必须是非空字符串");
        }
        if (!(ts instanceof Number)) {
            throw new IllegalArgumentException(
                    "字段 timestamp 必须是数字（epoch 毫秒）");
        }
        long t = ((Number) ts).longValue();
        if (m.containsKey("seq")) {
            throw new IllegalArgumentException(
                    "字段 seq 由引擎按输入顺序分配，客户端不得指定");
        }
        return new Event((String) type, (String) entityId, t, -1);
    }

    private static void requireEventObject(Object o) {
        if (!(o instanceof Map)) {
            throw new IllegalArgumentException("数组元素必须是事件对象");
        }
    }

    private static Map<String, Object> eventView(Event e) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("type", e.type);
        m.put("entityId", e.entityId);
        m.put("timestamp", e.timestamp);
        m.put("seq", e.seq);
        return m;
    }

    private static Map<String, Object> matchView(Match m) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("entityId", m.entityId());
        out.put("a", eventView(m.a));
        out.put("b", eventView(m.b));
        out.put("c", eventView(m.c));
        out.put("spanMs", m.c.timestamp - m.a.timestamp);
        return out;
    }

    private static List<Object> matchList(List<Match> list) {
        List<Object> out = new ArrayList<>(list.size());
        for (Match m : list) {
            out.add(matchView(m));
        }
        return out;
    }

    private static Map<String, Object> stateView(Engine.EntityStateView v) {
        List<Object> as = new ArrayList<>();
        for (Event e : v.waitingAs) {
            as.add(eventView(e));
        }
        List<Object> abs = new ArrayList<>();
        for (Engine.ABView ab : v.waitingABs) {
            Map<String, Object> x = new LinkedHashMap<>();
            x.put("a", eventView(ab.a));
            x.put("b", eventView(ab.b));
            abs.add(x);
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("entityId", v.entityId);
        out.put("watermark", v.watermark == Long.MIN_VALUE ? null : v.watermark);
        out.put("waitingA", as);
        out.put("waitingAB", abs);
        return out;
    }

    // ------------------------------------------------------------ HTTP 原语

    private String readBody(HttpExchange ex) throws IOException {
        long len = ex.getRequestHeaders().getFirst("Content-Length") == null
                ? -1
                : Long.parseLong(ex.getRequestHeaders().getFirst("Content-Length"));
        if (len > MAX_BODY_BYTES) {
            throw new IOException("请求体超过上限 " + MAX_BODY_BYTES + " 字节");
        }
        byte[] bytes = ex.getRequestBody().readAllBytes();
        if (bytes.length > MAX_BODY_BYTES) {
            throw new IOException("请求体超过上限 " + MAX_BODY_BYTES + " 字节");
        }
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private static String queryParam(HttpExchange ex, String name) {
        String q = ex.getRequestURI().getRawQuery();
        if (q == null) {
            return null;
        }
        for (String pair : q.split("&")) {
            int i = pair.indexOf('=');
            String key = i < 0 ? pair : pair.substring(0, i);
            if (key.equals(name)) {
                return i < 0 ? "" : java.net.URLDecoder.decode(
                        pair.substring(i + 1), StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    static void sendJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] payload = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, payload.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(payload);
        }
    }

    static void sendError(HttpExchange ex, int status, String code, String message)
            throws IOException {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", code);
        body.put("message", message);
        sendJson(ex, status, body);
    }

    // ------------------------------------------------------------ main

    public static void main(String[] args) throws Exception {
        int port = 8080;
        String dataDir = "data";
        boolean testEndpoints = false;
        int maxCombines = 100_000;

        for (String arg : args) {
            if (arg.startsWith("--port=")) {
                port = Integer.parseInt(arg.substring("--port=".length()));
            } else if (arg.startsWith("--data-dir=")) {
                dataDir = arg.substring("--data-dir=".length());
            } else if (arg.equals("--test-endpoints")) {
                testEndpoints = true;
            } else if (arg.startsWith("--max-combines=")) {
                maxCombines = Integer.parseInt(arg.substring("--max-combines=".length()));
            } else if (arg.equals("--help") || arg.equals("-h")) {
                System.out.println("用法: java cep.Main [--port=8080] [--data-dir=data] "
                        + "[--max-combines=100000] [--test-endpoints]");
                return;
            } else {
                System.err.println("未知参数: " + arg);
                System.exit(2);
            }
        }

        Engine engine = new Engine(maxCombines);
        EventLog log = new EventLog(Path.of(dataDir));

        long t0 = System.nanoTime();
        List<List<Event>> recovered = log.replay();
        engine.recover(recovered);
        long recoveredEvents = recovered.stream().mapToLong(List::size).sum();
        long ms = (System.nanoTime() - t0) / 1_000_000;
        System.out.println("[cep] 故障恢复: 重放 " + recovered.size() + " 个批次 / "
                + recoveredEvents + " 个事件，重建匹配 " + engine.totalMatches()
                + " 个，耗时 " + ms + " ms");

        Main app = new Main(engine, log, testEndpoints);
        int actual = app.start(port);
        System.out.println("[cep] 监听 http://127.0.0.1:" + actual
                + "  (数据目录 " + dataDir + ", 测试端点 "
                + (testEndpoints ? "开启" : "关闭") + ", 组合上限 " + maxCombines + ")");

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("[cep] 收到退出信号，关闭中...");
            app.stop(1);
            log.close();
        }, "cep-shutdown"));

        Thread.currentThread().join();
    }
}
