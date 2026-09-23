package com.example.intervalindex.http;

import com.example.intervalindex.core.Interval;
import com.example.intervalindex.core.IntervalIndex;
import com.example.intervalindex.json.Json;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 基于 JDK 内置 {@link HttpServer} 的 HTTP 服务，无任何第三方依赖。
 *
 * <p>路由：
 * <pre>
 *  POST   /intervals             插入区间   {"start":1,"end":5}
 *  DELETE /intervals/{id}        按 id 删除
 *  GET    /intervals?start=&end= 查询与 [start,end) 相交的全部区间
 *  GET    /intervals/count?at=t  统计覆盖时刻 t 的区间数
 *  GET    /intervals/all         导出全部区间
 *  GET    /health                健康检查
 * </pre>
 */
public class IntervalServer {

    private final IntervalIndex index = new IntervalIndex();
    private final HttpServer server;
    private final ExecutorService pool;

    public IntervalServer(int port) throws IOException {
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        server.createContext("/health", this::handleHealth);
        server.createContext("/intervals", this::handleIntervals);
        AtomicInteger threadNo = new AtomicInteger();
        ThreadFactory daemonFactory = r -> {
            Thread t = new Thread(r, "http-worker-" + threadNo.incrementAndGet());
            t.setDaemon(true);
            return t;
        };
        this.pool = Executors.newFixedThreadPool(8, daemonFactory);
        server.setExecutor(pool);
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
        pool.shutdownNow();
    }

    public int getPort() {
        return server.getAddress().getPort();
    }

    // ------------------------------------------------------------------

    private void handleHealth(HttpExchange ex) throws IOException {
        writeJson(ex, 200, Json.object("status", "ok", "size", index.size()));
    }

    private void handleIntervals(HttpExchange ex) throws IOException {
        try {
            String method = ex.getRequestMethod();
            String path = ex.getRequestURI().getPath();
            Map<String, String> params = parseQuery(ex.getRequestURI().getRawQuery());

            if ("POST".equals(method) && path.equals("/intervals")) {
                handleInsert(ex);
            } else if ("DELETE".equals(method) && path.startsWith("/intervals/")) {
                handleDelete(ex, path.substring("/intervals/".length()));
            } else if ("GET".equals(method) && path.equals("/intervals")) {
                handleOverlapQuery(ex, params);
            } else if ("GET".equals(method) && path.equals("/intervals/count")) {
                handleCount(ex, params);
            } else if ("GET".equals(method) && path.equals("/intervals/all")) {
                handleAll(ex);
            } else {
                writeError(ex, 404, "not found: " + method + " " + path);
            }
        } catch (IllegalArgumentException e) {
            writeError(ex, 400, e.getMessage());
        } catch (Exception e) {
            writeError(ex, 500, "internal error: " + e);
        }
    }

    private void handleInsert(HttpExchange ex) throws IOException {
        Object parsed = Json.parse(readBody(ex));
        long start = Json.longField(parsed, "start");
        long end = Json.longField(parsed, "end");
        long id = index.insert(start, end);
        writeJson(ex, 201, Json.object("id", id, "start", start, "end", end));
    }

    private void handleDelete(HttpExchange ex, String idRaw) throws IOException {
        long id;
        try {
            id = Long.parseLong(idRaw);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("id must be an integer: " + idRaw);
        }
        boolean deleted = index.delete(id);
        if (!deleted) {
            writeError(ex, 404, "no interval with id " + id);
            return;
        }
        writeJson(ex, 200, Json.object("deleted", true, "id", id, "size", index.size()));
    }

    private void handleOverlapQuery(HttpExchange ex, Map<String, String> params) throws IOException {
        long lo = requireLongParam(params, "start");
        long hi = requireLongParam(params, "end");
        if (lo >= hi) {
            throw new IllegalArgumentException(
                    "invalid query [" + lo + ", " + hi + "): require start < end");
        }
        List<Interval> hits = index.findOverlaps(lo, hi);
        writeJson(ex, 200, Json.object(
                "query", Json.object("start", lo, "end", hi),
                "count", hits.size(),
                "intervals", toJsonList(hits)));
    }

    private void handleCount(HttpExchange ex, Map<String, String> params) throws IOException {
        long t = requireLongParam(params, "at");
        writeJson(ex, 200, Json.object(
                "at", t,
                "count", index.countCovering(t)));
    }

    private void handleAll(HttpExchange ex) throws IOException {
        List<Interval> all = index.toList();
        writeJson(ex, 200, Json.object("count", all.size(), "intervals", toJsonList(all)));
    }

    // ------------------------------------------------------------------
    // 工具方法
    // ------------------------------------------------------------------

    private static List<Map<String, Object>> toJsonList(List<Interval> intervals) {
        return intervals.stream()
                .map(i -> Json.object("id", i.id(), "start", i.start(), "end", i.end()))
                .toList();
    }

    private static long requireLongParam(Map<String, String> params, String name) {
        String v = params.get(name);
        if (v == null || v.isBlank()) {
            throw new IllegalArgumentException("missing query parameter: " + name);
        }
        try {
            return Long.parseLong(v);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("query parameter " + name + " must be an integer");
        }
    }

    static Map<String, String> parseQuery(String raw) {
        Map<String, String> out = new LinkedHashMap<>();
        if (raw == null || raw.isEmpty()) {
            return out;
        }
        for (String pair : raw.split("&")) {
            int eq = pair.indexOf('=');
            String key = eq < 0 ? pair : pair.substring(0, eq);
            String value = eq < 0 ? "" : pair.substring(eq + 1);
            out.put(urlDecode(key), urlDecode(value));
        }
        return out;
    }

    private static String urlDecode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private static String readBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static void writeJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] data = Json.stringify(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(data);
        }
    }

    private static void writeError(HttpExchange ex, int status, String message) throws IOException {
        writeJson(ex, status, Json.object("error", message, "status", status));
    }

    // ------------------------------------------------------------------

    public static void main(String[] args) throws Exception {
        int port = 8080;
        if (args.length > 0) {
            port = Integer.parseInt(args[0]);
        } else {
            String envPort = System.getenv("PORT");
            if (envPort != null && !envPort.isBlank()) {
                port = Integer.parseInt(envPort);
            }
        }
        IntervalServer srv = new IntervalServer(port);
        srv.start();
        System.out.println("Interval index service listening on http://localhost:"
                + srv.getPort());
    }
}
