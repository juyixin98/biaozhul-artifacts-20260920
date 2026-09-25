package cep.service;

import cep.json.JsonException;
import cep.json.JsonParser;
import cep.json.JsonValue;
import cep.json.JsonWriter;
import cep.model.EngineResult;
import cep.service.SessionStore.IngestOp;
import cep.service.SessionStore.Session;

import com.sun.net.httpserver.Filter;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.Executors;

/**
 * 基于 JDK 内置 {@link HttpServer} 的 JSON/HTTP 服务（零外部依赖、无消息中间件）。
 *
 * <p>端点：
 * <ul>
 *   <li>POST /evaluate                  —— 一次性确定性评估（useReference=true 时同时跑参考实现）</li>
 *   <li>POST /api/sessions              —— 创建有状态会话，返回 sessionId</li>
 *   <li>POST /api/sessions/{id}/events  —— 追加一个/一批事件</li>
 *   <li>POST /api/sessions/{id}/watermark?watermark=N（或 body {"watermark":N}）</li>
 *   <li>GET  /api/sessions/{id}         —— 当前快照（不推进）</li>
 *   <li>POST /api/sessions/{id}/flush   —— 终局推进并返回全部结果</li>
 *   <li>POST /api/sessions/{id}/replay  —— reset + 按原始喂入日志重放 + flush</li>
 *   <li>GET  /healthz                   —— 健康检查</li>
 * </ul>
 */
public final class HttpApiServer {

    private final HttpServer server;
    private final SessionStore store = new SessionStore();

    public HttpApiServer(int port) {
        try {
            this.server = HttpServer.create(new InetSocketAddress(port), 0);
        } catch (IOException e) {
            throw new IllegalStateException("无法创建 HTTP 服务 (port=" + port + ")", e);
        }
        server.createContext("/evaluate", wrap(this::handleEvaluate));
        server.createContext("/api/sessions", wrap(this::handleSessions));
        server.createContext("/healthz", wrap(ex ->
                writeJson(ex, 200, JsonValue.obj().set("status", "ok"))));
        server.setExecutor(Executors.newFixedThreadPool(8));
    }

    public void start() { server.start(); }

    public void stop() { server.stop(0); }

    public int getPort() { return server.getAddress().getPort(); }

    // ----------------------------------------------------------- 路由

    private void handleEvaluate(HttpExchange ex) throws IOException {
        requireMethod(ex, "POST");
        JsonValue.Obj out = ApiService.evaluate(readBody(ex));
        writeJson(ex, 200, out);
    }

    private void handleSessions(HttpExchange ex) throws IOException {
        String path = ex.getRequestURI().getPath();
        String rest = path.substring("/api/sessions".length());

        if (rest.isEmpty() || rest.equals("/")) {
            requireMethod(ex, "POST");
            String id = store.create(ApiService.parseConfig(readBody(ex)));
            writeJson(ex, 201, JsonValue.obj()
                    .set("sessionId", id)
                    .set("status", "created"));
            return;
        }

        List<String> parts = Arrays.stream(rest.split("/"))
                .filter(s -> !s.isEmpty())
                .toList();
        String id = parts.get(0);
        Session session = store.require(id);
        String action = parts.size() >= 2 ? parts.get(1) : "";

        switch (action) {
            case "" -> {
                requireMethod(ex, "GET");
                writeJson(ex, 200, resultEnvelope(id, session, store.snapshot(session)));
            }
            case "events" -> {
                requireMethod(ex, "POST");
                if (session.flushed()) {
                    throw new ApiException(409, "会话已 flush，不能再追加事件；请调用 replay");
                }
                JsonValue.Obj req = readBody(ex);
                JsonValue events = req.has("events") ? req.get("events") : req;
                JsonValue.Arr arr;
                if (events instanceof JsonValue.Arr a) {
                    arr = a;
                } else if (events instanceof JsonValue.Obj o) {
                    arr = JsonValue.arr().add(o);
                } else {
                    throw ApiException.badRequest("需要 events 数组或单个事件对象");
                }
                for (JsonValue raw : arr.values()) {
                    if (!(raw instanceof JsonValue.Obj ev)) {
                        throw ApiException.badRequest("事件必须是对象");
                    }
                    String eid = ev.requireString("id");
                    String key = ev.requireString("key");
                    long ts = ev.requireLong("timestamp");
                    try {
                        if (ev.has("seq")) {
                            store.recordAndApply(session,
                                    new IngestOp(eid, key, ts, true, ev.requireLong("seq")));
                        } else {
                            store.recordAndApply(session,
                                    new IngestOp(eid, key, ts, false, 0L));
                        }
                    } catch (IllegalArgumentException e) {
                        throw ApiException.badRequest(e.getMessage());
                    }
                }
                JsonValue.Obj out = resultEnvelope(id, session, store.snapshot(session));
                out.set("accepted", arr.size());
                writeJson(ex, 200, out);
            }
            case "watermark" -> {
                requireMethod(ex, "POST");
                String wmParam = firstParam(ex, "watermark");
                if (wmParam == null) {
                    wmParam = Long.toString(readBody(ex).requireLong("watermark"));
                }
                long wm;
                try {
                    wm = Long.parseLong(wmParam);
                } catch (NumberFormatException e) {
                    throw ApiException.badRequest("非法 watermark: " + wmParam);
                }
                store.advanceWatermark(session, wm);
                writeJson(ex, 200, resultEnvelope(id, session, store.snapshot(session)));
            }
            case "flush" -> {
                requireMethod(ex, "POST");
                writeJson(ex, 200, resultEnvelope(id, session, store.flush(session)));
            }
            case "replay" -> {
                requireMethod(ex, "POST");
                EngineResult r = store.replayAndFlush(session);
                JsonValue.Obj out = resultEnvelope(id, session, r);
                out.set("replayed", true);
                writeJson(ex, 200, out);
            }
            default -> throw ApiException.notFound("未知操作: " + action);
        }
    }

    private JsonValue.Obj resultEnvelope(String id, Session s, EngineResult r) {
        return ApiService.resultJson(r, s.flushed()).set("sessionId", id);
    }

    // ----------------------------------------------------------- 工具

    private static void requireMethod(HttpExchange ex, String expected) {
        if (!expected.equals(ex.getRequestMethod())) {
            throw ApiException.badRequest("仅支持 " + expected + "，收到 " + ex.getRequestMethod());
        }
    }

    private static String firstParam(HttpExchange ex, String name) {
        String q = ex.getRequestURI().getRawQuery();
        if (q == null) {
            return null;
        }
        for (String pair : q.split("&")) {
            int i = pair.indexOf('=');
            if (i > 0 && pair.substring(0, i).equals(name)) {
                return java.net.URLDecoder.decode(pair.substring(i + 1), StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    private JsonValue.Obj readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        try {
            JsonValue v = JsonParser.parse(new String(bytes, StandardCharsets.UTF_8));
            if (!(v instanceof JsonValue.Obj obj)) {
                throw ApiException.badRequest("请求体必须是 JSON 对象");
            }
            return obj;
        } catch (JsonException e) {
            throw ApiException.badRequest("JSON 解析失败: " + e.getMessage());
        }
    }

    private static void writeJson(HttpExchange ex, int status, JsonValue body) throws IOException {
        byte[] data = JsonWriter.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(data);
        }
    }

    /** 统一把业务异常映射成 JSON 错误响应，其它异常为 500。 */
    private static HttpHandler wrap(HttpHandler handler) {
        return ex -> {
            try {
                handler.handle(ex);
            } catch (ApiException e) {
                if (!ex.getResponseHeaders().containsKey("Content-Type")) {
                    writeJson(ex, e.status(), errorBody(e.status(), e.getMessage()));
                }
            } catch (IllegalArgumentException e) {
                writeJson(ex, 400, errorBody(400, e.getMessage()));
            } catch (Exception e) {
                writeJson(ex, 500, errorBody(500, "内部错误: " + e));
            } finally {
                ex.close();
            }
        };
    }

    public static JsonValue.Obj errorBody(int status, String message) {
        return JsonValue.obj()
                .set("error", true)
                .set("status", status)
                .set("message", message);
    }
}
