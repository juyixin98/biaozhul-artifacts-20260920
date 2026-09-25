package dev.example.cp.fail;

import dev.example.cp.json.Json;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 一次性故障注入器：规则持久化在 {@code faults/injected.json}，因此跨进程也生效。
 *
 * <p>规则字段：
 * <ul>
 *   <li>{@code at} —— {@link CrashPoint} 名称</li>
 *   <li>{@code epoch} —— 在哪个 epoch（检查点 id）触发；与当前 epoch 相等才触发</li>
 *   <li>{@code halt} —— true=Runtime.halt 真杀进程；false=抛 {@link InjectedCrash}（默认）</li>
 * </ul>
 *
 * 触发规则是一次性的：触发后立即删除规则文件，使“恢复后的重试”能继续完成，
 * 这正好建模真实系统里“故障被人工清除 / 机器重启后不会无限死在同一条指令上”。
 */
public final class FailureInjector {

    private final Path ruleFile;
    private volatile Rule rule;

    public FailureInjector(Path dataDir) {
        this.ruleFile = dataDir.resolve("faults").resolve("injected.json");
        this.rule = load();
    }

    public record Rule(CrashPoint at, long epoch, boolean halt) {
    }

    /** 安排一次注入（持久化，进程重启仍然生效）。 */
    public synchronized void arm(CrashPoint at, long epoch, boolean halt) {
        Map<String, Object> json = new LinkedHashMap<>();
        json.put("at", at.name());
        json.put("epoch", epoch);
        json.put("halt", halt);
        try {
            Files.createDirectories(ruleFile.getParent());
            Files.writeString(ruleFile, Json.write(json) + System.lineSeparator(), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new RuntimeException("cannot arm fault", e);
        }
        this.rule = new Rule(at, epoch, halt);
    }

    /** 清除注入规则。 */
    public synchronized void disarm() {
        try {
            Files.deleteIfExists(ruleFile);
        } catch (IOException e) {
            throw new RuntimeException("cannot disarm fault", e);
        }
        this.rule = null;
    }

    /** 当前是否还有待触发的规则。 */
    public synchronized boolean isArmed() {
        return rule != null;
    }

    /**
     * 在检查点处理的某个阶段调用。若规则匹配（点 + epoch），触发故障：
     * halt 模式直接终止 JVM；否则删除规则并抛出 {@link InjectedCrash}。
     */
    public synchronized void crashIfArmed(CrashPoint point, long epochId) {
        Rule r = rule;
        if (r == null) {
            return;
        }
        if (r.at() == point && r.epoch() == epochId) {
            if (r.halt()) {
                // 先尝试删除规则文件（尽力而为）：若没删掉，恢复后会在重试时再次崩溃，
                // 这是“持久性故障”的建模；CLI 测试用一次性规则，因此先删除。
                try {
                    Files.deleteIfExists(ruleFile);
                } catch (IOException ignored) {
                    // 尽力而为
                }
                System.err.println("[fault] HALT at " + point + " epoch " + epochId);
                System.err.flush();
                Runtime.getRuntime().halt(99);
            }
            this.rule = null;
            try {
                Files.deleteIfExists(ruleFile);
            } catch (IOException ignored) {
                // 删除失败也按已消费处理（进程内模式）
            }
            throw new InjectedCrash(point, epochId);
        }
    }

    private Rule load() {
        if (!Files.exists(ruleFile)) {
            return null;
        }
        try {
            Map<String, Object> json = Json.parseObject(Files.readString(ruleFile, StandardCharsets.UTF_8));
            return new Rule(
                    CrashPoint.parse(Json.str(json, "at")),
                    Json.lng(json, "epoch"),
                    Boolean.TRUE.equals(json.get("halt")));
        } catch (IOException e) {
            throw new RuntimeException("cannot read fault rule", e);
        }
    }
}
