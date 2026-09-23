package com.example.txflow;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadPoolExecutor;

/**
 * HTTP 服务（JDK 内置 com.sun.net.httpserver，无任何第三方依赖）。
 *
 * 接口一览（均为本地单机接口，默认仅监听 127.0.0.1）：
 *
 *   GET  /health                        健康检查
 *   POST /inputs          {"text":...}  追加一条输入记录，返回 offset
 *   POST /process         {"maxRecords":N,"failPoint":"AFTER_PROCESS"|...}
 *                                        处理一个微批（一个接收器事务）
 *   POST /recover                       显式触发恢复对账（启动时已自动执行一次）
 *   GET  /state                         快照视图：偏移、提交数、prepared、状态
 *   GET  /outputs                       接收器【可见】输出（仅已提交），JSON 行
 *   GET  /markers                       接收器 committed.log 提交标记
 */
public class HttpApiServer {

    private final Engine engine;
    private final HttpServer server;
    private final int port;

    public HttpApiServer(Engine engine, String host, int port) throws IOException {
        this.engine = engine;
        this.port = port;
        this.server = HttpServer.create(new InetSocketAddress(host, port), 0);
        this.server.setExecutor(Executors.newFixedThreadPool(4, r -> {
            Thread t = new Thread(r, "txflow-http");
            t.setDaemon(true);
            return t;
        }));
        register();
    }

    public int getPort() {
        return server.getAddress().getPort();
    }

    public void start() {
        server.start();
    }

    public void stop() {
        server.stop(0);
        ((ThreadPoolExecutor) server.getExecutor()).shutdownNow();
    }

    private void register() {
        server.createContext("/health", ex -> handle(ex, this::health));
        server.createContext("/inputs", ex -> handle(ex, this::appendInput));
        server.createContext("/process", ex -> handle(ex, this::process));
        server.createContext("/recover", ex -> handle(ex, this::recover));
        server.createContext("/state", ex -> handle(ex, this::state));
        server.createContext("/outputs", ex -> handle(ex, this::outputs));
        server.createContext("/markers", ex -> handle(ex, this::markers));
    }

    // ---------- handlers ----------

    private Object health(HttpExchange ex) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("status", "ok");
        m.put("service", "txflow-transactional-stream-snapshot");
        m.put("scope", "exactly-once guarantee covers ONLY the built-in local file sink");
        return m;
    }

    private Object appendInput(HttpExchange ex) throws IOException {
        requireMethod(ex, "POST");
        Map<String, Object> body = readJsonBody(ex);
        if (!body.containsKey("text")) {
            throw new ApiException(400, "请求体必须包含 \"text\" 字段，例如 {\"text\":\"a b c\"}");
        }
        Object text = body.get("text");
        if (!(text instanceof String)) {
            throw new ApiException(400, "\"text\" 必须是字符串");
        }
        long offset = engine.inputLog().append(Json.write(body));
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("appended", true);
        resp.put("offset", offset);
        return resp;
    }

    private Object process(HttpExchange ex) throws IOException {
        requireMethod(ex, "POST");
        Map<String, Object> body = readJsonBody(ex);
        int maxRecords = (int) Json.lng(body, "maxRecords", 1);
        if (maxRecords < 1) {
            throw new ApiException(400, "maxRecords 必须 >= 1");
        }
        String fpName = Json.str(body, "failPoint", "NONE");
        Engine.FailPoint failPoint;
        try {
            failPoint = Engine.FailPoint.valueOf(fpName);
        } catch (IllegalArgumentException e) {
            throw new ApiException(400, "未知 failPoint: " + fpName
                    + "，可选 NONE/AFTER_PROCESS/AFTER_STATE_PERSIST/AFTER_OUTPUT_PREPARE/AFTER_COMMIT");
        }
        return engine.process(maxRecords, failPoint);
    }

    private Object recover(HttpExchange ex) throws IOException {
        requireMethod(ex, "POST");
        return engine.recover();
    }

    private Object state(HttpExchange ex) {
        requireMethod(ex, "GET");
        return engine.snapshotState();
    }

    private Object outputs(HttpExchange ex) throws IOException {
        requireMethod(ex, "GET");
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("scope", "仅包含内置本地文件接收器的【已提交可见】输出");
        resp.put("lines", engine.visibleOutputs());
        resp.put("count", engine.visibleOutputs().size());
        return resp;
    }

    private Object markers(HttpExchange ex) throws IOException {
        requireMethod(ex, "GET");
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("scope", "内置本地文件接收器 committed.log 中的已提交事务标记");
        resp.put("markers", engine.committedMarkers());
        resp.put("count", engine.committedMarkers().size());
        return resp;
    }

    // ---------- plumbing ----------

    private interface Handler {
        Object handle(HttpExchange ex) throws Exception;
    }

    private void handle(HttpExchange ex, Handler h) {
        try {
            Object result = h.handle(ex);
            sendJson(ex, 200, Json.writePretty(result));
        } catch (ApiException e) {
            sendJson(ex, e.status, Json.writePretty(Map.of("error", e.getMessage())));
        } catch (Exception e) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
            sendJson(ex, 500, Json.writePretty(err));
        }
    }

    private static void requireMethod(HttpExchange ex, String method) {
        if (!ex.getRequestMethod().equals(method)) {
            throw new ApiException(405, "仅支持 " + method + " " + ex.getRequestURI().getPath());
        }
    }

    private static Map<String, Object> readJsonBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        if (bytes.length == 0) {
            return new LinkedHashMap<>();
        }
        try {
            return Json.parseObject(new String(bytes, StandardCharsets.UTF_8));
        } catch (RuntimeException e) {
            throw new ApiException(400, "请求体不是合法 JSON: " + e.getMessage());
        }
    }

    private static void sendJson(HttpExchange ex, int status, String body) {
        byte[] data = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        try {
            ex.sendResponseHeaders(status, data.length);
            try (OutputStream os = ex.getResponseBody()) {
                os.write(data);
            }
        } catch (IOException e) {
            // 连接已断开（例如故障注入 halt 前），忽略
        }
    }

    static final class ApiException extends RuntimeException {
        final int status;

        ApiException(int status, String message) {
            super(message);
            this.status = status;
        }
    }

    // ---------- main ----------

    public static void main(String[] args) throws Exception {
        String host = System.getenv().getOrDefault("TXFLOW_HOST", "127.0.0.1");
        int port = Integer.parseInt(System.getenv().getOrDefault("TXFLOW_PORT", "8080"));
        String dir = System.getenv().getOrDefault("TXFLOW_DATA", "./txflow-data");

        Path dataDir = Paths.get(dir);
        Engine engine = new Engine(dataDir);

        // 启动时自动恢复对账（正常启动为空操作）
        Map<String, Object> report = engine.recover();
        System.out.println("[txflow] 启动恢复完成: " + Json.write(report));

        HttpApiServer api = new HttpApiServer(engine, host, port);
        api.start();
        System.out.println("[txflow] 监听 http://" + host + ":" + api.getPort()
                + "  数据目录=" + dataDir.toAbsolutePath());
    }
}
