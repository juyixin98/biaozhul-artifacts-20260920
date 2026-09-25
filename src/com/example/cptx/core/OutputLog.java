package com.example.cptx.core;

import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 本地输出事务日志（两阶段提交风格，对本地文件系统而言由 rename 的原子性保证）：
 *
 * <pre>
 *   output/
 *     txn-NNNNNN.staged   —— 阶段1：事务内容 fsync 落盘，但对外不可见（未提交）
 *     txn-NNNNNN.json     —— 阶段2：原子 rename 后即为已提交，内容不可变
 * </pre>
 *
 * 事务号 = 检查点号，因此“重放”是确定性的：恢复后再次处理同一批输入，产生的事务号、
 * 事务内容逐字节相同。提交是幂等的——已存在同名正式文件时，内容必须一致，直接覆盖。
 *
 * 故障语义：
 *  - OUTPUT_STAGE  崩溃：只留下 .staged，恢复时丢弃，重放重建 → 无输出、无重复。
 *  - OUTPUT_COMMIT 崩溃：rename 已完成则提交生效（汇总表稍后由日志重建）；
 *                       rename 未完成则等价于未提交。两种情况下恢复都得到恰好一次。
 */
public final class OutputLog {

    static final String PREFIX = "txn-";
    private static final String STAGED = ".staged";
    private static final String COMMITTED = ".json";

    private final Path dir;

    public OutputLog(Path baseDir) throws IOException {
        Files.createDirectories(baseDir);
        this.dir = baseDir;
    }

    /** 阶段1：写暂存事务文件（fsync）。injectAfterStage=true 时写完后立即注入 OUTPUT_STAGE 故障。 */
    public void stage(long txnId, long nextOffset, List<Map<String, Object>> rows,
                      Clock clock, boolean injectAfterStage) throws IOException {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("txnId", txnId);
        payload.put("nextOffset", nextOffset);
        payload.put("committedAtMillis", clock.nowMillis());
        payload.put("rows", rows);

        Path staged = stagedPath(txnId);
        Files.deleteIfExists(staged); // 清理上次崩溃的同编号暂存（重放重建）
        byte[] data = Json.pretty(payload).getBytes(StandardCharsets.UTF_8);
        try (FileOutputStream out = new FileOutputStream(staged.toFile())) {
            out.write(data);
            out.flush();
            out.getChannel().force(true);
        }
        if (injectAfterStage) {
            throw new InjectedFaultException(FaultPhase.OUTPUT_STAGE, txnId);
        }
    }

    /**
     * 阶段2：提交暂存事务（原子 rename）。injectAfterCommit=true 时 rename 之后、
     * 汇总表应用之前注入 OUTPUT_COMMIT 故障。
     */
    public void commit(long txnId, boolean injectAfterCommit) throws IOException {
        Path staged = stagedPath(txnId);
        if (!Files.exists(staged)) {
            throw new IOException("无法提交事务 " + txnId + "：暂存文件不存在");
        }
        Path committed = committedPath(txnId);
        try {
            Files.move(staged, committed,
                    java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        } catch (java.nio.file.AtomicMoveNotSupportedException e) {
            Files.move(staged, committed, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        }
        FileIO.fsyncDirectory(committed);
        if (injectAfterCommit) {
            throw new InjectedFaultException(FaultPhase.OUTPUT_COMMIT, txnId);
        }
    }

    /** 读取所有已提交事务（按事务号升序）。 */
    public List<Txn> readCommitted() throws IOException {
        return FileIO.list(dir, PREFIX).stream()
                .filter(p -> p.getFileName().toString().endsWith(COMMITTED))
                .sorted((a, b) -> Long.compare(idOf(a), idOf(b)))
                .map(p -> {
                    try {
                        return Txn.fromJson(Json.obj(Json.parse(Files.readString(p, StandardCharsets.UTF_8))));
                    } catch (IOException e) {
                        throw new RuntimeException(e);
                    }
                })
                .toList();
    }

    public List<Long> committedIds() throws IOException {
        return FileIO.list(dir, PREFIX).stream()
                .filter(p -> p.getFileName().toString().endsWith(COMMITTED))
                .map(OutputLog::idOf)
                .sorted()
                .toList();
    }

    /** 启动时清理所有未提交的暂存文件——它们可能包含崩溃时未提交的输出，必须丢弃。 */
    public void abortStaged() throws IOException {
        for (Path p : FileIO.list(dir, PREFIX)) {
            if (p.getFileName().toString().endsWith(STAGED)) {
                Files.deleteIfExists(p);
            }
        }
    }

    /** 判断事务是否已提交（用于幂等重提交）。 */
    public boolean isCommitted(long txnId) {
        return Files.exists(committedPath(txnId));
    }

    /** 读取已提交事务字节（逐字节比较用）。 */
    public byte[] readCommittedBytes(long txnId) throws IOException {
        return Files.readAllBytes(committedPath(txnId));
    }

    private Path stagedPath(long txnId) {
        return dir.resolve(PREFIX + String.format("%06d", txnId) + STAGED);
    }

    private Path committedPath(long txnId) {
        return dir.resolve(PREFIX + String.format("%06d", txnId) + COMMITTED);
    }

    static long idOf(Path p) {
        String n = p.getFileName().toString();
        String body = n.substring(PREFIX.length());
        return Long.parseLong(body.substring(0, 6));
    }

    /** 已提交事务记录。 */
    public static final class Txn {
        public final long txnId;
        public final long nextOffset;
        public final long committedAtMillis;
        public final List<Map<String, Object>> rows;

        Txn(long txnId, long nextOffset, long committedAtMillis, List<Map<String, Object>> rows) {
            this.txnId = txnId;
            this.nextOffset = nextOffset;
            this.committedAtMillis = committedAtMillis;
            this.rows = rows;
        }

        @SuppressWarnings("unchecked")
        static Txn fromJson(Map<String, Object> m) {
            return new Txn(
                    Json.lng(m.get("txnId")),
                    Json.lng(m.get("nextOffset")),
                    Json.lng(m.get("committedAtMillis")),
                    (List<Map<String, Object>>) (List<?>) Json.arr(m.get("rows")));
        }
    }
}
