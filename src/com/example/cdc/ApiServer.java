package com.example.cdc;

import com.sun.net.httpserver.HttpExchange;
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
import java.util.concurrent.Executors;

/**
 * 基于 JDK 内置 {@link HttpServer} 的 REST 接口（纯后端，无界面）。
 *
 * <pre>
 * POST /events            投递变更事件（JSON 数组）
 * GET  /status            消费状态（水位、RUNNING/PAUSED、滞留位置、打开事务）
 * GET  /tables            所有表当前已提交行
 * GET  /tables/{name}     单表当前已提交行
 * GET  /changes?table=xx  已提交变更台账（表/主键/旧值/新值/源位置）
 * GET  /txns/{id}         某事务状态（OPEN/已结束）——可观察“提交前不可见”
 * GET  /reconcile         与源事务解释器对账
 * POST /reset             清空内存状态与 WAL
 * GET  /health            健康检查
 * </pre>
 */
public final class ApiServer {

    private final HttpServer server;
    private final Engine engine;
    private final Wal wal;

    public ApiServer(int port, Engine engine, Wal wal) {
        this.engine = engine;
        this.wal = wal;
        try {
            this.server = HttpServer.create(new InetSocketAddress(port), 0);
        } catch (IOException e) {
            throw new RuntimeException("无法绑定端口 " + port, e);
        }
        server.createContext("/", this::route);
        // 单线程 executor：请求天然串行，引擎锁无竞争，语义易推理
        server.setExecutor(Executors.newSingleThreadExecutor(r -> {
            Thread t = new Thread(r, "cdc-http");
            t.setDaemon(true);
            return t;
        }));
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
    }

    public int port() {
        return server.getAddress().getPort();
    }

    private void route(HttpExchange ex) {
        String path = ex.getRequestURI().getPath();
        String method = ex.getRequestMethod();
        try {
            if (path.equals("/health") && method.equals("GET")) {
                sendJson(ex, 200, map("status", "ok"));
            } else if (path.equals("/events") && method.equals("POST")) {
                handleEvents(ex);
            } else if (path.equals("/status") && method.equals("GET")) {
                sendJson(ex, 200, engine.status());
            } else if (path.equals("/tables") && method.equals("GET")) {
                sendJson(ex, 200, engine.snapshot());
            } else if (path.startsWith("/tables/") && method.equals("GET")) {
                sendJson(ex, 200, engine.snapshot().getOrDefault(
                        decode(path.substring("/tables/".length())), new ArrayList<>()));
            } else if (path.equals("/changes") && method.equals("GET")) {
                String table = ex.getRequestURI().getQuery();
                if (table != null && table.startsWith("table=")) {
                    table = decode(table.substring("table=".length()));
                } else {
                    table = null;
                }
                sendJson(ex, 200, engine.ledger(table));
            } else if (path.startsWith("/txns/") && method.equals("GET")) {
                sendJson(ex, 200, engine.openTxn(decode(path.substring("/txns/".length()))));
            } else if (path.equals("/reconcile") && method.equals("GET")) {
                sendJson(ex, 200, engine.reconcile());
            } else if (path.equals("/reset") && method.equals("POST")) {
                engine.reset();
                sendJson(ex, 200, map("reset", true));
            } else {
                sendJson(ex, 404, map("error", "未找到接口: " + method + " " + path));
            }
        } catch (Engine.ProtocolException pe) {
            safeSend(ex, 400, map("error", pe.getMessage()));
        } catch (IllegalArgumentException iae) {
            safeSend(ex, 400, map("error", iae.getMessage()));
        } catch (Exception e) {
            safeSend(ex, 500, map("error", "内部错误: " + e));
        } finally {
            ex.close();
        }
    }

    private static void safeSend(HttpExchange ex, int code, Object payload) {
        try {
            sendJson(ex, code, payload);
        } catch (IOException ioe) {
            ioe.printStackTrace();
        }
    }

    @SuppressWarnings("unchecked")
    private void handleEvents(HttpExchange ex) throws IOException {
        String body = readBody(ex);
        Object parsed;
        try {
            parsed = Json.parse(body);
        } catch (IllegalArgumentException e) {
            sendJson(ex, 400, map("error", "请求体不是合法 JSON: " + e.getMessage()));
            return;
        }
        List<Object> rawList;
        if (parsed instanceof List) {
            rawList = (List<Object>) parsed;
        } else if (parsed instanceof Map && ((Map<String, Object>) parsed).get("events") instanceof List) {
            rawList = (List<Object>) ((Map<String, Object>) parsed).get("events");
        } else {
            sendJson(ex, 400, map("error", "请求体必须是事件 JSON 数组，或 {\"events\": [...]}"));
            return;
        }
        // 整批先解析校验：任何一条非法都整体拒绝（不落任何 WAL）
        List<Event> events = new ArrayList<>();
        for (int i = 0; i < rawList.size(); i++) {
            Object item = rawList.get(i);
            if (!(item instanceof Map)) {
                sendJson(ex, 400, map("error", "第 " + i + " 条事件不是 JSON 对象"));
                return;
            }
            events.add(Event.fromMap((Map<String, Object>) item));
        }
        Engine.IngestResult result = engine.ingest(events);
        sendJson(ex, 200, result.toMap());
    }

    // ---------- 工具 ----------

    private static Map<String, Object> map(String k, Object v) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put(k, v);
        return m;
    }

    private static String decode(String s) {
        return java.net.URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private static String readBody(HttpExchange ex) throws IOException {
        try (InputStream in = ex.getRequestBody()) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private static void sendJson(HttpExchange ex, int code, Object payload) throws IOException {
        byte[] data = Json.writePretty(payload).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, data.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(data);
        }
    }
}
