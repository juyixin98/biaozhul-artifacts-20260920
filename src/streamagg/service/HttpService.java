package streamagg.service;

import java.io.IOException;
import java.io.OutputStream;
import java.math.BigDecimal;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import streamagg.core.ApplyResult;
import streamagg.core.Clock;
import streamagg.core.EventOp;
import streamagg.core.JournalEntry;
import streamagg.core.KeyStats;
import streamagg.core.LedgerEntry;
import streamagg.core.ReconciliationReport;
import streamagg.core.Scheduler;
import streamagg.core.StreamProcessor;
import streamagg.core.SystemClock;
import streamagg.json.Json;
import streamagg.json.JsonException;
import streamagg.json.JsonParser;
import streamagg.json.JsonWriter;

/**
 * JSON 输入输出服务（纯后端，基于 JDK 内置 HttpServer，无外部依赖）。
 *
 * <p>端点：
 * <ul>
 *   <li>GET  /health                        健康检查</li>
 *   <li>POST /events                        提交单条事件操作</li>
 *   <li>POST /events/batch                  批量提交</li>
 *   <li>GET  /stats                         全部键聚合；?key=k 查询单键</li>
 *   <li>GET  /ledger                        最终事件账本</li>
 *   <li>GET  /journal                       原始输入日志</li>
 *   <li>GET  /pending?eventId=e1            某事件被乱序缓存的操作</li>
 *   <li>POST /reconcile                     增量状态 vs 账本重算 对账</li>
 *   <li>POST /replay                        日志重放并与当前状态比较</li>
 *   <li>POST /reset                         清空全部状态</li>
 * </ul>
 */
public final class HttpService implements AutoCloseable {

    private final HttpServer server;
    private final Clock clock;
    private final Scheduler scheduler;
    private final long reconcilePeriod;

    private StreamProcessor processor;

    public HttpService(int port) {
        this(port, new SystemClock(), null, 0L);
    }

    public HttpService(int port, Clock clock, Scheduler scheduler, long reconcilePeriodMillis) {
        this.clock = clock;
        this.scheduler = scheduler;
        this.reconcilePeriod = reconcilePeriodMillis;
        this.processor = new StreamProcessor(clock, scheduler, reconcilePeriodMillis);
        try {
            this.server = HttpServer.create(new InetSocketAddress(port), 0);
        } catch (IOException e) {
            throw new RuntimeException("无法在端口 " + port + " 启动 HTTP 服务", e);
        }
        registerRoutes();
        server.createContext("/", this::notFound);
    }

    private void registerRoutes() {
        server.createContext("/health", ex -> handle(ex, this::health));
        server.createContext("/events/batch", ex -> handle(ex, this::batch));
        server.createContext("/events", ex -> handle(ex, this::events));
        server.createContext("/stats", ex -> handle(ex, this::stats));
        server.createContext("/ledger", ex -> handle(ex, this::ledger));
        server.createContext("/journal", ex -> handle(ex, this::journal));
        server.createContext("/pending", ex -> handle(ex, this::pending));
        server.createContext("/reconcile", ex -> handle(ex, this::reconcile));
        server.createContext("/replay", ex -> handle(ex, this::replay));
        server.createContext("/reset", ex -> handle(ex, this::reset));
    }

    public void start() {
        server.start();
    }

    public int getPort() {
        return server.getAddress().getPort();
    }

    @Override
    public void close() {
        processor.shutdown();
        server.stop(0);
    }

    // ------------------------------------------------------------------
    // 处理器
    // ------------------------------------------------------------------

    private void health(HttpExchange ex) {
        Map<String, Object> body = Json.object();
        body.put("status", "ok");
        body.put("timeMillis", clock.nowMillis());
        writeJson(ex, 200, body);
    }

    private void events(HttpExchange ex) throws IOException {
        requireMethod(ex, "POST");
        Map<String, Object> req = readJsonObject(ex);
        EventOp op = Protocol.parseOp(req);
        ApplyResult result;
        try {
            result = processor.submit(op);
        } catch (IllegalArgumentException iae) {
            writeError(ex, 409, iae.getMessage());
            return;
        }
        writeJson(ex, 200, applyResultJson(result, processor));
    }

    private void batch(HttpExchange ex) throws IOException {
        requireMethod(ex, "POST");
        Object parsed = JsonParser.parse(readBody(ex));
        List<Object> rawOps = Json.asArray(parsed);
        List<Map<String, Object>> results = new ArrayList<>();
        int errorCount = 0;
        for (Object raw : rawOps) {
            Map<String, Object> item = Json.asObject(raw);
            EventOp op;
            ApplyResult result;
            try {
                op = Protocol.parseOp(item);
                result = processor.submit(op);
            } catch (RuntimeException e) {
                errorCount++;
                Map<String, Object> err = Json.object();
                err.put("ok", false);
                err.put("error", e.getMessage());
                err.put("input", item);
                results.add(err);
                continue;
            }
            results.add(applyResultJson(result, processor));
        }
        Map<String, Object> resp = Json.object();
        resp.put("received", rawOps.size());
        resp.put("errors", errorCount);
        resp.put("results", results);
        resp.put("stats", allStatsJson(processor));
        writeJson(ex, 200, resp);
    }

    private void stats(HttpExchange ex) {
        requireMethod(ex, "GET");
        Map<String, String> params = queryParams(ex);
        Map<String, Object> resp = Json.object();
        if (params.containsKey("key")) {
            String key = params.get("key");
            resp.put("key", key);
            resp.put("stats", keyStatsJson(processor.statsOf(key)));
        } else {
            resp.put("stats", allStatsJson(processor));
        }
        writeJson(ex, 200, resp);
    }

    private void ledger(HttpExchange ex) {
        requireMethod(ex, "GET");
        List<Object> entries = new ArrayList<>();
        for (LedgerEntry e : processor.ledger()) {
            Map<String, Object> m = Json.object();
            m.put("eventId", e.eventId());
            m.put("key", e.key());
            m.put("value", e.value());
            m.put("version", e.version());
            m.put("active", e.active());
            entries.add(m);
        }
        Map<String, Object> resp = Json.object();
        resp.put("entries", entries);
        resp.put("count", entries.size());
        writeJson(ex, 200, resp);
    }

    private void journal(HttpExchange ex) {
        requireMethod(ex, "GET");
        List<Object> entries = new ArrayList<>();
        for (JournalEntry je : processor.journal()) {
            Map<String, Object> m = Json.object();
            m.put("seq", je.seq());
            m.put("eventId", je.op().eventId());
            m.put("op", je.op().type().name());
            m.put("key", je.op().key());
            m.put("value", je.op().value());
            m.put("submittedVersion", je.op().version());
            m.put("canonicalVersion", je.canonicalVersion());
            m.put("opId", je.op().opId());
            m.put("receivedAtMillis", je.receivedAtMillis());
            entries.add(m);
        }
        Map<String, Object> resp = Json.object();
        resp.put("entries", entries);
        writeJson(ex, 200, resp);
    }

    private void pending(HttpExchange ex) {
        requireMethod(ex, "GET");
        Map<String, String> params = queryParams(ex);
        String eventId = params.get("eventId");
        if (eventId == null) {
            writeError(ex, 400, "缺少 eventId 查询参数");
            return;
        }
        List<Object> pending = new ArrayList<>();
        processor.pendingOf(eventId).forEach(op -> {
            Map<String, Object> m = Json.object();
            m.put("eventId", op.eventId());
            m.put("op", op.type().name());
            m.put("key", op.key());
            m.put("value", op.value());
            m.put("version", op.version());
            m.put("opId", op.opId());
            pending.add(m);
        });
        Map<String, Object> resp = Json.object();
        resp.put("eventId", eventId);
        resp.put("pending", pending);
        writeJson(ex, 200, resp);
    }

    private void reconcile(HttpExchange ex) {
        requireMethod(ex, "POST");
        writeReport(ex, processor.reconcile());
    }

    private void replay(HttpExchange ex) {
        requireMethod(ex, "POST");
        writeReport(ex, processor.replay());
    }

    private synchronized void reset(HttpExchange ex) {
        requireMethod(ex, "POST");
        processor.shutdown();
        processor = new StreamProcessor(clock, scheduler, reconcilePeriod);
        Map<String, Object> resp = Json.object();
        resp.put("status", "reset");
        writeJson(ex, 200, resp);
    }

    private void notFound(HttpExchange ex) {
        writeError(ex, 404, "未知端点: " + ex.getRequestMethod() + " " + ex.getRequestURI().getPath());
    }

    // ------------------------------------------------------------------
    // JSON 组装
    // ------------------------------------------------------------------

    private static Map<String, Object> applyResultJson(ApplyResult r, StreamProcessor p) {
        Map<String, Object> m = Json.object();
        m.put("ok", r.status() == ApplyResult.Status.APPLIED
                || r.status() == ApplyResult.Status.BUFFERED);
        m.put("status", r.status().name());
        m.put("eventId", r.eventId());
        m.put("version", r.version());
        if (r.reason() != null) {
            m.put("reason", r.reason());
        }
        m.put("stats", allStatsJson(p));
        return m;
    }

    private static Map<String, Object> allStatsJson(StreamProcessor p) {
        Map<String, Object> stats = Json.object();
        for (var e : p.allStats().entrySet()) {
            stats.put(e.getKey(), keyStatsJson(e.getValue()));
        }
        return stats;
    }

    private static Map<String, Object> keyStatsJson(KeyStats s) {
        Map<String, Object> m = Json.object();
        m.put("count", s.count());
        m.put("sum", s.sum());
        return m;
    }

    private static void writeReport(HttpExchange ex, ReconciliationReport report) {
        Map<String, Object> resp = Json.object();
        resp.put("consistent", report.consistent());
        if (report.detail() != null) {
            resp.put("detail", report.detail());
        }
        resp.put("incremental", statsMapToJson(report.incremental()));
        resp.put("recomputed", statsMapToJson(report.recomputed()));
        writeJson(ex, 200, resp);
    }

    private static Map<String, Object> statsMapToJson(Map<String, KeyStats> map) {
        Map<String, Object> out = Json.object();
        for (var e : map.entrySet()) {
            out.put(e.getKey(), keyStatsJson(e.getValue()));
        }
        return out;
    }

    // ------------------------------------------------------------------
    // HTTP 基础设施
    // ------------------------------------------------------------------

    private void handle(HttpExchange ex, Handler h) {
        try {
            h.handle(ex);
        } catch (JsonException je) {
            writeError(ex, 400, "JSON 错误: " + je.getMessage());
        } catch (IllegalArgumentException iae) {
            writeError(ex, 400, iae.getMessage());
        } catch (Exception e) {
            writeError(ex, 500, "服务器内部错误: " + e);
        } finally {
            ex.close();
        }
    }

    private interface Handler {
        void handle(HttpExchange ex) throws IOException;
    }

    private static void requireMethod(HttpExchange ex, String method) {
        if (!ex.getRequestMethod().equals(method)) {
            throw new JsonException("仅支持 " + method + "，实际为 " + ex.getRequestMethod());
        }
    }

    private static String readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private static Map<String, Object> readJsonObject(HttpExchange ex) throws IOException {
        String body = readBody(ex);
        if (body.isBlank()) {
            throw new JsonException("请求体为空");
        }
        return Json.asObject(JsonParser.parse(body));
    }

    private static Map<String, String> queryParams(HttpExchange ex) {
        Map<String, String> params = new LinkedHashMap<>();
        String query = ex.getRequestURI().getRawQuery();
        if (query == null || query.isEmpty()) {
            return params;
        }
        for (String pair : query.split("&")) {
            int eq = pair.indexOf('=');
            String k = eq < 0 ? pair : pair.substring(0, eq);
            String v = eq < 0 ? "" : pair.substring(eq + 1);
            params.put(java.net.URLDecoder.decode(k, StandardCharsets.UTF_8),
                    java.net.URLDecoder.decode(v, StandardCharsets.UTF_8));
        }
        return params;
    }

    private static void writeJson(HttpExchange ex, int status, Object body) {
        byte[] bytes = JsonWriter.pretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        try {
            ex.sendResponseHeaders(status, bytes.length);
            try (OutputStream os = ex.getResponseBody()) {
                os.write(bytes);
            }
        } catch (IOException ignored) {
            // 客户端断开等情况无需再响应
        }
    }

    private static void writeError(HttpExchange ex, int status, String message) {
        Map<String, Object> body = Json.object();
        body.put("ok", false);
        body.put("error", message);
        writeJson(ex, status, body);
    }
}
