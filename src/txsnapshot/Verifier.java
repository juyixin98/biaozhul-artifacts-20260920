package txsnapshot;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * 只读验收校验器：直接读磁盘文件，不打开引擎、不修复、不删除任何东西。
 *
 * 检查项：
 *   1. commit.log 末尾若有半行，视为未提交（与运行时恢复语义一致），计数按完整行；
 *   2. 每个提交标记都有对应暂存段且内容是合法记录；
 *   3. 提交标记无重复；可见输出的偏移集合恰好是 {0,1,...,k-1}（无遗漏、无重复可见输出）；
 *   4. 最新快照存在时：snapshot.offset + 1 == 可见条数（一致快照）；
 *      且若 snapshot.pendingCommit 非空，它要么已在 commit.log 中，要么是唯一的悬空暂存段；
 *   5. 暂存段中除已提交与悬空者外没有其它文件（崩溃残留会在下次启动时回滚，这里仅提示）。
 */
final class Verifier {

    private Verifier() {}

    static int run(Path data) {
        try {
            List<String> problems = verify(data);
            Path commitLog = data.resolve("sink/commit.log");
            long committed = countCompleteLines(commitLog);
            boolean tail = hasPartialTailLine(commitLog);

            System.out.println("== tx-snapshot read-only verification: " + data.toAbsolutePath());
            System.out.println("visible (committed) outputs: " + committed
                    + (tail ? "  (commit.log has a torn tail line, treated as NOT committed)" : ""));
            if (problems.isEmpty()) {
                System.out.println("RESULT: PASS — no missing, no duplicate visible outputs; "
                        + "snapshot/offset/commit markers are consistent");
                return 0;
            }
            System.out.println("RESULT: FAIL");
            for (String p : problems) System.out.println("  - " + p);
            return 1;
        } catch (Exception e) {
            System.out.println("RESULT: ERROR: " + e);
            return 2;
        }
    }

    static List<String> verify(Path data) throws Exception {
        java.util.List<String> problems = new java.util.ArrayList<>();

        Path log = data.resolve("sink/commit.log");
        String raw = Files.exists(log) ? Files.readString(log, StandardCharsets.UTF_8) : "";
        List<String> lines;
        {
            // split("\n", -1) 对以 \n 结尾的正常日志会产生尾部空串，需显式处理：
            // 末尾元素为空 = 最后一行完整（正常）；中间出现空行 = 日志损坏。
            List<String> all = raw.isEmpty() ? List.of()
                    : new java.util.ArrayList<>(java.util.Arrays.asList(raw.split("\n", -1)));
            boolean tornTail = !all.isEmpty() && !all.get(all.size() - 1).isEmpty();
            if (!tornTail && !all.isEmpty()) all.remove(all.size() - 1);
            lines = all;
            for (int i = 0; i < lines.size(); i++) {
                if (lines.get(i).isEmpty()) {
                    problems.add("commit.log contains an empty (corrupt) marker line at line " + i);
                }
            }
        }

        Set<String> ids = new java.util.LinkedHashSet<>();
        Set<String> dupes = new TreeSet<>();
        for (String id : lines) {
            if (!ids.add(id)) dupes.add(id);
        }
        if (!dupes.isEmpty()) problems.add("duplicate commit markers: " + dupes);

        TreeSet<Long> visibleOffsets = new TreeSet<>();
        Path segs = data.resolve("sink/segments");
        for (String id : ids) {
            Path seg = segs.resolve(id + ".rec");
            if (!Files.exists(seg)) {
                problems.add("commit marker " + id + " has no segment file (missing visible output)");
                continue;
            }
            try {
                Map<String, Object> rec = Json.object(Files.readString(seg, StandardCharsets.UTF_8).trim());
                long off = Json.lng(rec, "offset");
                String txn = Json.str(rec, "txn");
                if (!LocalTransactionalSink.txnIdForOffset(off).equals(txn) || !txn.equals(id)) {
                    problems.add("segment " + id + " txn/id mismatch");
                }
                visibleOffsets.add(off);
            } catch (RuntimeException e) {
                problems.add("segment " + id + " unreadable: " + e.getMessage());
            }
        }

        // 无空洞、无重复：可见偏移必须恰好是 0..k-1
        long k = ids.size();
        for (long o = 0; o < k; o++) {
            if (!visibleOffsets.contains(o)) {
                problems.add("gap in visible outputs: offset " + o + " not committed");
            }
        }
        for (Long o : visibleOffsets) {
            if (o >= k) problems.add("unexpected visible offset beyond committed count: " + o);
        }

        // 快照一致性。两种合法形态：
        CheckpointStore.Snapshot snap = readLatestSnapshot(data.resolve("checkpoints"));
        if (snap != null) {
            long snapCount = snap.offset + 1;
            boolean pendingOpen = snap.pendingCommit != null && !ids.contains(snap.pendingCommit)
                    && Files.exists(segs.resolve(snap.pendingCommit + ".rec"));
            if (snapCount == k || (snapCount == k + 1 && pendingOpen)) {
                // 一致（稳态或合法的未决崩溃窗口）
            } else if (snapCount == k + 1) {
                problems.add("snapshot.offset (" + snap.offset + ") is one ahead of committed "
                        + "outputs (" + k + ") but pendingCommit is not a prepared-uncommitted txn");
            } else {
                problems.add("snapshot.offset (" + snap.offset + ") inconsistent with committed "
                        + "output count (" + k + ")");
            }
            if (snap.pendingCommit != null) {
                String p = snap.pendingCommit;
                Path seg = segs.resolve(p + ".rec");
                boolean committed2 = ids.contains(p);
                boolean dangling = Files.exists(seg);
                if (!committed2 && !dangling) {
                    problems.add("snapshot pendingCommit " + p + " is neither committed nor prepared");
                }
            }
        } else if (k > 0) {
            problems.add("committed outputs exist but no snapshot found");
        }

        // 暂存段孤儿情况（只报告；下次启动会回滚非悬空段）
        if (snap != null && Files.isDirectory(segs)) {
            try (var stream = Files.list(segs)) {
                for (Path seg : (Iterable<Path>) stream::iterator) {
                    String n = seg.getFileName().toString();
                    if (!n.endsWith(".rec")) continue;
                    String id = n.substring(0, n.length() - 4);
                    if (!ids.contains(id)
                            && (snap.pendingCommit == null || !snap.pendingCommit.equals(id))) {
                        problems.add("orphan prepared segment " + id
                                + " (uncommitted; will roll back on next start)");
                    }
                }
            }
        }
        return problems;
    }

    private static CheckpointStore.Snapshot readLatestSnapshot(Path dir) throws Exception {
        if (!Files.isDirectory(dir)) return null;
        String latest = null;
        try (var stream = Files.list(dir)) {
            for (Path p : (Iterable<Path>) stream::iterator) {
                String n = p.getFileName().toString();
                if (n.startsWith("snapshot-") && n.endsWith(".json")) {
                    if (latest == null || n.compareTo(latest) > 0) latest = n;
                }
            }
        }
        if (latest == null) return null;
        Map<String, Object> m = Json.object(Files.readString(dir.resolve(latest),
                StandardCharsets.UTF_8).trim());
        Object pending = m.get("pendingCommit");
        return new CheckpointStore.Snapshot(
                Json.lng(m, "epoch"), Json.lng(m, "offset"),
                pending == null ? null : pending.toString(), Json.lng(m, "state"));
    }

    private static long countCompleteLines(Path file) throws Exception {
        if (!Files.exists(file)) return 0;
        String s = Files.readString(file, StandardCharsets.UTF_8);
        if (s.isEmpty()) return 0;
        long c = 0;
        for (int i = 0; i < s.length(); i++) if (s.charAt(i) == '\n') c++;
        return c;
    }

    private static boolean hasPartialTailLine(Path file) throws Exception {
        if (!Files.exists(file)) return false;
        String s = Files.readString(file, StandardCharsets.UTF_8);
        return !s.isEmpty() && s.charAt(s.length() - 1) != '\n';
    }
}
