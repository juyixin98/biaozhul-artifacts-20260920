package dev.example.cp.cli;

import dev.example.cp.core.Event;
import dev.example.cp.engine.CheckpointScheduler;
import dev.example.cp.engine.Clock;
import dev.example.cp.engine.CountScheduler;
import dev.example.cp.engine.Engine;
import dev.example.cp.engine.ManualScheduler;
import dev.example.cp.fail.CrashPoint;
import dev.example.cp.fail.InjectedCrash;
import dev.example.cp.json.Json;
import dev.example.cp.storage.SourceLog;
import dev.example.cp.svc.HttpService;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 命令行入口（零依赖）。
 *
 * <pre>
 *  init     --data DIR
 *  load     --data DIR --file events.json          # 从 JSON 数组文件导入事件
 *  append   --data DIR --key a --value 1           # 追加单条事件
 *  run      --data DIR [--every N]                 # 消费并在结束时提交全部
 *  status   --data DIR                             # 打印可比较的状态 JSON
 *  fault    --data DIR --at STATE_WRITE --epoch 2 [--halt]
 *  disarm   --data DIR
 *  serve    --data DIR --port 8080
 * </pre>
 */
public final class Cli {

    public static void main(String[] args) {
        int code = new Cli().run(args);
        if (code != 0) {
            System.exit(code);
        }
    }

    int run(String[] args) {
        if (args.length == 0) {
            usage();
            return 2;
        }
        Map<String, String> opts = parseArgs(args);
        String cmd = args[0];
        try {
            switch (cmd) {
                case "init" -> {
                    Files.createDirectories(dataDir(opts));
                    System.out.println("initialized " + dataDir(opts));
                }
                case "load" -> doLoad(opts);
                case "append" -> doAppend(opts);
                case "run" -> doRun(opts);
                case "status" -> doStatus(opts);
                case "peek" -> doPeek(opts);
                case "fault" -> doFault(opts, true);
                case "disarm" -> doFault(opts, false);
                case "serve" -> doServe(opts);
                default -> {
                    usage();
                    return 2;
                }
            }
            return 0;
        } catch (InjectedCrash crash) {
            // halt=false 的进程内崩溃：文件现场已保留；以非零码退出便于脚本断言“崩溃确实发生”。
            System.err.println("[crash] " + crash.getMessage());
            return 99;
        } catch (IllegalArgumentException iae) {
            System.err.println("error: " + iae.getMessage());
            return 2;
        } catch (Exception e) {
            System.err.println("error: " + e);
            return 1;
        }
    }

    private void doLoad(Map<String, String> opts) throws Exception {
        Path file = Path.of(required(opts, "file"));
        String text = Files.readString(file, StandardCharsets.UTF_8).trim();
        List<Event> events = new ArrayList<>();
        if (text.startsWith("[")) {
            List<Object> arr = Json.parseArray(text);
            for (int i = 0; i < arr.size(); i++) {
                @SuppressWarnings("unchecked")
                Map<String, Object> item = (Map<String, Object>) arr.get(i);
                events.add(Event.fromJson(item, i));
            }
        } else {
            // JSON Lines
            int i = 0;
            for (String line : text.split("\\R")) {
                if (line.isBlank()) {
                    continue;
                }
                events.add(Event.fromJson(Json.parseObject(line), i++));
            }
        }
        long first = -1;
        long last = -1;
        SourceLog src = new SourceLog(dataDir(opts));
        for (Event e : events) {
            long off = src.append(e);
            if (first < 0) {
                first = off;
            }
            last = off;
        }
        System.out.println("loaded " + events.size() + " events, offsets " + first + ".." + last);
    }

    private void doAppend(Map<String, String> opts) {
        String key = required(opts, "key");
        long value = Long.parseLong(required(opts, "value"));
        long off = new SourceLog(dataDir(opts)).append(Event.of(-1, key, value));
        System.out.println("appended at offset " + off);
    }

    private void doRun(Map<String, String> opts) {
        CheckpointScheduler scheduler = opts.containsKey("every")
                ? new CountScheduler(Long.parseLong(opts.get("every")))
                : new ManualScheduler();
        Engine engine = Engine.open(dataDir(opts), scheduler, Clock.SYSTEM);
        Engine.RunReport report;
        if (opts.containsKey("max-events")) {
            long n = Long.parseLong(opts.get("max-events"));
            report = engine.runUntilEventCountAndCommitted(n);
        } else {
            report = engine.runUntilDrainedAndCommitted();
        }
        System.out.println("processed=" + report.eventsProcessed()
                + " checkpoints=" + report.checkpointsCompleted());
        printStatus(engine);
    }

    private void doStatus(Map<String, String> opts) {
        Engine engine = Engine.open(dataDir(opts), new ManualScheduler(), Clock.SYSTEM);
        printStatus(engine);
    }

    /** 只读查看磁盘现场：不触发恢复、不写任何文件（用于观察崩溃后的中间状态）。 */
    private void doPeek(Map<String, String> opts) {
        var reader = new dev.example.cp.engine.StatusReader(dataDir(opts));
        var t = reader.table();
        Map<String, Object> json = new LinkedHashMap<>();
        json.put("stateEpoch", reader.stateEpochOrZero());
        json.put("appliedEpoch", t.appliedEpoch());
        json.put("committedOffset", t.lastConsumedOffset());
        json.put("sums", t.sums());
        System.out.println(Json.writePretty(json).trim());
    }

    private void printStatus(Engine engine) {
        Engine.Status s = engine.status();
        Map<String, Object> json = new LinkedHashMap<>();
        json.put("latestEpoch", s.latestEpoch());
        json.put("lastConsumedOffset", s.lastConsumedOffset());
        json.put("appliedEpoch", s.appliedEpoch());
        json.put("committedOffset", s.committedOffset());
        json.put("sourceEvents", s.sourceEvents());
        json.put("processedCount", s.processedCount());
        json.put("sums", s.sums());
        System.out.println(Json.writePretty(json).trim());
    }

    private void doFault(Map<String, String> opts, boolean arm) {
        Path dir = dataDir(opts);
        Engine engine = Engine.open(dir, new ManualScheduler(), Clock.SYSTEM);
        if (!arm) {
            engine.faults().disarm();
            System.out.println("fault disarmed");
            return;
        }
        CrashPoint point = CrashPoint.parse(required(opts, "at"));
        long epoch = Long.parseLong(required(opts, "epoch"));
        boolean halt = Boolean.parseBoolean(opts.getOrDefault("halt", "false"));
        engine.faults().arm(point, epoch, halt);
        System.out.println("armed: at=" + point + " epoch=" + epoch + " halt=" + halt);
    }

    private void doServe(Map<String, String> opts) throws Exception {
        int port = Integer.parseInt(opts.getOrDefault("port", "8080"));
        HttpService service = new HttpService(dataDir(opts), port);
        service.start();
        System.out.println("listening on http://localhost:" + port);
        Thread.currentThread().join();
    }

    private Path dataDir(Map<String, String> opts) {
        return Path.of(required(opts, "data"));
    }

    private String required(Map<String, String> opts, String key) {
        String v = opts.get(key);
        if (v == null) {
            throw new IllegalArgumentException("missing required option --" + key);
        }
        return v;
    }

    /** 极简 --key value 解析（布尔存在性标志如 --halt 用空字符串值）。 */
    private Map<String, String> parseArgs(String[] args) {
        Map<String, String> m = new LinkedHashMap<>();
        for (int i = 1; i < args.length; i++) {
            String a = args[i];
            if (a.startsWith("--")) {
                String key = a.substring(2);
                if (i + 1 < args.length && !args[i + 1].startsWith("--")) {
                    m.put(key, args[++i]);
                } else {
                    m.put(key, "true");
                }
            }
        }
        return m;
    }

    private void usage() {
        System.err.println("""
                usage: cp-tx <command> --data <dir> [options]
                  init                                 初始化数据目录
                  load   --file events.json            导入事件（JSON 数组或 JSONL）
                  append --key <k> --value <n>         追加一条事件
                  run    [--every N] [--max-events M]   消费输入并在结束时提交
                  status                               打印偏移 / 汇总状态（打开即恢复）
                  peek                                 只读磁盘现场（不恢复，观察崩溃中间态）
                  fault  --at <POINT> --epoch <ID> [--halt]
                                                       注入一次性故障（STATE_WRITE / COMMIT_RENAME /
                                                       TABLE_APPLY / AFTER_COMMIT / PENDING_WRITE）
                  disarm                               清除已安排的故障
                  serve  [--port 8080]                 启动 HTTP JSON 服务
                """);
    }
}
