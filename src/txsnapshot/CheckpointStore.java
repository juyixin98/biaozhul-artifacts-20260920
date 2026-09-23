package txsnapshot;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

/**
 * 检查点存储：状态 + 输入偏移 + 输出提交标记 构成的一致快照。
 *
 * 快照文件 snapshot-NNNN.tmp（JSON）经原子 rename 发布为 snapshot-NNNN.json，
 * "current" 符号链接（或回退为纯文本指针文件）指向最新快照，发布顺序：
 *   1) 数据文件 fsync + rename
 *   2) 指针原子切换 + fsync 目录
 * 因此指针永远指向一份完整快照。
 *
 * 快照内容：
 *   epoch           单调递增的快照号
 *   offset          已完成处理的输入偏移（-1 表示空）
 *   pendingCommit   已 prepare、但 commit 标记尚未确认的事务 ID（至多 1 个）
 *   state           算子状态
 *   checksum        (offset+":"+pending+":"+stateValue) 的简单校验
 */
public final class CheckpointStore {

    public static final class Snapshot {
        public final long epoch;
        public final long offset;
        public final String pendingCommit; // null 表示无悬空事务
        public final long state;

        Snapshot(long epoch, long offset, String pendingCommit, long state) {
            this.epoch = epoch;
            this.offset = offset;
            this.pendingCommit = pendingCommit;
            this.state = state;
        }

        Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("epoch", epoch);
            m.put("offset", offset);
            m.put("pendingCommit", pendingCommit);
            m.put("state", state);
            m.put("checksum", checksum(offset, pendingCommit, state));
            return m;
        }
    }

    private final Path dir;

    public CheckpointStore(Path dir) throws IOException {
        DurableFiles.ensureDir(dir);
        this.dir = dir;
        cleanupTemps();
    }

    /** 发布一份新快照，然后清理旧快照（保留最新两份）。 */
    public synchronized Snapshot save(long epoch, long offset, String pendingCommit, long state)
            throws IOException {
        Snapshot snap = new Snapshot(epoch, offset, pendingCommit, state);
        Path target = dir.resolve("snapshot-" + epoch + ".json");
        DurableFiles.writeAtomically(target, (Json.dump(snap.toMap()) + "\n")
                .getBytes(StandardCharsets.UTF_8));
        publishPointer(target.getFileName().toString());
        prune(epoch);
        return snap;
    }

    /** 读取最新快照；无快照返回 null。 */
    public synchronized Snapshot loadLatest() throws IOException {
        String name = readPointer();
        if (name == null) {
            name = scanLatest();
            if (name == null) return null;
        }
        Path f = dir.resolve(name);
        if (!Files.exists(f)) {
            name = scanLatest();
            if (name == null) return null;
            f = dir.resolve(name);
        }
        Map<String, Object> m = Json.object(Files.readString(f, StandardCharsets.UTF_8));
        long epoch = Json.lng(m, "epoch");
        long offset = Json.lng(m, "offset");
        Object pending = m.get("pendingCommit");
        String pendingCommit = pending == null ? null : pending.toString();
        long state = Json.lng(m, "state");
        String want = checksum(offset, pendingCommit, state);
        if (!want.equals(m.get("checksum"))) {
            throw new IOException("snapshot checksum mismatch in " + f);
        }
        return new Snapshot(epoch, offset, pendingCommit, state);
    }

    private void publishPointer(String fileName) throws IOException {
        // 优先符号链接；不支持时回退到 current.txt 指针文件
        Path link = dir.resolve("current");
        try {
            Path tmpLink = dir.resolve("current.tmp-" + ProcessHandle.current().pid() + "-"
                    + System.nanoTime());
            try {
                Files.createSymbolicLink(tmpLink, Path.of(fileName));
            } catch (java.nio.file.FileAlreadyExistsException e) {
                Files.delete(tmpLink);
                Files.createSymbolicLink(tmpLink, Path.of(fileName));
            }
            Files.move(tmpLink, link, java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        } catch (UnsupportedOperationException | IOException e) {
            DurableFiles.writeAtomically(dir.resolve("current.txt"),
                    (fileName + "\n").getBytes(StandardCharsets.UTF_8));
        }
        DurableFiles.fsyncDir(dir);
    }

    private String readPointer() throws IOException {
        Path link = dir.resolve("current");
        if (Files.isSymbolicLink(link)) {
            return Files.readSymbolicLink(link).getFileName().toString();
        }
        Path txt = dir.resolve("current.txt");
        if (Files.exists(txt)) {
            return Files.readString(txt, StandardCharsets.UTF_8).trim();
        }
        return null;
    }

    private String scanLatest() throws IOException {
        try (Stream<Path> s = Files.list(dir)) {
            return s.map(p -> p.getFileName().toString())
                    .filter(n -> n.startsWith("snapshot-") && n.endsWith(".json"))
                    .max(Comparator.naturalOrder())
                    .orElse(null);
        }
    }

    private void prune(long keepEpoch) throws IOException {
        List<String> names;
        try (Stream<Path> s = Files.list(dir)) {
            names = s.map(p -> p.getFileName().toString())
                    .filter(n -> n.startsWith("snapshot-") && n.endsWith(".json"))
                    .sorted(Comparator.reverseOrder())
                    .toList();
        }
        for (String n : names) {
            long epoch = Long.parseLong(n.substring("snapshot-".length(), n.length() - ".json".length()));
            if (epoch < keepEpoch - 1) {
                Files.deleteIfExists(dir.resolve(n));
            }
        }
    }

    private void cleanupTemps() throws IOException {
        try (Stream<Path> s = Files.list(dir)) {
            for (Path p : s.toList()) {
                String n = p.getFileName().toString();
                if (n.endsWith(".tmp") || n.startsWith(".tmp-")
                        || (n.startsWith("snapshot-") && n.contains(".tmp-"))
                        || n.startsWith("current.tmp-")) {
                    Files.deleteIfExists(p);
                }
            }
        }
        DurableFiles.fsyncDir(dir);
    }

    static String checksum(long offset, String pendingCommit, long state) {
        return Long.toHexString(offset ^ (pendingCommit == null ? 0L : pendingCommit.hashCode()) * 31L)
                + ":" + Long.toHexString(state);
    }
}
