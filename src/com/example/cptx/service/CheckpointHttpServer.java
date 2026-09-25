package com.example.cptx.service;

import com.example.cptx.core.Clock;
import com.example.cptx.core.CheckpointPolicy;
import com.example.cptx.core.FaultPhase;
import com.example.cptx.core.FaultSpec;
import com.example.cptx.core.InjectedFaultException;
import com.example.cptx.core.Json;
import com.example.cptx.core.Pipeline;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;

/**
 * 零依赖 JSON-over-HTTP 服务（基于 JDK 内置 HttpServer，无外部消息系统/Web 框架）。
 *
 * 端点：
 *   GET  /healthz
 *   POST /events          {"events":[{"key":"A","value":1.5}, ...]}  追加并处理
 *   POST /checkpoint      强制在当前偏移做检查点
 *   GET  /status          最终偏移、检查点、已提交事务、汇总表
 *   GET  /summary         仅汇总表
 *   POST /faults          {"phase":"OUTPUT_COMMIT","checkpointId":2} 注入故障（一次性）
 *   POST /recover         以同一数据目录“重启进程”：丢弃暂存、从最近检查点恢复、重放日志
 *
 * 崩溃后语义模拟：注入故障命中时，当前 Pipeline 实例标记为 CRASHED（内存状态视为丢失），
 * 必须调用 /recover 才能继续；这与 CLI 的新进程恢复走完全相同的启动代码路径。
 */
public final class CheckpointHttpServer {

    private final Path dataDir;
    private final long countEvery;
    private volatile HttpServer server;

    private Pipeline pipeline;
    private FaultSpec faults = new FaultSpec();
    private InjectedFaultException crashInfo;

    public CheckpointHttpServer(Path dataDir, long countEvery) {
        this.dataDir = dataDir;
        this.countEvery = countEvery;
    }

    public synchronized void start(int port) throws IOException {
        openPipeline();
        HttpServer s = HttpServer.create(new InetSocketAddress(port), 0);
        s.createContext("/healthz", this::health);
        s.createContext("/events", this::events);
        s.createContext("/checkpoint", this::forceCheckpoint);
        s.createContext("/status", this::status);
        s.createContext("/summary", this::summary);
        s.createContext("/faults", this::armFault);
        s.createContext("/recover", this::recover);
        s.setExecutor(Executors.newSingleThreadExecutor()); // 串行化所有请求
        s.start();
        this.server = s;
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
            // HttpServer.stop 不关闭自定义 Executor，需显式关停，否则其非守护线程阻止 JVM 退出。
            if (server.getExecutor() instanceof java.util.concurrent.ExecutorService es) {
                es.shutdownNow();
            }
        }
    }

    public int getAddressPort() {
        return server == null ? -1 : server.getAddress().getPort();
    }

    private void openPipeline() throws IOException {
        Clock clock = new Clock() {
            @Override
            public long nowMillis() {
                return System.currentTimeMillis();
            }
        };
        this.pipeline = new Pipeline(dataDir, CheckpointPolicy.count(countEvery), clock, faults);
        this.crashInfo = null;
    }

    // ---- 端点 ----

    private void health(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) { method(ex, "GET"); return; }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("ok", crashInfo == null);
        body.put("state", crashInfo == null ? "RUNNING" : "CRASHED");
        sendJson(ex, 200, body);
    }

    private void events(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) { method(ex, "POST"); return; }
        if (crashedGuard(ex)) return;
        Map<String, Object> req;
        try {
            req = Json.obj(Json.parse(readBody(ex)));
        } catch (RuntimeException e) {
            badRequest(ex, "请求体必须是 JSON 对象: " + e.getMessage());
            return;
        }
        Object raw = req.get("events");
        if (!(raw instanceof List<?> list) || list.isEmpty()) {
            badRequest(ex, "字段 events 必须是非空数组，元素为 {key,value}");
            return;
        }
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> events = (List<Map<String, Object>>) (List<?>) list;
        try {
            var appended = pipeline.appendAndProcess(events);
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("accepted", appended.size());
            resp.put("firstOffset", appended.get(0).offset);
            resp.put("lastOffset", appended.get(appended.size() - 1).offset);
            resp.put("status", pipeline.status());
            sendJson(ex, 200, resp);
        } catch (InjectedFaultException crash) {
            onCrash(ex, crash);
        } catch (IllegalArgumentException e) {
            badRequest(ex, e.getMessage());
        }
    }

    private void forceCheckpoint(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) { method(ex, "POST"); return; }
        if (crashedGuard(ex)) return;
        try {
            long id = pipeline.flushCheckpoint();
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("checkpointId", id);
            resp.put("status", pipeline.status());
            sendJson(ex, 200, resp);
        } catch (InjectedFaultException crash) {
            onCrash(ex, crash);
        }
    }

    private void status(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) { method(ex, "GET"); return; }
        // 崩溃后仍允许读取：从磁盘重新打开只读视图不合适，这里返回崩溃前最后的状态说明。
        Map<String, Object> resp = new LinkedHashMap<>();
        if (crashInfo != null) {
            resp.put("state", "CRASHED");
            resp.put("crashedAt", crashInfo.getMessage());
            resp.put("hint", "调用 POST /recover 模拟进程重启恢复");
        } else {
            resp.put("state", "RUNNING");
            resp.putAll(pipeline.status());
        }
        sendJson(ex, 200, resp);
    }

    private void summary(HttpExchange ex) throws IOException {
        if (!"GET".equals(ex.getRequestMethod())) { method(ex, "GET"); return; }
        if (crashInfo == null) {
            sendJson(ex, 200, Json.obj(pipeline.status().get("summary")));
        } else {
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("error", "进程已崩溃，汇总表可能滞后但绝不包含未提交行；请 POST /recover");
            sendJson(ex, 503, resp);
        }
    }

    private void armFault(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) { method(ex, "POST"); return; }
        Map<String, Object> req;
        try {
            req = Json.obj(Json.parse(readBody(ex)));
        } catch (RuntimeException e) {
            badRequest(ex, "请求体必须是 JSON 对象: " + e.getMessage());
            return;
        }
        String phaseName = Json.str(req.get("phase"));
        if (phaseName == null) {
            badRequest(ex, "缺少 phase（STATE_WRITE/OUTPUT_STAGE/OUTPUT_COMMIT/TABLE_APPLY）");
            return;
        }
        try {
            FaultPhase phase = FaultPhase.parse(phaseName);
            long id = req.get("checkpointId") instanceof Number n ? n.longValue() : 0L;
            faults.arm(phase, id);
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("armed", phase.name() + "@" + (id <= 0 ? "next" : id));
            resp.put("allArms", faults.arms().keySet().stream().map(Enum::name).toList());
            sendJson(ex, 200, resp);
        } catch (IllegalArgumentException e) {
            badRequest(ex, e.getMessage());
        }
    }

    private void recover(HttpExchange ex) throws IOException {
        if (!"POST".equals(ex.getRequestMethod())) { method(ex, "POST"); return; }
        // 模拟新进程启动：不保留任何内存状态，复用同一 FaultSpec（可用于连续故障场景的二次注入前重新 arm）。
        openPipeline();
        long offset = pipeline.resume();
        // 与 CLI resume 一致：提交重放产生的尾部片段，使恢复后状态完全持久化
        // （否则不足一个检查点间隔的尾部只存在于内存，汇总表会落后于 nextOffset）。
        long checkpointId = pipeline.flushCheckpoint();
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("recovered", true);
        resp.put("resumedToOffset", offset);
        resp.put("checkpointId", checkpointId);
        resp.put("status", pipeline.status());
        sendJson(ex, 200, resp);
    }

    // ---- 工具 ----

    private boolean crashedGuard(HttpExchange ex) throws IOException {
        if (crashInfo != null) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("error", "进程已因注入故障终止（内存状态已丢弃）");
            err.put("crashedAt", crashInfo.getMessage());
            err.put("recovery", "POST /recover");
            sendJson(ex, 503, err);
            return true;
        }
        return false;
    }

    private void onCrash(HttpExchange ex, InjectedFaultException crash) throws IOException {
        // “杀死进程”：丢弃 Pipeline 内存引用，之后只有磁盘上的协议文件能恢复状态。
        this.crashInfo = crash;
        this.pipeline = null;
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("error", "INJECTED_CRASH");
        err.put("phase", crash.phase().name());
        err.put("checkpointId", crash.checkpointId());
        err.put("message", crash.getMessage());
        err.put("recovery", "POST /recover");
        sendJson(ex, 500, err);
    }

    private void method(HttpExchange ex, String expected) throws IOException {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("error", "方法不允许，期望 " + expected);
        sendJson(ex, 405, err);
    }

    private void badRequest(HttpExchange ex, String msg) throws IOException {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("error", msg);
        sendJson(ex, 400, err);
    }

    private String readBody(HttpExchange ex) throws IOException {
        byte[] bytes = ex.getRequestBody().readAllBytes();
        return new String(bytes, StandardCharsets.UTF_8);
    }

    private void sendJson(HttpExchange ex, int status, Object body) throws IOException {
        byte[] data = Json.pretty(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, data.length);
        try (OutputStream out = ex.getResponseBody()) {
            out.write(data);
        }
    }
}
