package drvb.http;

import drvb.json.Json;
import drvb.json.JsonException;
import drvb.model.Event;
import drvb.model.ProcessResult;
import drvb.rule.RuleException;
import drvb.service.DynamicRuleService;
import drvb.stream.EventProcessor;
import drvb.time.Scheduler;
import drvb.version.Binding;
import drvb.version.VersionException;

import com.sun.net.httpserver.Headers;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 纯后端 JSON/HTTP 服务。无任何第三方依赖，基于 JDK 内置 HttpServer。
 *
 * <p>路由见 README "HTTP API"。时间与调度全部来自 {@link DynamicRuleService}
 * 中注入的实现，manual 模式下由管理接口驱动，确定性可测。
 */
public final class RuleHttpServer {

    private final DynamicRuleService service;
    private HttpServer server;

    public RuleHttpServer(DynamicRuleService service) {
        this.service = service;
    }

    /** 在指定端口启动（port=0 表示由操作系统分配）；返回实际监听端口。 */
    public int start(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        server.createContext("/", new RootHandler());
        server.setExecutor(java.util.concurrent.Executors.newFixedThreadPool(8));
        server.start();
        return server.getAddress().getPort();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
    }

    // ------------------------------------------------------------------
    // 处理器
    // ------------------------------------------------------------------

    private final class RootHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange ex) throws IOException {
            try {
                route(ex);
            } catch (Exception e) {
                if (e instanceof VersionException) {
                    respondError(ex, 409, ((VersionException) e).code(), e.getMessage());
                } else if (e instanceof RuleException) {
                    respondError(ex, 400, "INVALID_RULE", e.getMessage());
                } else if (e instanceof JsonException) {
                    respondError(ex, 400, "INVALID_JSON", e.getMessage());
                } else if (e instanceof IllegalArgumentException) {
                    respondError(ex, 400, "BAD_REQUEST", e.getMessage());
                } else {
                    respondError(ex, 500, "INTERNAL_ERROR",
                            e.getClass().getSimpleName() + ": " + e.getMessage());
                }
            } finally {
                ex.close();
            }
        }

        private void route(HttpExchange ex) throws IOException {
            String method = ex.getRequestMethod();
            String path = ex.getRequestURI().getPath();

            if ("GET".equals(method) && "/health".equals(path)) {
                respondJson(ex, 200, Json.obj(
                        "status", "UP",
                        "clockMode", service.isManualTime() ? "manual" : "wall",
                        "processingTime", service.processingTime(),
                        "watermark", wm(service.watermark())));
                return;
            }

            if ("GET".equals(method) && "/state".equals(path)) {
                handleState(ex);
                return;
            }

            if ("/rules/publish".equals(path)) {
                requirePost(ex);
                handlePublish(ex);
                return;
            }
            if ("/rules/rollback".equals(path)) {
                requirePost(ex);
                handleRollback(ex);
                return;
            }
            if ("/events".equals(path)) {
                requirePost(ex);
                handleEvents(ex);
                return;
            }
            if ("/reclaim".equals(path)) {
                requirePost(ex);
                handleReclaim(ex);
                return;
            }
            if ("/admin/tick".equals(path)) {
                requirePost(ex);
                handleTick(ex);
                return;
            }
            if ("GET".equals(method) && "/results".equals(path)) {
                handleResults(ex);
                return;
            }
            if ("GET".equals(method) && "/admin/scheduler".equals(path)) {
                handleScheduler(ex);
                return;
            }

            respondError(ex, 404, "NOT_FOUND", "无此路由: " + method + " " + path);
        }

        // ---------- 规则 ----------

        private void handlePublish(HttpExchange ex) throws IOException {
            Map<String, Object> m = Json.asObject(readJsonBody(ex));
            String id = Json.getString(m, "versionId");
            String name = Json.getOptionalString(m, "name", id);
            if (!m.containsKey("effectiveFrom")) {
                throw new IllegalArgumentException("缺少必填字段 'effectiveFrom'（事件时间边界，毫秒）");
            }
            long from = Json.asLong(m.get("effectiveFrom"));
            Object predicate = m.get("predicate");
            if (!(predicate instanceof Map)) {
                throw new IllegalArgumentException("缺少必填对象字段 'predicate'");
            }
            String note = m.get("note") == null ? null : Json.asString(m.get("note"));
            @SuppressWarnings("unchecked")
            Map<String, Object> spec = (Map<String, Object>) predicate;
            Binding b = service.publishVersion(id, name, from, spec, note);
            respondJson(ex, 201, Json.obj(
                    "binding", Dtos.bindingToJson(b),
                    "message", "版本 " + id + " 已发布，自事件时间 " + from + " 起生效"));
        }

        private void handleRollback(HttpExchange ex) throws IOException {
            Map<String, Object> m = Json.asObject(readJsonBody(ex));
            String id = Json.getString(m, "versionId");
            if (!m.containsKey("effectiveFrom")) {
                throw new IllegalArgumentException("缺少必填字段 'effectiveFrom'（事件时间边界，毫秒）");
            }
            long from = Json.asLong(m.get("effectiveFrom"));
            String note = m.get("note") == null ? null : Json.asString(m.get("note"));
            Binding b = service.rollback(id, from, note);
            respondJson(ex, 201, Json.obj(
                    "binding", Dtos.bindingToJson(b),
                    "message", "已回滚：自事件时间 " + from + " 起版本 " + id + " 重新生效"));
        }

        // ---------- 事件 ----------

        private void handleEvents(HttpExchange ex) throws IOException {
            Object body = readJsonBody(ex);
            List<Event> events = new ArrayList<>();
            boolean batch = body instanceof List;
            if (batch) {
                for (Object item : Json.asArray(body)) {
                    events.add(Dtos.toEvent(item));
                }
            } else {
                events.add(Dtos.toEvent(body));
            }
            List<Object> items = new ArrayList<>();
            int rejected = 0;
            for (Event e : events) {
                ProcessResult r = service.submit(e);
                if (r.rejected()) {
                    rejected++;
                }
                items.add(Dtos.resultToJson(r));
            }
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("watermark", wm(service.watermark()));
            if (batch) {
                resp.put("count", items.size());
                resp.put("rejectedCount", rejected);
                resp.put("results", items);
            } else {
                resp.put("result", items.get(0));
                if (rejected > 0) {
                    ProcessResult r0 = service.allResults().get(service.allResults().size() - 1);
                    resp.put("rejected", true);
                    resp.put("rejectReason", r0.rejectReason().name());
                }
            }
            // 拒绝是业务结果而非 HTTP 错误：统一 200，逐条携带状态
            respondJson(ex, 200, resp);
        }

        private void handleResults(HttpExchange ex) throws IOException {
            Map<String, String> q = query(ex);
            EventProcessor.ResultFilter f = EventProcessor.ResultFilter.all();
            String status = q.get("status");
            if (status != null) {
                EventProcessor.ResultStatus rs;
                try {
                    rs = EventProcessor.ResultStatus.valueOf(status.toUpperCase());
                } catch (IllegalArgumentException iae) {
                    throw new IllegalArgumentException(
                            "未知 status: " + status
                                    + "（可选 ALL/MATCHED/FILTERED_OUT/LATE/REJECTED）");
                }
                f = EventProcessor.ResultFilter.of(rs);
            }
            String version = q.get("version");
            if (version != null) {
                f = f.withVersion(version);
            }
            String limitStr = q.get("limit");
            if (limitStr != null) {
                f = f.withLimit(Integer.parseInt(limitStr));
            }
            List<Object> items = new ArrayList<>();
            for (ProcessResult r : service.queryResults(f)) {
                items.add(Dtos.resultToJson(r));
            }
            respondJson(ex, 200, Json.obj("count", items.size(), "results", items));
        }

        // ---------- 回收 ----------

        private void handleReclaim(HttpExchange ex) throws IOException {
            Map<String, Object> m = Json.asObject(readJsonBody(ex));
            String mode = Json.getOptionalString(m, "mode", "eligible");
            if ("eligible".equals(mode)) {
                List<String> ids = service.reclaimEligible();
                List<Object> reclaimed = new ArrayList<>(ids);
                respondJson(ex, 200, Json.obj(
                        "reclaimed", reclaimed,
                        "reclaimGate", wm(service.reclaimGate()),
                        "message", ids.isEmpty()
                                ? "当前没有满足回收前提的版本"
                                : "已回收 " + ids.size() + " 个版本"));
                return;
            }
            if ("one".equals(mode)) {
                String id = Json.getString(m, "versionId");
                var before = service.eligibility(id);
                service.reclaim(id);
                respondJson(ex, 200, Json.obj(
                        "reclaimed", id,
                        "precondition", before.reason()));
                return;
            }
            if ("check".equals(mode)) {
                String id = Json.getString(m, "versionId");
                var e = service.eligibility(id);
                respondJson(ex, 200, Json.obj(
                        "versionId", id,
                        "eligible", e.eligible(),
                        "latestIntervalEnd", e.latestIntervalEnd(),
                        "reclaimGate", wm(service.reclaimGate()),
                        "reason", e.reason()));
                return;
            }
            throw new IllegalArgumentException("mode 必须是 eligible / one / check");
        }

        // ---------- 状态 / 管理 ----------

        private void handleState(HttpExchange ex) throws IOException {
            var snap = service.table().snapshot();
            Map<String, Object> state = Dtos.stateToJson(
                    snap,
                    service.watermark(),
                    service.allowedLateness(),
                    service.retentionHorizon(),
                    service.reclaimGate(),
                    service.processingTime(),
                    service.eligibilityView(),
                    recentResults(20));
            respondJson(ex, 200, state);
        }

        private void handleTick(HttpExchange ex) throws IOException {
            Map<String, Object> m = Json.asObject(readJsonBody(ex));
            long current = service.processingTime();
            Long target = null;
            if (m.containsKey("advanceTo")) {
                target = Json.asLong(m.get("advanceTo"));
            } else if (m.containsKey("advanceBy")) {
                target = current + Json.asLong(m.get("advanceBy"));
            }
            if (target == null) {
                throw new IllegalArgumentException(
                        "需要 advanceTo（绝对时间戳）或 advanceBy（增量毫秒）");
            }
            List<String> fired = service.advanceTimeAndRun(target);
            respondJson(ex, 200, Json.obj(
                    "processingTime", service.processingTime(),
                    "watermark", wm(service.watermark()),
                    "reclaimGate", wm(service.reclaimGate()),
                    "firedTasks", new ArrayList<>(fired)));
        }

        private void handleScheduler(HttpExchange ex) throws IOException {
            List<Object> tasks = new ArrayList<>();
            for (Scheduler.ScheduledTask t : service.scheduler().tasks()) {
                tasks.add(taskJson(t));
            }
            respondJson(ex, 200, Json.obj(
                    "schedulerType", service.isManualTime() ? "manual" : "executor",
                    "tasks", tasks));
        }

        private List<ProcessResult> recentResults(int n) {
            List<ProcessResult> all = service.allResults();
            return all.size() <= n ? all : all.subList(all.size() - n, all.size());
        }
    }

    private static Map<String, Object> taskJson(Scheduler.ScheduledTask t) {
        return Json.obj(
                "name", t.name(),
                "periodMillis", t.periodMillis(),
                "runCount", t.runCount());
    }

    private static Object wm(long v) {
        return v == Long.MIN_VALUE ? null : v;
    }

    // ------------------------------------------------------------------
    // HTTP 基础工具
    // ------------------------------------------------------------------

    private static void requirePost(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) {
            throw new IllegalArgumentException("仅支持 POST，实际: " + ex.getRequestMethod());
        }
    }

    private static Object readJsonBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            String raw = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            if (raw.isEmpty()) {
                throw new IllegalArgumentException("请求体不能为空（需要 JSON）");
            }
            return Json.parse(raw);
        }
    }

    private static Map<String, String> query(HttpExchange ex) {
        Map<String, String> out = new LinkedHashMap<>();
        String q = ex.getRequestURI().getRawQuery();
        if (q == null || q.isEmpty()) {
            return out;
        }
        for (String pair : q.split("&")) {
            int i = pair.indexOf('=');
            if (i < 0) {
                out.put(urlDecode(pair), "");
            } else {
                out.put(urlDecode(pair.substring(0, i)), urlDecode(pair.substring(i + 1)));
            }
        }
        return out;
    }

    private static String urlDecode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private static void respondJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] bytes = Json.writePretty(body).getBytes(StandardCharsets.UTF_8);
        Headers h = ex.getResponseHeaders();
        h.set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(bytes);
        }
    }

    private static void respondError(HttpExchange ex, int status, String code, String message)
            throws IOException {
        respondJson(ex, status, Json.obj("error", code, "message", message));
    }
}
