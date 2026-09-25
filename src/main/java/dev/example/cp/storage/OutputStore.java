package dev.example.cp.storage;

import dev.example.cp.engine.OutputRecord;
import dev.example.cp.fail.CrashPoint;
import dev.example.cp.fail.FailureInjector;
import dev.example.cp.json.Json;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 本地输出日志存储（{@code output/committing/}）。
 *
 * <p>每个 epoch 一个文件：
 * <ul>
 *   <li>{@code chk-&lt;id&gt;.pending} —— 已写出但事务未提交；崩溃后可能存在</li>
 *   <li>{@code chk-&lt;id&gt;.committed} —— 已提交（rename 是单原子操作）</li>
 * </ul>
 *
 * 提交（pending→committed 的 rename）即“本地输出事务”的提交点。
 * rename 在同一目录内是原子的，世界只能看到 pending 或 committed 二者之一。
 */
public final class OutputStore {

    private final Path dir;
    private final FailureInjector faults;

    public OutputStore(Path dataDir, FailureInjector faults) {
        this.dir = dataDir.resolve("output").resolve("committing");
        this.faults = faults;
    }

    /** 写出 pending 输出（临时文件 fsync 后原子改名为 .pending）。 */
    public void writePending(OutputRecord rec) {
        Path tmp = dir.resolve("chk-" + rec.epochId() + ".write-tmp");
        Path pending = pendingPath(rec.epochId());
        try {
            Files.createDirectories(dir);
            Files.writeString(tmp, Json.write(toJson(rec)), StandardCharsets.UTF_8);
            try (java.nio.channels.FileChannel ch = java.nio.channels.FileChannel.open(
                    tmp, java.nio.file.StandardOpenOption.WRITE)) {
                ch.force(true);
            }
            // —— 故障点：pending 输出写入（tmp 落盘后、发布为 pending 前）——
            faults.crashIfArmed(CrashPoint.PENDING_WRITE, rec.epochId());

            Files.move(tmp, pending,
                    java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        } catch (IOException e) {
            throw new StorageException("write pending output " + rec.epochId() + " failed", e);
        }
    }

    /**
     * 原子提交：pending → committed。
     *
     * <p>{@link CrashPoint#COMMIT_RENAME} 注入点在 rename 执行前——
     * 崩溃后文件仍叫 .pending，汇总表不可能看到该 epoch。
     */
    public void commit(long epochId) {
        // —— 故障点：输出提交（rename 之前）——
        faults.crashIfArmed(CrashPoint.COMMIT_RENAME, epochId);

        Path pending = pendingPath(epochId);
        Path committed = committedPath(epochId);
        try {
            if (!Files.exists(pending)) {
                throw new StorageException("cannot commit epoch " + epochId + ": pending file missing");
            }
            Files.move(pending, committed,
                    java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        } catch (IOException e) {
            throw new StorageException("commit output " + epochId + " failed", e);
        }
    }

    /** 删除某个 epoch 的 pending（恢复判定其状态缺失时调用）。 */
    public void discardPending(long epochId) {
        try {
            Files.deleteIfExists(pendingPath(epochId));
        } catch (IOException e) {
            throw new StorageException("discard pending " + epochId + " failed", e);
        }
    }

    /** 恢复时清理未发布的写入临时文件。 */
    public void pruneTemps() {
        if (!Files.exists(dir)) {
            return;
        }
        try (var paths = Files.newDirectoryStream(dir, "*.write-tmp")) {
            for (Path p : paths) {
                Files.deleteIfExists(p);
            }
        } catch (IOException e) {
            throw new StorageException("prune output temps failed", e);
        }
    }

    /** 列出所有已提交 epoch 的输出记录，按 epoch 升序。 */
    public List<OutputRecord> readAllCommitted() {
        List<Long> ids = new ArrayList<>();
        if (Files.exists(dir)) {
            try (var paths = Files.newDirectoryStream(dir, "chk-*.committed")) {
                for (Path p : paths) {
                    ids.add(parseId(p.getFileName().toString(), ".committed"));
                }
            } catch (IOException e) {
                throw new StorageException("list committed outputs failed", e);
            }
        }
        ids.sort(Long::compareTo);
        List<OutputRecord> records = new ArrayList<>();
        for (Long id : ids) {
            records.add(readOne(committedPath(id)));
        }
        return records;
    }

    /** 读单个 pending（若存在）。 */
    public OutputRecord readPending(long epochId) {
        Path p = pendingPath(epochId);
        return Files.exists(p) ? readOne(p) : null;
    }

    private OutputRecord readOne(Path p) {
        try {
            Map<String, Object> json = Json.parseObject(Files.readString(p, StandardCharsets.UTF_8));
            long epochId = Json.lng(json, "epochId");
            long lastOffset = Json.lng(json, "lastConsumedOffset");
            @SuppressWarnings("unchecked")
            Map<String, Object> rawSums = (Map<String, Object>) json.getOrDefault("sumsAfter", new LinkedHashMap<>());
            Map<String, Long> sums = new LinkedHashMap<>();
            rawSums.forEach((k, v) -> sums.put(k, ((Number) v).longValue()));
            List<OutputRecord.Delta> deltas = new ArrayList<>();
            Object rawDeltas = json.get("deltas");
            if (rawDeltas instanceof List<?> list) {
                for (Object item : list) {
                    @SuppressWarnings("unchecked")
                    Map<String, Object> d = (Map<String, Object>) item;
                    deltas.add(new OutputRecord.Delta(
                            Json.lng(d, "offset"), Json.str(d, "key"), Json.lng(d, "sumAfter")));
                }
            }
            return new OutputRecord(epochId, lastOffset, sums, deltas);
        } catch (IOException e) {
            throw new StorageException("read output record failed: " + p, e);
        }
    }

    private long parseId(String name, String suffix) {
        return Long.parseLong(name.substring("chk-".length(), name.length() - suffix.length()));
    }

    private Path pendingPath(long epochId) {
        return dir.resolve("chk-" + epochId + ".pending");
    }

    private Path committedPath(long epochId) {
        return dir.resolve("chk-" + epochId + ".committed");
    }

    static Map<String, Object> toJson(OutputRecord rec) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("epochId", rec.epochId());
        m.put("lastConsumedOffset", rec.lastConsumedOffset());
        m.put("sumsAfter", rec.sumsAfter());
        List<Map<String, Object>> deltas = new ArrayList<>();
        for (OutputRecord.Delta d : rec.deltas()) {
            Map<String, Object> dm = new LinkedHashMap<>();
            dm.put("offset", d.offset());
            dm.put("key", d.key());
            dm.put("sumAfter", d.sumAfter());
            deltas.add(dm);
        }
        m.put("deltas", deltas);
        return m;
    }
}
