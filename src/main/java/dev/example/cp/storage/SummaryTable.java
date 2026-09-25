package dev.example.cp.storage;

import dev.example.cp.engine.OutputRecord;
import dev.example.cp.fail.CrashPoint;
import dev.example.cp.fail.FailureInjector;
import dev.example.cp.json.Json;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.TreeMap;

/**
 * 受控的本地汇总表（{@code output/table.json}）。
 *
 * <p>这是一个<b>被本系统控制</b>的提交目标：应用一个已提交 epoch 的方式是
 * 用该 epoch 携带的完整快照原子重写整个表文件，并在同一次原子写入里推进
 * {@code appliedEpoch}。因此“应用输出”本身具备事务语义：
 * 崩溃后要么停在旧 epoch（重放时再应用一次），要么已是新 epoch（重放自动跳过），
 * 天然幂等，<b>无重复提交</b>。
 *
 * <p><b>边界：</b>该保证依赖于“提交目标是我们能用一次原子 rename 重写的本地文件”。
 * 对任意外部副作用（发 HTTP 请求、发邮件、调支付、向无法回滚的第三方系统写数据），
 * 本协议<b>不</b>提供 exactly-once；详见 README 的“适用边界”章节。
 */
public final class SummaryTable {

    private final Path tableFile;
    private final FailureInjector faults;

    public SummaryTable(Path dataDir, FailureInjector faults) {
        this.tableFile = dataDir.resolve("output").resolve("table.json");
        this.faults = faults;
    }

    /** 当前表状态；不存在返回空表（appliedEpoch=-1, committedOffset=-1）。 */
    public synchronized TableState load() {
        if (!Files.exists(tableFile)) {
            return new TableState(-1L, -1L, new TreeMap<>());
        }
        try {
            Map<String, Object> json = Json.parseObject(Files.readString(tableFile, StandardCharsets.UTF_8));
            long applied = Json.lngOr(json, "appliedEpoch", -1L);
            long committedOffset = Json.lngOr(json, "lastConsumedOffset", -1L);
            @SuppressWarnings("unchecked")
            Map<String, Object> raw = (Map<String, Object>) json.getOrDefault("sums", new LinkedHashMap<>());
            TreeMap<String, Long> sums = new TreeMap<>();
            raw.forEach((k, v) -> sums.put(k, ((Number) v).longValue()));
            return new TableState(applied, committedOffset, sums);
        } catch (IOException e) {
            throw new StorageException("load summary table failed", e);
        }
    }

    /**
     * 幂等地应用一个已提交的 epoch：
     * <ul>
     *   <li>{@code rec.epochId <= appliedEpoch}：已经应用过，直接跳过（重复提交安全）；</li>
     *   <li>{@code rec.epochId > appliedEpoch}：原子重写表并推进 appliedEpoch。</li>
     * </ul>
     * 调用方必须按 epoch 升序调用（恢复协调器保证这一点）。
     *
     * @return true 表示本次真正应用；false 表示因已应用而跳过
     */
    public synchronized boolean apply(OutputRecord rec) {
        TableState current = load();
        if (rec.epochId() <= current.appliedEpoch()) {
            return false;
        }
        // —— 故障点：输出已提交(committed 可见)、但汇总表应用尚未落盘 ——
        faults.crashIfArmed(CrashPoint.TABLE_APPLY, rec.epochId());

        Map<String, Object> json = new LinkedHashMap<>();
        json.put("appliedEpoch", rec.epochId());
        json.put("lastConsumedOffset", rec.lastConsumedOffset());
        TreeMap<String, Long> sums = new TreeMap<>(rec.sumsAfter());
        json.put("sums", sums);
        DurableFiles.writeAtomic(tableFile, Json.writePretty(json));
        return true;
    }

    /** 汇总表当前内容。 */
    public record TableState(long appliedEpoch, long lastConsumedOffset, TreeMap<String, Long> sums) {
    }
}
