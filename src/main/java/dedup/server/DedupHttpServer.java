package dedup.server;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import dedup.core.Event;
import dedup.json.Json;
import dedup.state.FileSnapshotStore;
import dedup.state.SnapshotStore;
import dedup.stream.OutputEntry;
import dedup.stream.StreamProcessor;
import dedup.time.Clock;
import dedup.time.TaskScheduler;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.List;
import java.util.concurrent.Executors;

/**
 * JSON over HTTP 服务（JDK 内置 {@link HttpServer}，无外部依赖）。
 *
 * <p>端点：
 * <pre>
 * POST /events        发送单个事件        {"id","key"?,"eventTime","payload"?}
 * POST /events/batch  发送一批事件        {"events":[ ... ]}
 * POST /watermark     manual 模式推进水位线 {"watermark": 12345}
 * POST /tick          bounded 模式触发周期推进 {}
 * GET  /outputs?sinceSeq=N&drain=true 读取已发射事件
 * GET  /stats         可观察计数器
 * POST /checkpoint    立即落盘
 * POST /admin/reset   清空内存与快照
 * GET  /health        健康检查
 * </pre>
 *
 * 判定结果三态 {@code ACCEPT / SUPPRESS / UNGUARANTEED}，
 * 每条输出带 {@code dedupGuaranteed} 与 {@code payloadMismatch} 可观察标志。
 */
public final class DedupHttpServer {

    private static final int MAX_BODY = 2 * 1024 * 1024;

    private final HttpServer server;
    private final StreamProcessor processor;
    private final TaskScheduler scheduler;
    private final TaskScheduler.Cancellable tickHandle;

    public DedupHttpServer(int port, StreamProcessor processor,
                           TaskScheduler scheduler, long tickPeriodMillis) throws IOException {
        this.processor = processor;
        this.scheduler = scheduler;
        this.server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        server.setExecutor(Executors.newFixedThreadPool(4, r -> {
            Thread t = new Thread(r, "dedup-http");
            t.setDaemon(true);
            return t;
        }));
        registerRoutes();
        this.tickHandle = scheduler.schedulePeriodic(tickPeriodMillis, tickPeriodMillis,
                processor::tick);
    }

    public void start() {
        server.start();
    }

    public int port() {
        return server.getAddress().getPort();
    }

    /** 关闭：先落盘再停止 HTTP 与调度（重启恢复依赖这一步）。 */
    public void stop() {
        try {
            processor.checkpoint();
        } catch (RuntimeException ignored) {
            // 关闭路径上的快照失败不阻止停机
        }
        tickHandle.cancel();
        server.stop(0);
    }

    private void registerRoutes() {
        server.createContext("/health", ex -> handle(ex, this::health));
        server.createContext("/events/batch", ex -> handle(ex, this::batch));
        server.createContext("/events", ex -> handle(ex, this::events));
        server.createContext("/watermark", ex -> handle(ex, this::watermark));
        server.createContext("/tick", ex -> handle(ex, this::tick));
        server.createContext("/outputs", ex -> handle(ex, this::outputs));
        server.createContext("/stats", ex -> handle(ex, this::stats));
        server.createContext("/checkpoint", ex -> handle(ex, this::checkpoint));
        server.createContext("/admin/reset", ex -> handle(ex, this::reset));
    }

    // ------------------------------------------------------------------
    // 端点
    // ------------------------------------------------------------------

    private Json.Value health(HttpExchange ex) {
        Json.Obj o = new Json.Obj();
        o.put("status", new Json.Str("ok"));
        return o;
    }

    private Json.Value events(HttpExchange ex) throws IOException {
        requirePost(ex);
        Json.Obj body = readJsonObject(ex);
        Event event = Event.fromJson(body);
        var result = processor.process(event);
        Json.Obj o = new Json.Obj();
        o.put("result", result.toJson());
        return o;
    }

    private Json.Value batch(HttpExchange ex) throws IOException {
        requirePost(ex);
        Json.Obj body = readJsonObject(ex);
        Json.Value evs = body.get("events");
        if (!(evs instanceof Json.Arr arr)) {
            throw new Json.JsonException("batch 请求必须包含 events 数组");
        }
        Json.Arr results = new Json.Arr();
        for (Json.Value v : arr) {
            if (!(v instanceof Json.Obj eo)) {
                throw new Json.JsonException("events 数组元素必须是对象");
            }
            results.add(processor.process(Event.fromJson(eo)).toJson());
        }
        Json.Obj o = new Json.Obj();
        o.put("count", Json.Num.of(results.size()));
        o.put("results", results);
        return o;
    }

    private Json.Value watermark(HttpExchange ex) throws IOException {
        requirePost(ex);
        Json.Obj body = readJsonObject(ex);
        Json.Value w = body.get("watermark");
        if (w == null) throw new Json.JsonException("缺少字段 watermark（整数毫秒）");
        long wm = Json.longExact(w, "watermark");
        boolean advanced = processor.setManualWatermark(wm);
        Json.Obj o = new Json.Obj();
        o.put("advanced", new Json.Bool(advanced));
        o.put("watermark", processor.deduplicator().currentWatermark() == null
                ? Json.Nul.INSTANCE
                : Json.Num.of(processor.deduplicator().currentWatermark()));
        if (!advanced) {
            o.put("note", new Json.Str("水位线必须单调不减：回退被忽略（时钟回退安全）"));
        }
        return o;
    }

    private Json.Value tick(HttpExchange ex) throws IOException {
        requirePost(ex);
        readJsonObject(ex); // 允许空对象体
        Long newWm = processor.tick();
        Json.Obj o = new Json.Obj();
        o.put("advanced", new Json.Bool(newWm != null));
        o.put("watermark", newWm == null
                ? (processor.deduplicator().currentWatermark() == null
                    ? Json.Nul.INSTANCE
                    : Json.Num.of(processor.deduplicator().currentWatermark()))
                : Json.Num.of(newWm));
        return o;
    }

    private Json.Value outputs(HttpExchange ex) {
        long sinceSeq = 0;
        boolean drain = false;
        String query = ex.getRequestURI().getRawQuery();
        if (query != null) {
            for (String pair : query.split("&")) {
                int eq = pair.indexOf('=');
                String k = eq < 0 ? pair : pair.substring(0, eq);
                String val = eq < 0 ? "" : pair.substring(eq + 1);
                if ("sinceSeq".equals(k)) {
                    try {
                        sinceSeq = Long.parseLong(val.isEmpty() ? "0" : val);
                    } catch (NumberFormatException nfe) {
                        throw new Json.JsonException("sinceSeq 必须是整数");
                    }
                } else if ("drain".equals(k)) {
                    drain = "true".equalsIgnoreCase(val) || "1".equals(val);
                }
            }
        }
        List<OutputEntry> entries = processor.drainOutputs(sinceSeq, drain);
        Json.Arr arr = new Json.Arr();
        for (OutputEntry e : entries) {
            arr.add(e.toJson());
        }
        Json.Obj o = new Json.Obj();
        o.put("count", Json.Num.of(arr.size()));
        o.put("outputs", arr);
        return o;
    }

    private Json.Value stats(HttpExchange ex) {
        return processor.statsJson();
    }

    private Json.Value checkpoint(HttpExchange ex) throws IOException {
        requirePost(ex);
        readJsonObject(ex);
        processor.checkpoint();
        Json.Obj o = new Json.Obj();
        o.put("saved", new Json.Bool(true));
        return o;
    }

    private Json.Value reset(HttpExchange ex) throws IOException {
        requirePost(ex);
        readJsonObject(ex);
        processor.reset();
        Json.Obj o = new Json.Obj();
        o.put("reset", new Json.Bool(true));
        return o;
    }

    // ------------------------------------------------------------------
    // HTTP 胶水
    // ------------------------------------------------------------------

    private interface HandlerFn {
        Json.Value apply(HttpExchange ex) throws IOException;
    }

    private void handle(HttpExchange ex, HandlerFn fn) {
        try {
            Json.Value v = fn.apply(ex);
            writeJson(ex, 200, v);
        } catch (Json.JsonException | IllegalArgumentException e) {
            writeError(ex, 400, e.getMessage());
        } catch (Exception e) {
            writeError(ex, 500, "内部错误: " + e);
        }
    }

    private static void requirePost(HttpExchange ex) throws IOException {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            throw new Json.JsonException("该端点只接受 POST");
        }
    }

    private Json.Obj readJsonObject(HttpExchange ex) throws IOException {
        if (!"POST".equalsIgnoreCase(ex.getRequestMethod())) {
            throw new Json.JsonException("该端点只接受 POST");
        }
        byte[] body = ex.getRequestBody().readNBytes(MAX_BODY + 1);
        if (body.length > MAX_BODY) {
            throw new Json.JsonException("请求体超过 2 MiB 上限");
        }
        String text = new String(body, StandardCharsets.UTF_8).trim();
        if (text.isEmpty()) {
            return new Json.Obj(); // 空体视为空对象（如 /tick {}）
        }
        Json.Value v = Json.parse(text);
        if (!(v instanceof Json.Obj o)) {
            throw new Json.JsonException("请求体必须是 JSON 对象");
        }
        return o;
    }

    private static void writeJson(HttpExchange ex, int status, Json.Value v) throws IOException {
        byte[] bytes = Json.write(v).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    private static void writeError(HttpExchange ex, int status, String message) {
        try {
            Json.Obj o = new Json.Obj();
            o.put("error", new Json.Str(message == null ? "未知错误" : message));
            byte[] bytes = Json.write(o).getBytes(StandardCharsets.UTF_8);
            ex.getResponseHeaders().add("Content-Type", "application/json; charset=utf-8");
            ex.sendResponseHeaders(status, bytes.length);
            try (OutputStream os = ex.getResponseBody()) {
                os.write(bytes);
            }
        } catch (IOException ignored) {
            ex.close();
        }
    }

    // ------------------------------------------------------------------
    // main
    // ------------------------------------------------------------------

    @SuppressWarnings("CallToPrintStackTrace")
    public static void main(String[] args) throws Exception {
        int port = 8080;
        long allowedLateness = 5_000;
        int maxTombstones = 100_000;
        String mode = "manual";
        long outOfOrderness = 2_000;
        long tickPeriod = 1_000;
        Path stateFile = Path.of("state", "snapshot.json");

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--port" -> port = Integer.parseInt(args[++i]);
                case "--allowed-lateness-ms" -> allowedLateness = Long.parseLong(args[++i]);
                case "--max-tombstones" -> maxTombstones = Integer.parseInt(args[++i]);
                case "--watermark-mode" -> mode = args[++i];
                case "--out-of-orderness-ms" -> outOfOrderness = Long.parseLong(args[++i]);
                case "--tick-period-ms" -> tickPeriod = Long.parseLong(args[++i]);
                case "--state-file" -> stateFile = Path.of(args[++i]);
                default -> {
                    System.err.println("未知参数: " + args[i]);
                    System.err.println("用法: [--port N] [--allowed-lateness-ms N] "
                            + "[--max-tombstones N] [--watermark-mode manual|bounded] "
                            + "[--out-of-orderness-ms N] [--tick-period-ms N] [--state-file PATH]");
                    System.exit(2);
                }
            }
        }

        var config = new StreamProcessor.Config(
                allowedLateness, maxTombstones, mode, outOfOrderness, 10_000);
        final Path stateFilePath = stateFile;
        Clock clock = Clock.system();
        SnapshotStore store = new FileSnapshotStore(stateFilePath);
        StreamProcessor processor = StreamProcessor.create(config, clock, store);
        TaskScheduler scheduler = TaskScheduler.system(clock);
        DedupHttpServer http = new DedupHttpServer(port, processor, scheduler, tickPeriod);
        http.start();

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            http.stop();
            System.out.println("已落盘快照并关闭: " + stateFilePath.toAbsolutePath());
        }));

        System.out.println("乱序去重与墓碑服务已启动");
        System.out.println("  监听:       http://127.0.0.1:" + http.port());
        System.out.println("  水位线模式: " + mode
                + (mode.equals("bounded") ? " (outOfOrderness=" + outOfOrderness
                + ", tick=" + tickPeriod + "ms)" : "（POST /watermark 手动推进）"));
        System.out.println("  迟到窗口:   " + allowedLateness + " ms");
        System.out.println("  墓碑上限:   " + maxTombstones);
        System.out.println("  快照文件:   " + stateFilePath.toAbsolutePath());
    }
}
