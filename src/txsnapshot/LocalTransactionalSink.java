package txsnapshot;

import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 本地事务输出接收器。这是“本地”接收器：prepare 出的暂存段文件与 commit 标记
 * 都在同一台机器的本目录里，本类的保证仅覆盖这个接收器自身；
 * 它不是两阶段提交协调器，不对任何外部系统（数据库、消息队列、远程服务等）
 * 提供事务保证。
 *
 * 目录布局：
 *   segments/txn-&lt;offset&gt;.rec   暂存的输出记录（prepare），JSON 单行
 *   commit.log                     只追加的提交标记，每行一个事务 ID
 *
 * 可见性规则：一条输出当且仅当其事务 ID 作为完整一行出现在 commit.log 中时可见。
 * commit.log 只追加 + 每次 fsync + 启动时截掉末尾半行，因此：
 *   - prepare 后、commit 前崩溃：段文件成为孤儿，恢复时回滚删除（不可见）；
 *   - commit 写一半崩溃：半行被截掉，等价于未提交（不可见）；
 *   - commit 完成后崩溃：标记已 fsync，输出持续可见（不会因重放丢或重）。
 * 事务 ID 由输入偏移确定性派生（txn-&lt;offset&gt;），因此重放/恢复时提交天然幂等。
 */
public final class LocalTransactionalSink implements AutoCloseable {

    private final Path segmentsDir;
    private final Path commitLog;
    private FileChannel commitChannel;
    private final Set<String> committed = new HashSet<>();
    private final List<String> committedOrder = new ArrayList<>();

    private LocalTransactionalSink(Path base, List<String> committedIds) throws IOException {
        this.segmentsDir = DurableFiles.ensureDir(base.resolve("segments"));
        this.commitLog = base.resolve("commit.log");
        this.committed.addAll(committedIds);
        this.committedOrder.addAll(committedIds);
        this.commitChannel = DurableFiles.openAppend(commitLog);
    }

    /**
     * 打开并恢复接收器。
     *
     * @param snapshotPending 最新快照里记录的悬空事务（已 prepare、未确认 commit）；
     *                        非 null 时恢复程序会把它提交（resume）。null 表示无悬空事务。
     */
    public static LocalTransactionalSink open(Path base, String snapshotPending) throws IOException {
        DurableFiles.ensureDir(base.resolve("segments"));
        Path log = base.resolve("commit.log");
        DurableFiles.truncateToLastNewline(log);
        List<String> ids = readCommitIds(log);

        LocalTransactionalSink sink = new LocalTransactionalSink(base, ids);

        // 1) 快照引用的悬空事务：段文件必须在，提交标记补写（幂等）
        if (snapshotPending != null && !sink.committed.contains(snapshotPending)) {
            Path seg = sink.segmentsDir.resolve(snapshotPending + ".rec");
            if (!Files.exists(seg)) {
                throw new IOException("snapshot references pending txn " + snapshotPending
                        + " but its segment is missing; refusing to guess (possible data loss)");
            }
            sink.appendCommitMarker(snapshotPending);
        }
        // 2) 其余 prepare 段（既未提交、也不是快照悬空事务）一律回滚
        sink.rollbackOrphans(snapshotPending);
        return sink;
    }

    /**
     * 阶段一：把输出记录持久化到暂存段（prepare）。
     * 故意直接写目标文件（不经原子 rename），这样 DURING_PREPARE 能真实产生
     * “写了一半的暂存段”；这类残段在恢复时按回滚处理，永远不会变可见。
     */
    public void prepare(String txnId, long offset, long value, Fault fault) throws IOException {
        Path seg = segmentsDir.resolve(txnId + ".rec");
        byte[] payload = (Json.dump(record(txnId, offset, value)) + "\n").getBytes(StandardCharsets.UTF_8);

        if (fault != null) {
            // 一半正常写、一半在故障点；HALT 时留下半文件，EXCEPTION 时也可能已写一部分
            int half = payload.length / 2;
            try (FileChannel ch = FileChannel.open(seg,
                    java.nio.file.StandardOpenOption.CREATE,
                    java.nio.file.StandardOpenOption.WRITE,
                    java.nio.file.StandardOpenOption.TRUNCATE_EXISTING)) {
                java.nio.ByteBuffer bb = java.nio.ByteBuffer.wrap(payload, 0, half);
                while (bb.hasRemaining()) ch.write(bb);
                ch.force(false);
            }
            fault.fire(Fault.Point.DURING_PREPARE, offset);
            try (FileChannel ch = FileChannel.open(seg,
                    java.nio.file.StandardOpenOption.WRITE,
                    java.nio.file.StandardOpenOption.APPEND)) {
                java.nio.ByteBuffer bb = java.nio.ByteBuffer.wrap(payload, half, payload.length - half);
                while (bb.hasRemaining()) ch.write(bb);
                ch.force(false);
            }
        } else {
            Files.write(seg, payload,
                    java.nio.file.StandardOpenOption.CREATE,
                    java.nio.file.StandardOpenOption.WRITE,
                    java.nio.file.StandardOpenOption.TRUNCATE_EXISTING);
        }
        DurableFiles.fsyncDir(segmentsDir);
    }

    /** 阶段二：写入提交标记（commit）。重复提交同一事务是无操作。 */
    public void commit(String txnId, long offset, Fault fault) throws IOException {
        synchronized (this) {
            if (committed.contains(txnId)) return;
            byte[] line = (txnId + "\n").getBytes(StandardCharsets.UTF_8);
            if (fault != null) {
                commitChannel.position(commitChannel.size());
                int half = Math.max(1, line.length / 2);
                // 先写半行并 fsync，再注入故障，模拟标记写到一半
                java.nio.ByteBuffer bb = java.nio.ByteBuffer.wrap(line, 0, half);
                while (bb.hasRemaining()) commitChannel.write(bb);
                commitChannel.force(false);
                fault.fire(Fault.Point.DURING_COMMIT, offset);
                bb = java.nio.ByteBuffer.wrap(line, half, line.length - half);
                while (bb.hasRemaining()) commitChannel.write(bb);
                commitChannel.force(false);
            } else {
                DurableFiles.appendForce(commitChannel, line);
            }
            committed.add(txnId);
            committedOrder.add(txnId);
            DurableFiles.fsyncDir(segmentsDir);
        }
    }

    /** 当前所有已可见（已提交）输出，按提交顺序。 */
    public synchronized List<Map<String, Object>> readVisible() throws IOException {
        List<Map<String, Object>> out = new ArrayList<>();
        for (String id : committedOrder) {
            Path seg = segmentsDir.resolve(id + ".rec");
            if (!Files.exists(seg)) {
                throw new IOException("committed txn " + id + " missing segment file");
            }
            out.add(Json.object(Files.readString(seg, StandardCharsets.UTF_8).trim()));
        }
        return out;
    }

    public synchronized int visibleCount() {
        return committedOrder.size();
    }

    public static String txnIdForOffset(long offset) {
        return "txn-" + offset;
    }

    private static Map<String, Object> record(String txnId, long offset, long value) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("txn", txnId);
        m.put("offset", offset);
        m.put("value", value);
        m.put("result", value * value); // 算子语义：输出 = 输入的平方
        return m;
    }

    private void appendCommitMarker(String txnId) throws IOException {
        byte[] line = (txnId + "\n").getBytes(StandardCharsets.UTF_8);
        DurableFiles.appendForce(commitChannel, line);
        committed.add(txnId);
        committedOrder.add(txnId);
    }

    private void rollbackOrphans(String snapshotPending) throws IOException {
        try (var stream = Files.list(segmentsDir)) {
            for (Path seg : stream.toList()) {
                String n = seg.getFileName().toString();
                if (!n.startsWith("txn-") || !n.endsWith(".rec")) continue;
                String id = n.substring(0, n.length() - ".rec".length());
                if (!committed.contains(id) && !id.equals(snapshotPending)) {
                    Files.deleteIfExists(seg);
                }
            }
        }
        DurableFiles.fsyncDir(segmentsDir);
    }

    private static List<String> readCommitIds(Path log) throws IOException {
        List<String> ids = new ArrayList<>();
        if (!Files.exists(log)) return ids;
        for (String line : Files.readString(log, StandardCharsets.UTF_8).split("\n", -1)) {
            if (!line.isEmpty()) ids.add(line);
        }
        return ids;
    }

    @Override
    public synchronized void close() throws IOException {
        if (commitChannel != null) {
            commitChannel.close();
            commitChannel = null;
        }
    }
}
