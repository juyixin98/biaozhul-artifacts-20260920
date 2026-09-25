package dev.example.cp.svc;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import dev.example.cp.core.Event;
import dev.example.cp.engine.Clock;
import dev.example.cp.engine.Engine;
import dev.example.cp.fail.CrashPoint;
import dev.example.cp.fail.InjectedCrash;
import dev.example.cp.json.Json;
import dev.example.cp.storage.SourceLog;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * 基于 JDK 内置 {@code com.sun.net.httpserver} 的 HTTP JSON 服务（零外部依赖）。
 *
 * <pre>
 *  POST /events        {"key":"a","value":3}            追加事件；可带 "drain":true 立即消费
 *  POST /drain                                           消费全部输入
 *  POST /checkpoints                                     插入屏障，同步完成一次检查点
 *  GET  /status                                          当前偏移 / 汇总（可比较状态）
 *  POST /faults        {"at":"STATE_WRITE","epoch":2,"halt":false}
 *  DELETE /faults
 * </pre>
 *
 * 若故障以 {@code halt:false} 注入，处理请求的线程会收到 {@link InjectedCrash}；
 * 下一次请求会重新打开引擎并执行恢复——这建模了“崩溃后守护进程被拉起”。
 */
public final class HttpService {

    private final Path dataDir;
    private final int requestedPort;
    private int boundPort;
    private HttpServer server;
    private java.util.concurrent.ExecutorService executor;

    private Engine engine;

    public HttpService(Path dataDir, int port) {
        this.dataDir = dataDir;
        this.requestedPort = port;
    }

    /** 实际监听端口（传入 0 时由操作系统分配，start 后可由此读取）。 */
    public int boundPort() {
        return boundPort;
    }

    public void start() throws IOException {
        this.engine = Engine.open(dataDir, new dev.example.cp.engine.ManualScheduler(), Clock.SYSTEM);
        server = HttpServer.create(new InetSocketAddress(requestedPort), 0);
        this.boundPort = server.getAddress().getPort();
        server.createContext("/", this::route);
        // 单线程：无锁串行、确定性；守护线程，stop 后 JVM 可退出
        this.executor = Executors.newSingleThreadExecutor(r -> {
            Thread t = new Thread(r, "http-service");
            t.setDaemon(true);
            return t;
        });
        server.setExecutor(executor);
        server.start();
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
        if (executor != null) {
            executor.shutdownNow();
        }
    }

    private void route(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            String method = ex.getRequestMethod();
            switch (method + " " + path) {
                case "POST /events" -> postEvent(ex);
                case "POST /drain" -> postDrain(ex);
                case "POST /checkpoints" -> postCheckpoint(ex);
                case "GET /status" -> getStatus(ex);
                case "POST /faults" -> postFault(ex);
                case "DELETE /faults" -> deleteFault(ex);
                default -> sendJson(ex, 404, Map.of("error", "not found", "path", path));
            }
        } catch (InjectedCrash crash) {
            // 模拟进程崩溃：标记引擎死亡并释放资源；下一次请求重新 open（= 重启 + 恢复）。
            this.engine = null;
            try {
                sendJson(ex, 500, Map.of("crashed", true, "at", crash.point().name(),
                        "epoch", crash.epochId()));
            } catch (IOException ignored) {
                // 连接可能已断
            }
        } catch (Exception e) {
            sendJson(ex, 400, Map.of("error", String.valueOf(e.getMessage())));
        }
    }

    /** 崩溃后的“进程重启”：重新打开引擎（构造函数会执行恢复协议）。 */
    private synchronized Engine engine() {
        if (engine == null) {
            engine = Engine.open(dataDir, new dev.example.cp.engine.ManualScheduler(), Clock.SYSTEM);
        }
        return engine;
    }

    private void postEvent(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonBody(ex);
        String key = String.valueOf(body.getOrDefault("key", ""));
        if (key.isEmpty()) {
            sendJson(ex, 400, Map.of("error", "missing 'key'"));
            return;
        }
        Object v = body.get("value");
        if (!(v instanceof Number)) {
            sendJson(ex, 400, Map.of("error", "missing numeric 'value'"));
            return;
        }
        Engine eng = engine();
        long offset = eng.source().append(new Event(-1, key, ((Number) v).longValue()));
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("offset", offset);
        if (Boolean.TRUE.equals(body.get("drain"))) {
            Engine.RunReport r = eng.runUntilDrained();
            resp.put("processed", r.eventsProcessed());
        }
        sendJson(ex, 200, resp);
    }

    private void postDrain(HttpExchange ex) throws IOException {
        readBody(ex);
        Engine eng = engine();
        Engine.RunReport r = eng.runUntilDrainedAndCommitted();
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("processed", r.eventsProcessed());
        resp.put("checkpoints", r.checkpointsCompleted());
        sendJson(ex, 200, resp);
    }

    private void postCheckpoint(HttpExchange ex) throws IOException {
        readBody(ex);
        long epoch = engine().triggerCheckpoint();
        sendJson(ex, 200, Map.of("epoch", epoch));
    }

    private void getStatus(HttpExchange ex) throws IOException {
        Engine.Status s = engine().status();
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("latestEpoch", s.latestEpoch());
        resp.put("lastConsumedOffset", s.lastConsumedOffset());
        resp.put("appliedEpoch", s.appliedEpoch());
        resp.put("committedOffset", s.committedOffset());
        resp.put("sourceEvents", s.sourceEvents());
        resp.put("processedCount", s.processedCount());
        resp.put("sums", s.sums());
        sendJson(ex, 200, resp);
    }

    private void postFault(HttpExchange ex) throws IOException {
        Map<String, Object> body = readJsonBody(ex);
        CrashPoint point = CrashPoint.parse(String.valueOf(body.get("at")));
        long epoch = ((Number) body.getOrDefault("epoch", 0)).longValue();
        boolean halt = Boolean.TRUE.equals(body.get("halt"));
        engine().faults().arm(point, epoch, halt);
        sendJson(ex, 200, Map.of("armed", true, "at", point.name(), "epoch", epoch, "halt", halt));
    }

    private void deleteFault(HttpExchange ex) throws IOException {
        readBody(ex);
        engine().faults().disarm();
        sendJson(ex, 200, Map.of("armed", false));
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> readJsonBody(HttpExchange ex) throws IOException {
        String text = readBody(ex);
        if (text.isBlank()) {
            return new LinkedHashMap<>();
        }
        Object parsed = Json.parse(text);
        if (!(parsed instanceof Map)) {
            throw new IllegalArgumentException("request body must be a JSON object");
        }
        return (Map<String, Object>) parsed;
    }

    private String readBody(HttpExchange ex) throws IOException {
        try (var is = ex.getRequestBody()) {
            return new String(is.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    private void sendJson(HttpExchange ex, int code, Object body) throws IOException {
        byte[] data = (Json.writePretty(body)).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(code, data.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(data);
        }
    }
}
