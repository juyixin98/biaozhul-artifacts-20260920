package intervalindex;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;

/**
 * 基于 JDK 内置 {@link HttpServer} 的时间区间检索服务（纯后端，无第三方依赖）。
 *
 * <h2>接口（均为 JSON）</h2>
 * <pre>
 * POST   /intervals              插入区间      {"lo":1,"hi":5,"count":1?}
 * DELETE /intervals              删除区间      {"lo":1,"hi":5,"count":1?}
 * POST   /intervals/overlap      交集查询      {"lo":2,"hi":6}
 * GET    /intervals/coverage?t=3 指定时刻覆盖计数（t 也可放 body）
 * GET    /intervals              列出全部不同区间
 * GET    /health                 健康检查
 * </pre>
 *
 * 端点为整数，统一左闭右开 [lo, hi)；拒绝空区间与逆序区间。
 */
public final class HttpServerApp {

    private static final int MAX_BODY_BYTES = 1 << 20; // 1 MiB
    private static final int HTTP_OK = 200;
    private static final int HTTP_BAD_REQUEST = 400;
    private static final int HTTP_NOT_FOUND = 404;
    private static final int HTTP_METHOD_NOT_ALLOWED = 405;
    private static final int HTTP_INTERNAL = 500;

    private final IntervalStore store;
    private HttpServer server;

    public HttpServerApp(IntervalStore store) {
        this.store = store;
    }

    /** 启动并返回实际绑定端口（port=0 时由系统分配）。 */
    public int start(String host, int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress(host, port), 0);
        server.createContext("/", this::route);
        ThreadPoolExecutor pool = (ThreadPoolExecutor) Executors.newFixedThreadPool(8, r -> {
            Thread t = new Thread(r, "interval-http");
            t.setDaemon(true);
            return t;
        });
        server.setExecutor(pool);
        server.start();
        return server.getAddress().getPort();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
    }

    // ------------------------------------------------------------------

    private void route(HttpExchange ex) {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (path) {
                case "/health" -> {
                    if ("GET".equals(method)) {
                        sendJson(ex, HTTP_OK, Json.object("status", "ok"));
                    } else {
                        sendJson(ex, HTTP_METHOD_NOT_ALLOWED,
                                Json.object("error", method + " not allowed on /health"));
                    }
                }
                case "/intervals" -> handleCollection(ex, method);
                case "/intervals/overlap" -> {
                    if ("POST".equals(method)) {
                        handleOverlap(ex);
                    } else {
                        sendJson(ex, HTTP_METHOD_NOT_ALLOWED,
                                Json.object("error", method + " not allowed on /intervals/overlap"));
                    }
                }
                case "/intervals/coverage" -> {
                    if ("GET".equals(method) || "POST".equals(method)) {
                        handleCoverage(ex);
                    } else {
                        sendJson(ex, HTTP_METHOD_NOT_ALLOWED,
                                Json.object("error", method + " not allowed on /intervals/coverage"));
                    }
                }
                default -> sendJson(ex, HTTP_NOT_FOUND,
                        Json.object("error", "no such endpoint: " + path));
            }
        } catch (BadRequestException | Json.ParseException bre) {
            sendJson(ex, HTTP_BAD_REQUEST, Json.object("error", bre.getMessage()));
        } catch (Exception e) {
            sendJson(ex, HTTP_INTERNAL, Json.object("error", "internal error: " + e.getMessage()));
        }
    }

    private void handleCollection(HttpExchange ex, String method) throws IOException {
        switch (method) {
            case "POST" -> {
                Map<String, Object> body = readJsonObject(ex);
                long lo = Json.requireLong(body, "lo");
                long hi = Json.requireLong(body, "hi");
                long count = Json.optionalLong(body, "count", 1);
                if (count <= 0) {
                    throw new BadRequestException("\"count\" must be a positive integer");
                }
                try {
                    long current = 0;
                    for (long i = 0; i < count; i++) {
                        current = store.insert(lo, hi);
                    }
                    sendJson(ex, HTTP_OK, Json.object(
                            "inserted", count,
                            "lo", lo, "hi", hi,
                            "currentCount", current,
                            "distinctIntervals", store.distinctCount(),
                            "totalIntervals", store.totalCount()));
                } catch (IllegalArgumentException iae) {
                    throw new BadRequestException(iae.getMessage());
                }
            }
            case "DELETE" -> {
                long lo;
                long hi;
                long count;
                Map<String, String> query = parseQuery(ex.getRequestURI().getRawQuery());
                if (query.containsKey("lo") || query.containsKey("hi")) {
                    // 允许 DELETE ?lo=..&hi=..&count=..
                    try {
                        lo = Long.parseLong(query.get("lo"));
                        hi = Long.parseLong(query.get("hi"));
                    } catch (NumberFormatException nfe) {
                        throw new BadRequestException("query parameter lo/hi must be integers");
                    }
                    String c = query.getOrDefault("count", "1");
                    try {
                        count = Long.parseLong(c);
                    } catch (NumberFormatException nfe) {
                        throw new BadRequestException("query parameter count must be an integer");
                    }
                } else {
                    Map<String, Object> body = readJsonObject(ex);
                    lo = Json.requireLong(body, "lo");
                    hi = Json.requireLong(body, "hi");
                    count = Json.optionalLong(body, "count", 1);
                }
                if (count <= 0) {
                    throw new BadRequestException("\"count\" must be a positive integer");
                }
                try {
                    long removed = store.delete(lo, hi, count);
                    if (removed == 0) {
                        sendJson(ex, HTTP_NOT_FOUND, Json.object(
                                "error", "interval [%d, %d) does not exist".formatted(lo, hi),
                                "removed", 0L));
                    } else {
                        sendJson(ex, HTTP_OK, Json.object(
                                "removed", removed,
                                "lo", lo, "hi", hi,
                                "remainingCount", store.all().stream()
                                        .filter(e -> e.lo() == lo && e.hi() == hi)
                                        .mapToLong(IntervalStore.Entry::count).findFirst().orElse(0),
                                "distinctIntervals", store.distinctCount(),
                                "totalIntervals", store.totalCount()));
                    }
                } catch (IllegalArgumentException iae) {
                    throw new BadRequestException(iae.getMessage());
                }
            }
            case "GET" -> {
                List<IntervalStore.Entry> entries = store.all();
                List<Object> arr = new ArrayList<>();
                for (IntervalStore.Entry e : entries) {
                    arr.add(Json.object("lo", e.lo(), "hi", e.hi(), "count", e.count()));
                }
                sendJson(ex, HTTP_OK, Json.object(
                        "intervals", arr,
                        "distinctIntervals", store.distinctCount(),
                        "totalIntervals", store.totalCount()));
            }
            default -> sendJson(ex, HTTP_METHOD_NOT_ALLOWED,
                    Json.object("error", method + " not allowed on /intervals"));
        }
    }

    private void handleOverlap(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonObject(ex);
        long lo = Json.requireLong(body, "lo");
        long hi = Json.requireLong(body, "hi");
        try {
            List<Interval> hits = store.queryOverlap(lo, hi);
            // 聚合重复区间
            Map<String, Map<String, Object>> grouped = new LinkedHashMap<>();
            for (Interval iv : hits) {
                String key = iv.lo() + ":" + iv.hi();
                Map<String, Object> g = grouped.computeIfAbsent(key, k -> {
                    Map<String, Object> m = new LinkedHashMap<>();
                    m.put("lo", iv.lo());
                    m.put("hi", iv.hi());
                    m.put("count", 0L);
                    return m;
                });
                g.put("count", (Long) g.get("count") + 1);
            }
            sendJson(ex, HTTP_OK, Json.object(
                    "query", Json.object("lo", lo, "hi", hi),
                    "overlaps", new ArrayList<>(grouped.values()),
                    "matchCount", (long) hits.size()));
        } catch (IllegalArgumentException iae) {
            throw new BadRequestException(iae.getMessage());
        }
    }

    private void handleCoverage(HttpExchange ex) throws IOException {
        long t;
        Map<String, String> query = parseQuery(ex.getRequestURI().getRawQuery());
        if (query.containsKey("t")) {
            try {
                t = Long.parseLong(query.get("t"));
            } catch (NumberFormatException nfe) {
                throw new BadRequestException("query parameter t must be an integer");
            }
        } else if ("POST".equals(ex.getRequestMethod())) {
            Map<String, Object> body = readJsonObject(ex);
            t = Json.requireLong(body, "t");
        } else {
            throw new BadRequestException("missing query parameter t, e.g. /intervals/coverage?t=3");
        }
        sendJson(ex, HTTP_OK, Json.object("t", t, "coverage", store.coverage(t)));
    }

    // ------------------------------------------------------------------
    // HTTP 小工具
    // ------------------------------------------------------------------

    @SuppressWarnings("serial")
    private static final class BadRequestException extends RuntimeException {
        BadRequestException(String msg) {
            super(msg);
        }
    }

    private Map<String, Object> readJsonObject(HttpExchange ex) throws IOException {
        byte[] raw = ex.getRequestBody().readNBytes(MAX_BODY_BYTES + 1);
        if (raw.length > MAX_BODY_BYTES) {
            throw new BadRequestException("request body too large (limit 1 MiB)");
        }
        String text = new String(raw, StandardCharsets.UTF_8).trim();
        if (text.isEmpty()) {
            throw new BadRequestException("request body is empty; expected JSON object");
        }
        try {
            return Json.parseObject(text);
        } catch (Json.ParseException pe) {
            throw new BadRequestException("invalid JSON: " + pe.getMessage());
        }
    }

    private void sendJson(HttpExchange ex, int status, Object payload) {
        byte[] data = Json.write(payload).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        try (OutputStream os = ex.getResponseBody()) {
            ex.sendResponseHeaders(status, data.length);
            os.write(data);
        } catch (IOException ioe) {
            // 客户端断开等：无法再写响应，直接放弃
        }
    }

    private static Map<String, String> parseQuery(String rawQuery) {
        Map<String, String> map = new LinkedHashMap<>();
        if (rawQuery == null || rawQuery.isEmpty()) {
            return map;
        }
        for (String pair : rawQuery.split("&")) {
            int eq = pair.indexOf('=');
            String k = eq < 0 ? pair : pair.substring(0, eq);
            String v = eq < 0 ? "" : pair.substring(eq + 1);
            map.put(URLDecoder.decode(k, StandardCharsets.UTF_8),
                    URLDecoder.decode(v, StandardCharsets.UTF_8));
        }
        return map;
    }

    // ------------------------------------------------------------------

    public static void main(String[] args) throws Exception {
        String host = System.getenv().getOrDefault("BIND_HOST", "127.0.0.1");
        int port = 8080;
        String portEnv = System.getenv("PORT");
        if (portEnv != null && !portEnv.isBlank()) {
            port = Integer.parseInt(portEnv);
        }
        if (args.length >= 1) {
            port = Integer.parseInt(args[0]);
        }
        if (args.length >= 2) {
            host = args[1];
        }

        HttpServerApp app = new HttpServerApp(new IntervalStore());
        int bound = app.start(host, port);
        System.out.printf("Interval overlap index listening on http://%s:%d%n", host, bound);
        System.out.println("Endpoints: POST/GET/DELETE /intervals, POST /intervals/overlap, "
                + "GET /intervals/coverage?t=, GET /health");

        // 优雅关闭：SIGTERM/SIGINT（HttpServer 已在 daemon 线程上，这里补一个关闭钩子）
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("Shutting down...");
            app.stop();
        }));

        Thread.currentThread().join();
    }
}
