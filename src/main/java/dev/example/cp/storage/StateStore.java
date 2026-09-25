package dev.example.cp.storage;

import dev.example.cp.core.OperatorSnapshot;
import dev.example.cp.engine.CheckpointState;
import dev.example.cp.fail.CrashPoint;
import dev.example.cp.fail.FailureInjector;
import dev.example.cp.json.Json;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 算子状态 + 输入偏移的检查点存储（{@code state/}）。
 *
 * <p>提交点是 {@code latest.tmp -> latest.json} 的原子 rename。
 * {@link CrashPoint#STATE_WRITE} 注入在“临时文件已 fsync、但尚未发布”之间，
 * 崩溃后旧的 latest.json 完好，临时文件成为孤儿（恢复时清理）。
 */
public final class StateStore {

    private final Path dir;
    private final Path latest;
    private final Path tmp;
    private final FailureInjector faults;

    public StateStore(Path dataDir, FailureInjector faults) {
        this.dir = dataDir.resolve("state");
        this.latest = dir.resolve("latest.json");
        this.tmp = dir.resolve("latest.tmp");
        this.faults = faults;
    }

    /** 持久化一个检查点（完整快照，小数据参考实现）。 */
    public void persist(CheckpointState cp) {
        try {
            Files.createDirectories(dir);
            Files.writeString(tmp, Json.write(toJson(cp)), StandardCharsets.UTF_8);
            try (java.nio.channels.FileChannel ch = java.nio.channels.FileChannel.open(
                    tmp, java.nio.file.StandardOpenOption.WRITE)) {
                ch.force(true);
            }
            // —— 故障点：状态写入（tmp 已落盘，latest 仍旧）——
            faults.crashIfArmed(CrashPoint.STATE_WRITE, cp.epochId());

            Files.move(tmp, latest,
                    java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        } catch (IOException e) {
            throw new StorageException("persist checkpoint " + cp.epochId() + " failed", e);
        }
    }

    /** 读取最新已发布检查点；不存在返回 null（全新作业）。 */
    public CheckpointState loadLatest() {
        if (!Files.exists(latest)) {
            return null;
        }
        try {
            Map<String, Object> json = Json.parseObject(Files.readString(latest, StandardCharsets.UTF_8));
            long epochId = Json.lng(json, "epochId");
            long lastOffset = Json.lng(json, "lastConsumedOffset");
            long processed = Json.lngOr(json, "processedCount", lastOffset + 1);
            @SuppressWarnings("unchecked")
            Map<String, Object> rawSums = (Map<String, Object>) json.getOrDefault("sums", new LinkedHashMap<>());
            Map<String, Long> sums = new LinkedHashMap<>();
            rawSums.forEach((k, v) -> sums.put(k, ((Number) v).longValue()));
            return new CheckpointState(epochId, lastOffset, new OperatorSnapshot(sums, processed));
        } catch (IOException e) {
            throw new StorageException("load latest checkpoint failed", e);
        }
    }

    /** 清理未发布的临时文件（恢复时调用）。 */
    public void pruneTemps() {
        try {
            Files.deleteIfExists(tmp);
        } catch (IOException e) {
            throw new StorageException("prune state temp failed", e);
        }
    }

    static Map<String, Object> toJson(CheckpointState cp) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("epochId", cp.epochId());
        m.put("lastConsumedOffset", cp.lastConsumedOffset());
        m.put("processedCount", cp.operator().processedCount());
        m.put("sums", cp.operator().sums());
        return m;
    }
}
