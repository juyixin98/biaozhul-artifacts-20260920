package com.example.monotime;

import com.example.monotime.api.ApiResponse;
import com.example.monotime.api.JsonMappers;
import com.example.monotime.data.RuleCatalog;
import com.example.monotime.domain.DeadlineCalculator;
import com.example.monotime.domain.MonotonicConverter;
import com.example.monotime.domain.ScheduledTimeout;
import com.example.monotime.domain.TimeRule;
import com.example.monotime.domain.TimeoutStatus;
import com.example.monotime.persistence.JsonTimeoutStore;
import com.example.monotime.persistence.PersistedTimeout;
import com.example.monotime.scenario.ScenarioDefinition;
import com.example.monotime.scenario.ScenarioReport;
import com.example.monotime.scenario.ScenarioRunner;
import com.example.monotime.tzdb.TzdbInfo;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * 纯后端命令行入口。无前端、无 HTTP 服务。
 *
 * <p>用法见 {@code help}。所有输出均为 JSON（stdout），错误也以统一封装返回并以非零码退出。
 * 固定测试数据来自 {@link RuleCatalog}；持久化为本地 JSON 文件目录。</p>
 */
public final class App {

    private static final ObjectMapper MAPPER = JsonMappers.create();
    private static final Path DEFAULT_STORE = Path.of("data", "store");
    /** 场景脚本默认可重复执行且会重置自身记录，使用与生产存储隔离的目录。 */
    private static final Path DEFAULT_SCENARIO_STORE = Path.of("data", "store-scenario");

    private App() {
    }

    public static void main(String[] args) {
        try {
            Object result = execute(args);
            System.out.println(MAPPER.writerWithDefaultPrettyPrinter().writeValueAsString(ApiResponse.ok(result)));
        } catch (IllegalArgumentException e) {
            // 输入/业务错误：消息面向用户，可安全返回。
            System.out.println(failureJson(e.getMessage()));
            System.exit(2);
        } catch (Exception e) {
            // 非预期错误：详情（可能含本地路径等内部信息）进 stderr，stdout 只给通用消息。
            System.err.println("内部错误: " + e.getClass().getSimpleName() + ": " + e.getMessage());
            System.out.println(failureJson("内部错误，请检查 stderr 日志或输入文件"));
            System.exit(1);
        }
    }

    /** 测试入口：执行命令并返回成功载荷；参数/业务错误以异常抛出，不调用 System.exit。 */
    public static Object execute(String[] args) throws IOException {
        Map<String, String> opts = parseOptions(args);
        String command = args.length == 0 ? "help" : args[0];
        return switch (command) {
            case "help" -> help();
            case "info" -> info();
            case "catalog" -> new RuleCatalog().all();
            case "schedule" -> schedule(opts);
            case "status" -> status(opts);
            case "list" -> store(opts).findAll();
            case "scenario" -> scenario(opts);
            case "request" -> request(opts);
            default -> throw new IllegalArgumentException("未知命令: " + command);
        };
    }

    static String failureJson(String message) {
        try {
            return MAPPER.writerWithDefaultPrettyPrinter().writeValueAsString(ApiResponse.failure(message));
        } catch (Exception ignored) {
            // message 理论上总能被 Jackson 序列化；万一不能，也绝不手工拼接出非法 JSON。
            return "{\"success\":false,\"data\":null,\"error\":\"unserializable error message\"}";
        }
    }

    private static Map<String, Object> info() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("tzdb", TzdbInfo.detect());
        out.put("ruleCount", new RuleCatalog().all().size());
        out.put("note", "单调刻度仅在单个 JVM 生命周期内有效；重启后必须依据墙钟截止瞬间重新计算。");
        return out;
    }

    private static ScheduledTimeout schedule(Map<String, String> opts) throws IOException {
        String id = require(opts, "id");
        String ruleId = require(opts, "rule");
        RuleCatalog catalog = new RuleCatalog();
        TimeRule rule = (opts.containsKey("version")
                ? catalog.find(ruleId, opts.get("version"))
                : catalog.findLatest(ruleId))
                .orElseThrow(() -> new IllegalArgumentException("规则不存在: " + ruleId));
        JsonTimeoutStore store = store(opts);
        store.initialize();
        MonotonicConverter converter = MonotonicConverter.create(new SystemClockPort());
        ScheduledTimeout scheduled = converter.schedule(id, rule);
        store.save(scheduled);
        return scheduled;
    }

    private static TimeoutStatus status(Map<String, String> opts) throws IOException {
        String id = require(opts, "id");
        JsonTimeoutStore store = store(opts);
        PersistedTimeout persisted = store.find(id)
                .orElseThrow(() -> new IllegalArgumentException("持久化记录不存在: " + id));
        // 每次 CLI 调用都是新 JVM：演示“重启后重新计算”——在同一时钟快照上
        // 用墙钟截止瞬间重新锚定单调死线并立即查询。
        MonotonicConverter converter = MonotonicConverter.create(new SystemClockPort());
        ScheduledTimeout wallFields = new ScheduledTimeout(
                persisted.timeoutId(), persisted.ruleId(), persisted.ruleVersion(),
                persisted.scheduledAtInstant(), persisted.deadlineInstant(),
                0L, 0L, 0L, false, false, persisted.scheduledTzdbVersion());
        return converter.recoverAndStatus(wallFields);
    }

    private static ScenarioReport scenario(Map<String, String> opts) throws IOException {
        JsonNode input = readJsonInput(opts);
        ScenarioDefinition definition = MAPPER.treeToValue(input, ScenarioDefinition.class);
        validate(definition);
        JsonTimeoutStore store = new JsonTimeoutStore(
                Path.of(opts.getOrDefault("store", DEFAULT_SCENARIO_STORE.toString())), MAPPER);
        return new ScenarioRunner(new RuleCatalog(), store).run(definition);
    }

    /**
     * 通用 JSON 请求：从文件或 stdin 读取
     * <pre>{"command":"scenario|info|catalog|schedule|status|list", ...字段}</pre>
     */
    private static Object request(Map<String, String> opts) throws IOException {
        JsonNode node = MAPPER.readTree(readInput(opts));
        String command = node.path("command").asText(null);
        if (command == null) {
            throw new IllegalArgumentException("JSON 请求缺少 command 字段");
        }
        return switch (command) {
            case "info" -> info();
            case "catalog" -> new RuleCatalog().all();
            case "list" -> storeFromNode(node).findAll();
            case "schedule" -> {
                Map<String, String> mapped = new LinkedHashMap<>();
                mapped.put("id", node.path("timeoutId").asText(null));
                mapped.put("rule", node.path("ruleId").asText(null));
                if (node.hasNonNull("version")) {
                    mapped.put("version", node.path("version").asText());
                }
                mapped.put("store", node.path("store").asText(DEFAULT_STORE.toString()));
                yield schedule(mapped);
            }
            case "scenario" -> {
                if (!node.hasNonNull("scenario")) {
                    throw new IllegalArgumentException("JSON scenario 请求缺少 scenario 字段");
                }
                ScenarioDefinition definition = MAPPER.treeToValue(node.get("scenario"), ScenarioDefinition.class);
                validate(definition);
                JsonTimeoutStore store = new JsonTimeoutStore(
                        Path.of(node.path("store").asText(DEFAULT_SCENARIO_STORE.toString())), MAPPER);
                yield new ScenarioRunner(new RuleCatalog(), store).run(definition);
            }
            default -> throw new IllegalArgumentException("JSON 请求不支持的 command: " + command);
        };
    }

    private static JsonTimeoutStore storeFromNode(JsonNode node) {
        return new JsonTimeoutStore(Path.of(node.path("store").asText(DEFAULT_STORE.toString())), MAPPER);
    }

    private static JsonTimeoutStore store(Map<String, String> opts) {
        return new JsonTimeoutStore(Path.of(opts.getOrDefault("store", DEFAULT_STORE.toString())), MAPPER);
    }

    private static JsonNode readJsonInput(Map<String, String> opts) throws IOException {
        return MAPPER.readTree(readInput(opts));
    }

    private static String readInput(Map<String, String> opts) throws IOException {
        if (opts.containsKey("file")) {
            return Files.readString(Path.of(opts.get("file")));
        }
        return new String(System.in.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
    }

    private static void validate(ScenarioDefinition definition) {
        if (definition.initialWallClock() == null) {
            throw new IllegalArgumentException("场景缺少 initialWallClock");
        }
        if (definition.steps() == null || definition.steps().isEmpty()) {
            throw new IllegalArgumentException("场景缺少 steps");
        }
    }

    private static String require(Map<String, String> opts, String key) {
        String value = opts.get(key);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("缺少必填参数 --" + key);
        }
        return value;
    }
    private static Map<String, String> parseOptions(String[] args) {
        Map<String, String> opts = new LinkedHashMap<>();
        for (int i = 1; i < args.length; i++) {
            String a = args[i];
            if (a.startsWith("--")) {
                String key = a.substring(2);
                // 裸标记（后面没有值）记为 null，由必填校验给出人类可读错误，而不是当成字面量。
                String value = null;
                if (i + 1 < args.length && !args[i + 1].startsWith("--")) {
                    value = args[++i];
                }
                opts.put(key, value);
            }
        }
        return opts;
    }

    private static Map<String, Object> help() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("service", "monotonic-timeout — 墙钟截止时间到单调计时器转换服务（纯后端）");
        out.put("commands", List.of(
                Map.of("name", "info", "desc", "时区数据库(TZDB)版本与运行环境信息"),
                Map.of("name", "catalog", "desc", "列出本地固定规则目录"),
                Map.of("name", "schedule", "desc", "--id <超时ID> --rule <规则ID> [--version <版本>] [--store <目录>]"),
                Map.of("name", "status", "desc", "--id <超时ID> [--store <目录>]（本进程即新 JVM，按重启恢复语义重算）"),
                Map.of("name", "list", "desc", "列出持久化记录（仅墙钟域，无单调刻度）"),
                Map.of("name", "scenario", "desc", "--file <场景JSON> 或 stdin，确定性执行校时/流逝/重启脚本"),
                Map.of("name", "request", "desc", "通用 JSON 请求（--file 或 stdin）")));
        return out;
    }
}
