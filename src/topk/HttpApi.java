package topk;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 基于 JDK 内置 com.sun.net.httpserver 的 HTTP 接口层。
 *
 * 接口：
 *   POST /events   {"eventId":"e1","group":"g1","key":"a","delta":5,"ts":1000}
 *   POST /retract  {"eventId":"e1"}
 *   POST /advance  {"now":5000}                      —— 显式推进逻辑时间
 *   GET  /topk?group=g1&k=3&now=2000                 —— now 可选，缺省不推进时间
 *   GET  /ranking?group=g1&now=2000                  —— 窗口内完整排序
 *   GET  /health
 */
public final class HttpApi {

    private final TopKService service;
    private final HttpServer server;
    private final ExecutorService pool;

    public HttpApi(TopKService service, int port) throws IOException {
        this.service = service;
        this.server = HttpServer.create(new InetSocketAddress(port), 0);
        this.pool = Executors.newFixedThreadPool(4);
        server.setExecutor(pool);
        server.createContext("/events", this::handleEvents);
        server.createContext("/retract", this::handleRetract);
        server.createContext("/advance", this::handleAdvance);
        server.createContext("/topk", this::handleTopK);
        server.createContext("/ranking", this::handleRanking);
        server.createContext("/health", ex -> respond(ex, 200, Json.obj("status", "ok")));
    }

    public void start() { server.start(); }

    /** 停止服务并关闭线程池（否则非守护线程会阻止 JVM 退出）。 */
    public void stop() {
        server.stop(0);
        pool.shutdownNow();
    }

    public int port() { return server.getAddress().getPort(); }

    private void handleEvents(HttpExchange ex) throws IOException {
        if (!requireMethod(ex, "POST")) return;
        try {
            Map<String, Object> body = Json.parseObject(readBody(ex));
            String eventId = reqString(body, "eventId");
            String group = reqString(body, "group");
            String key = reqString(body, "key");
            long delta = reqLong(body, "delta");
            long ts = reqLong(body, "ts");
            TopKService.InsertResult r = service.insert(eventId, group, key, delta, ts);
            switch (r) {
                case APPLIED -> respond(ex, 200, Json.obj("status", "applied", "watermark", service.watermark()));
                case DUPLICATE_EVENT_ID -> respond(ex, 409, Json.obj("status", "duplicate_event_id"));
                case ALREADY_EXPIRED -> respond(ex, 200, Json.obj("status", "already_expired", "watermark", service.watermark()));
            }
        } catch (IllegalArgumentException e) {
            respond(ex, 400, Json.obj("error", e.getMessage()));
        }
    }

    private void handleRetract(HttpExchange ex) throws IOException {
        if (!requireMethod(ex, "POST")) return;
        try {
            Map<String, Object> body = Json.parseObject(readBody(ex));
            String eventId = reqString(body, "eventId");
            boolean removed = service.retract(eventId);
            // 幂等：撤回不存在/已撤回/已过期的事件不算错误
            respond(ex, 200, Json.obj("status", removed ? "retracted" : "not_active"));
        } catch (IllegalArgumentException e) {
            respond(ex, 400, Json.obj("error", e.getMessage()));
        }
    }

    private void handleAdvance(HttpExchange ex) throws IOException {
        if (!requireMethod(ex, "POST")) return;
        try {
            Map<String, Object> body = Json.parseObject(readBody(ex));
            long now = reqLong(body, "now");
            service.advanceTo(now);
            respond(ex, 200, Json.obj("status", "advanced", "watermark", service.watermark()));
        } catch (IllegalArgumentException e) {
            respond(ex, 400, Json.obj("error", e.getMessage()));
        }
    }

    private void handleTopK(HttpExchange ex) throws IOException {
        if (!requireMethod(ex, "GET")) return;
        try {
            Map<String, String> q = query(ex);
            String group = required(q, "group");
            int k = Integer.parseInt(required(q, "k"));
            long now = q.containsKey("now") ? Long.parseLong(q.get("now")) : service.watermark();
            List<TopKService.Entry> items = service.topK(group, k, now);
            respond(ex, 200, Json.obj(
                    "group", group,
                    "k", k,
                    "windowMs", service.windowMs(),
                    "watermark", service.watermark(),
                    "count", items.size(),
                    "items", toJson(items)));
        } catch (IllegalArgumentException e) {
            respond(ex, 400, Json.obj("error", e.getMessage()));
        }
    }

    private void handleRanking(HttpExchange ex) throws IOException {
        if (!requireMethod(ex, "GET")) return;
        try {
            Map<String, String> q = query(ex);
            String group = required(q, "group");
            long now = q.containsKey("now") ? Long.parseLong(q.get("now")) : service.watermark();
            List<TopKService.Entry> items = service.ranking(group, now);
            respond(ex, 200, Json.obj(
                    "group", group,
                    "windowMs", service.windowMs(),
                    "watermark", service.watermark(),
                    "count", items.size(),
                    "items", toJson(items)));
        } catch (IllegalArgumentException e) {
            respond(ex, 400, Json.obj("error", e.getMessage()));
        }
    }

    private static List<Object> toJson(List<TopKService.Entry> items) {
        List<Object> out = new ArrayList<>(items.size());
        int rank = 1;
        for (TopKService.Entry e : items) {
            out.add(Json.obj("rank", rank++, "key", e.key(), "score", e.score()));
        }
        return out;
    }

    private static boolean requireMethod(HttpExchange ex, String method) throws IOException {
        if (ex.getRequestMethod().equalsIgnoreCase(method)) return true;
        respond(ex, 405, Json.obj("error", "method not allowed, use " + method));
        return false;
    }

    private static String readBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static void respond(HttpExchange ex, int code, Map<String, Object> body) throws IOException {
        byte[] bytes = Json.stringify(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    private static Map<String, String> query(HttpExchange ex) {
        Map<String, String> q = new LinkedHashMapCompat();
        String raw = ex.getRequestURI().getRawQuery();
        if (raw == null) return q;
        for (String pair : raw.split("&")) {
            int i = pair.indexOf('=');
            if (i > 0) q.put(urlDecode(pair.substring(0, i)), urlDecode(pair.substring(i + 1)));
            else q.put(urlDecode(pair), "");
        }
        return q;
    }

    private static String urlDecode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private static String required(Map<String, String> q, String name) {
        String v = q.get(name);
        if (v == null || v.isEmpty()) throw new IllegalArgumentException("missing query param: " + name);
        return v;
    }

    private static String reqString(Map<String, Object> body, String name) {
        Object v = body.get(name);
        if (!(v instanceof String s) || s.isEmpty())
            throw new IllegalArgumentException("missing or invalid field: " + name);
        return s;
    }

    private static long reqLong(Map<String, Object> body, String name) {
        Object v = body.get(name);
        if (v instanceof Number n) return n.longValue();
        throw new IllegalArgumentException("missing or invalid numeric field: " + name);
    }

    // 避免引入额外 import 的小工具
    private static final class LinkedHashMapCompat extends java.util.LinkedHashMap<String, String> {}
}
