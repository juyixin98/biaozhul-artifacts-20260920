package com.example.cptx;

import com.example.cptx.core.CheckpointPolicy;
import com.example.cptx.core.Clock;
import com.example.cptx.core.Event;
import com.example.cptx.core.FaultPhase;
import com.example.cptx.core.FaultSpec;
import com.example.cptx.core.InjectedFaultException;
import com.example.cptx.core.Json;
import com.example.cptx.core.Pipeline;
import com.example.cptx.service.CheckpointHttpServer;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * 命令行入口。所有“崩溃/恢复”演示都通过独立 JVM 进程完成：
 * crash 命令在注入点以退出码 42 硬退出（模拟进程死亡，内存状态全部丢失），
 * resume 命令以全新进程从同一数据目录恢复。
 */
public final class Main {

    /** 注入故障的约定退出码（!= 0 表示失败中止，42 专门表示“按计划注入的崩溃”）。 */
    static final int EXIT_INJECTED_CRASH = 42;

    private Main() {}

    public static void main(String[] args) {
        try {
            run(args);
        } catch (InjectedFaultException e) {
            System.err.println();
            System.err.println("[进程终止] 注入故障命中 -> " + e.getMessage());
            System.err.println("内存状态已丢弃。请用 resume 命令以新进程恢复。");
            System.exit(EXIT_INJECTED_CRASH);
        } catch (Exception e) {
            System.err.println("错误: " + e.getMessage());
            e.printStackTrace(System.err);
            System.exit(1);
        }
    }

    private static void run(String[] args) throws Exception {
        if (args.length == 0) {
            usage();
            System.exit(2);
        }
        switch (args[0]) {
            case "serve" -> serve(args);
            case "ingest" -> ingest(args, false, false);
            case "baseline" -> ingest(args, true, false);
            case "crash" -> ingest(args, false, true);
            case "resume" -> resume(args);
            case "status" -> status(args);
            case "help", "-h", "--help" -> usage();
            default -> {
                System.err.println("未知命令: " + args[0]);
                usage();
                System.exit(2);
            }
        }
    }

    // ---- serve ----

    private static void serve(String[] args) throws Exception {
        Path dir = Path.of(arg(args, "--dir", 1, "data/service"));
        int port = Integer.parseInt(arg(args, "--port", 1, "8080"));
        long every = Long.parseLong(arg(args, "--every", 1, "5"));
        CheckpointHttpServer http = new CheckpointHttpServer(dir, every);
        http.start(port);
        System.out.println("服务已启动: http://localhost:" + http.getAddressPort());
        System.out.println("数据目录: " + dir.toAbsolutePath());
        System.out.println("检查点策略: 每处理 " + every + " 条事件");
        // 阻塞直到被外部终止
        Thread.currentThread().join();
    }

    // ---- ingest / baseline / crash ----

    private static void ingest(String[] args, boolean baseline, boolean crashing) throws Exception {
        Path dir = Path.of(arg(args, "--dir", 1, "data/run"));
        Path eventsFile = Path.of(requireArg(args, "--events"));
        long every = Long.parseLong(arg(args, "--every", 1, "5"));

        List<Map<String, Object>> payloads = loadEventsFile(eventsFile);
        FaultSpec faults = crashing ? parseFaults(args) : new FaultSpec();

        Clock clock = System::currentTimeMillis;
        Pipeline pipeline = new Pipeline(dir, CheckpointPolicy.count(every), clock, faults);
        List<Event> appended = pipeline.appendAndProcess(payloads);
        long id = pipeline.flushCheckpoint();

        System.out.println((baseline ? "基线" : "崩溃运行") + "完成: 接收 " + appended.size()
                + " 条，最终检查点 " + id + "，nextOffset=" + pipeline.nextOffset());
        System.out.println(Json.pretty(pipeline.status()));
    }

    // ---- resume ----

    private static void resume(String[] args) throws Exception {
        Path dir = Path.of(arg(args, "--dir", 1, "data/run"));
        long every = Long.parseLong(arg(args, "--every", 1, "5"));

        // --fault 可选：用于“恢复后再次崩溃”的双故障场景；省略时即干净恢复。
        FaultSpec faults = hasFault(args) ? parseFaults(args) : new FaultSpec();
        Clock clock = System::currentTimeMillis;
        Pipeline pipeline = new Pipeline(dir, CheckpointPolicy.count(every), clock, faults);
        long offset = pipeline.resume();
        long id = pipeline.flushCheckpoint();

        System.out.println("恢复完成: nextOffset=" + offset + "，最终检查点 " + id);
        System.out.println(Json.pretty(pipeline.status()));
    }

    // ---- status ----

    private static void status(String[] args) throws Exception {
        Path dir = Path.of(arg(args, "--dir", 1, "data/run"));
        long every = Long.parseLong(arg(args, "--every", 1, "5"));
        Pipeline pipeline = new Pipeline(dir, CheckpointPolicy.count(every), System::currentTimeMillis, new FaultSpec());
        System.out.println(Json.pretty(pipeline.status()));
    }

    // ---- 参数与文件 ----

    /** 是否给出了至少一个 --fault。 */
    private static boolean hasFault(String[] args) {
        for (int i = 1; i < args.length; i++) {
            if (args[i].equals("--fault")) return true;
        }
        return false;
    }

    /** 解析可重复的 --fault PHASE[:ID]，可给多个（用于双故障）。 */
    private static FaultSpec parseFaults(String[] args) {
        FaultSpec spec = new FaultSpec();
        boolean any = false;
        for (int i = 1; i + 1 < args.length; i++) {
            if (args[i].equals("--fault")) {
                String token = args[i + 1];
                int colon = token.indexOf(':');
                FaultPhase phase = FaultPhase.parse(colon < 0 ? token : token.substring(0, colon));
                long id = colon < 0 ? 0L : Long.parseLong(token.substring(colon + 1));
                if (id <= 0) spec.armNext(phase); else spec.arm(phase, id);
                any = true;
                i++;
            }
        }
        if (!any) {
            throw new IllegalArgumentException("crash/resume 需要至少一个 --fault PHASE[:ID]");
        }
        return spec;
    }

    /** 事件文件支持两种形态：[{"key","value"},...] 或 {"events":[...]}。 */
    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> loadEventsFile(Path file) throws Exception {
        if (!Files.exists(file)) {
            throw new IllegalArgumentException("事件文件不存在: " + file);
        }
        Object parsed = Json.parse(Files.readString(file, StandardCharsets.UTF_8));
        Object raw;
        if (parsed instanceof Map<?, ?> m && m.containsKey("events")) {
            raw = m.get("events");
        } else {
            raw = parsed;
        }
        if (!(raw instanceof List<?> list) || list.isEmpty()) {
            throw new IllegalArgumentException("事件文件必须是非空 JSON 数组");
        }
        List<Map<String, Object>> out = new ArrayList<>();
        for (Object o : list) {
            out.add((Map<String, Object>) o);
        }
        return out;
    }

    private static String requireArg(String[] args, String name) {
        for (int i = 1; i + 1 < args.length; i++) {
            if (args[i].equals(name)) return args[i + 1];
        }
        throw new IllegalArgumentException("缺少必需参数 " + name);
    }

    private static String arg(String[] args, String name, int offset, String def) {
        for (int i = 1; i + offset < args.length; i++) {
            if (args[i].equals(name)) return args[i + offset];
        }
        return def;
    }

    private static void usage() {
        System.out.println("""
            用法: cptx <命令> [参数]

            命令:
              serve    --dir 数据目录 --port 8080 --every 5     启动 JSON HTTP 服务
              ingest   --dir 数据目录 --events 文件 --every 5   正常载入事件（无故障）
              baseline --dir 数据目录 --events 文件 --every 5   生成连续执行基线
              crash    --dir 数据目录 --events 文件 --every 5 \\
                       --fault PHASE[:ID] [--fault PHASE2[:ID2]]
                       载入事件并在注入点“崩溃”（退出码 42）
              resume   --dir 数据目录 --every 5 [--fault PHASE[:ID]]
                       新进程恢复：丢弃暂存、从最近检查点继续、重放日志
              status   --dir 数据目录                          打印偏移/检查点/汇总

            PHASE = STATE_WRITE | OUTPUT_STAGE | OUTPUT_COMMIT | TABLE_APPLY
            ID 为检查点号（事务号）；省略 ID 表示下一次检查点即命中。
            """);
    }
}
