package com.example.cptx.core;

import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 检查点存储。每个检查点一个不可变文件 checkpoint-NNNNNN.json：
 *
 * <pre>
 * {
 *   "id": 3,
 *   "nextOffset": 20,          // 已处理输入条数；恢复后从该偏移继续
 *   "state": { "A": {"count":7,"sum":17.5}, ... },
 *   "createdAtMillis": ...,
 *   "records": 5               // 本次输出事务包含的变更行数（信息字段）
 * }
 * </pre>
 *
 * 写入协议：checkpoint-N.tmp → fsync → 原子 rename → fsync 目录。
 * 崩溃若发生在 STATE_WRITE（tmp 已写、rename 前），恢复时该检查点视为不存在，从头重放，不丢数据。
 * 检查点文件一经 rename 即不可变。
 */
public final class CheckpointStore {

    static final String PREFIX = "checkpoint-";
    private static final String SUFFIX = ".json";

    private final Path dir;

    public CheckpointStore(Path baseDir) throws IOException {
        Files.createDirectories(baseDir);
        this.dir = baseDir;
        cleanupStaging();
    }

    /** 启动恢复：读取编号最大的检查点；没有则返回 null（全新启动）。 */
    public Checkpoint loadLatest() throws IOException {
        List<Path> files = FileIO.list(dir, PREFIX);
        Path latest = null;
        long latestId = -1;
        for (Path p : files) {
            long id = idOf(p);
            if (id > latestId) {
                latestId = id;
                latest = p;
            }
        }
        if (latest == null) return null;
        return Checkpoint.fromJson(Json.obj(Json.parse(Files.readString(latest, StandardCharsets.UTF_8))));
    }

    /**
     * 原子写入一个检查点。
     *
     * @param injectBeforeRename true 时（STATE_WRITE 注入）：临时文件已 fsync，rename 之前抛出故障。
     */
    public Checkpoint write(long id, long nextOffset, Map<String, Map<String, Object>> state,
                            long records, Clock clock, boolean injectBeforeRename) throws IOException {
        Checkpoint cp = new Checkpoint(id, nextOffset, state, clock.nowMillis(), records);
        byte[] data = (Json.pretty(cp.toJson())).getBytes(StandardCharsets.UTF_8);
        Path target = dir.resolve(PREFIX + String.format("%06d", id) + SUFFIX);
        Path tmp = dir.resolve(PREFIX + String.format("%06d", id) + SUFFIX + ".tmp");
        // 上次崩溃可能在 rename 前留下同编号 tmp；重放重建前先删除（正式 .json 不受影响）。
        Files.deleteIfExists(tmp);
        try (FileOutputStream out = new FileOutputStream(tmp.toFile())) {
            out.write(data);
            out.flush();
            out.getChannel().force(true);
        }
        if (injectBeforeRename) {
            // 故意保留 .tmp 文件以模拟“写到一半进程死亡”；启动恢复时 cleanupStaging 会清除。
            throw new InjectedFaultException(FaultPhase.STATE_WRITE, id);
        }
        // 直接原子 rename 我们自己 fsync 过的 tmp（不再另写第二个临时文件）。
        try {
            Files.move(tmp, target,
                    java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        } catch (java.nio.file.AtomicMoveNotSupportedException e) {
            Files.move(tmp, target, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        }
        FileIO.fsyncDirectory(target);
        return cp;
    }

    /** 列出已提交检查点编号（升序）。 */
    public List<Long> committedIds() throws IOException {
        return FileIO.list(dir, PREFIX).stream()
                .filter(p -> !p.getFileName().toString().endsWith(".tmp"))
                .map(CheckpointStore::idOf)
                .sorted()
                .toList();
    }

    /** 清理上次崩溃留下的临时检查点文件（绝不清理已 rename 的正式文件）。 */
    public void cleanupStaging() throws IOException {
        for (Path p : FileIO.list(dir, PREFIX)) {
            if (p.getFileName().toString().endsWith(".tmp")) {
                Files.deleteIfExists(p);
            }
        }
    }

    static long idOf(Path p) {
        String n = p.getFileName().toString();
        String body = n.substring(PREFIX.length());
        int dot = body.indexOf('.');
        return Long.parseLong(body.substring(0, dot));
    }

    /** 检查点记录。 */
    public static final class Checkpoint {
        public final long id;
        public final long nextOffset;
        public final Map<String, Map<String, Object>> state;
        public final long createdAtMillis;
        public final long records;

        Checkpoint(long id, long nextOffset, Map<String, Map<String, Object>> state,
                   long createdAtMillis, long records) {
            this.id = id;
            this.nextOffset = nextOffset;
            this.state = state;
            this.createdAtMillis = createdAtMillis;
            this.records = records;
        }

        Map<String, Object> toJson() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", id);
            m.put("nextOffset", nextOffset);
            m.put("state", new TreeMap<>(state));
            m.put("createdAtMillis", createdAtMillis);
            m.put("records", records);
            return m;
        }

        @SuppressWarnings("unchecked")
        static Checkpoint fromJson(Map<String, Object> m) {
            Map<String, Object> rawState = Json.obj(m.get("state"));
            Map<String, Map<String, Object>> state = new LinkedHashMap<>();
            for (Map.Entry<String, Object> e : rawState.entrySet()) {
                state.put(e.getKey(), (Map<String, Object>) e.getValue());
            }
            return new Checkpoint(
                    Json.lng(m.get("id")),
                    Json.lng(m.get("nextOffset")),
                    state,
                    Json.lng(m.get("createdAtMillis")),
                    Json.lng(m.get("records")));
        }
    }
}
