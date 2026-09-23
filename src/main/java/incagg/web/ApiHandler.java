package incagg.web;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import incagg.json.Json;
import incagg.model.Event;
import incagg.store.IncrementalViewStore;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 路由：
 *   GET  /health
 *   POST /events               应用单个事件（请求体即事件对象）
 *   POST /events/batch         批量应用（{"events":[...]}，按序应用，每个独立报告去重结果）
 *   GET  /view                 增量视图快照
 *   GET  /view/recompute       全量重算结果
 *   GET  /view/diff            增量 vs 全量差异（验收用；空 diffs 即一致）
 *   POST /admin/reset          清空全部状态（测试/演示用）
 */
final class ApiHandler implements HttpHandler {

    private final IncrementalViewStore store;

    ApiHandler(IncrementalViewStore store) {
        this.store = store;
    }

    @Override
    public void handle(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (path) {
                case "/health" -> {
                    if ("GET".equals(method)) {
                        ok(ex, Map.of("status", "UP"));
                    } else methodNotAllowed(ex);
                }
                case "/events" -> {
                    if ("POST".equals(method)) postEvent(ex, false);
                    else methodNotAllowed(ex);
                }
                case "/events/batch" -> {
                    if ("POST".equals(method)) postEvent(ex, true);
                    else methodNotAllowed(ex);
                }
                case "/view" -> {
                    if ("GET".equals(method)) ok(ex, ApiCodec.snapshotJson(store.snapshot()));
                    else methodNotAllowed(ex);
                }
                case "/view/recompute" -> {
                    if ("GET".equals(method)) ok(ex, ApiCodec.snapshotJson(store.fullRecompute()));
                    else methodNotAllowed(ex);
                }
                case "/view/diff" -> {
                    if ("GET".equals(method)) {
                        List<String> diffs = store.diffAgainstFull();
                        Map<String, Object> body = new LinkedHashMap<>();
                        body.put("consistent", diffs.isEmpty());
                        body.put("diffs", diffs);
                        ok(ex, body);
                    } else methodNotAllowed(ex);
                }
                case "/admin/reset" -> {
                    if ("POST".equals(method)) {
                        store.debugReset();
                        ok(ex, Map.of("status", "RESET"));
                    } else methodNotAllowed(ex);
                }
                default -> notFound(ex);
            }
        } catch (IllegalArgumentException e) {
            // 输入类错误：400，不改变任何状态（校验在应用前完成）
            error(ex, 400, e.getMessage());
        } catch (Exception e) {
            error(ex, 500, "服务器内部错误: " + e);
        }
    }

    private void postEvent(HttpExchange ex, boolean batch) throws IOException {
        String raw = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        if (raw.isBlank()) throw new IllegalArgumentException("请求体为空");
        Map<String, Object> body = Json.parseObject(raw);
        List<Event> events = batch ? ApiCodec.toEvents(body) : List.of(ApiCodec.toEvent(body));
        if (events.isEmpty()) throw new IllegalArgumentException("events 为空");

        // 先整体校验（构造时已校验），再按序应用；每个事件的去重判定相互独立。
        List<Map<String, Object>> results = new java.util.ArrayList<>();
        for (Event e : events) {
            IncrementalViewStore.ApplyResult r = store.apply(e);
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("eventId", e.eventId);
            item.put("type", e.type.name());
            item.put("status", r.status);
            item.put("ignored", r.ignored);
            item.put("conflict", r.conflict);
            item.put("detail", r.detail);
            results.add(item);
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("results", results);
        resp.put("view", ApiCodec.snapshotJson(store.snapshot()));
        ok(ex, resp);
    }

    // ---------------------------------------------------------------- HTTP 工具

    static void ok(HttpExchange ex, Object body) throws IOException {
        writeJson(ex, 200, body);
    }

    static void error(HttpExchange ex, int code, String message) throws IOException {
        writeJson(ex, code, Map.of("error", message == null ? "" : message));
    }

    static void notFound(HttpExchange ex) throws IOException {
        writeJson(ex, 404, Map.of("error", "路径不存在: " + ex.getRequestURI().getPath()));
    }

    static void methodNotAllowed(HttpExchange ex) throws IOException {
        writeJson(ex, 405, Map.of("error", "方法不允许: " + ex.getRequestMethod()));
    }

    private static void writeJson(HttpExchange ex, int code, Object body) throws IOException {
        byte[] bytes = ApiCodec.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }
}
