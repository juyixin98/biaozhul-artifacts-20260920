package com.example.txflow;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 单流处理引擎：把 输入日志 / 算子状态 / 快照 / 本地事务接收器 组装成
 * “事务性流快照”。
 *
 * <h3>处理一批记录的有序步骤（每个微批 = 一个接收器事务）</h3>
 * <pre>
 *   beginOffset = lastCommittedOffset + 1
 *   state       = 深拷贝(stateCommitted)
 *   对 beginOffset..endOffset 每条记录：state = op.processOne(...)，收集输出
 *
 *   [failpoint AFTER_PROCESS]      —— 处理完成、状态尚未落盘
 *
 *   写 output 到 sink.prepare()（staging fsync → rename 到 outputs/）
 *   构造 preparedTxn（含 stateAfter、输出摘要）
 *   checkpoint.save()             ← 状态落盘（原子快照，状态+输入偏移+输出标记对齐）
 *
 *   [failpoint AFTER_STATE_PERSIST / AFTER_OUTPUT_PREPARE]
 *                                  （prepare 与快照都已持久化，提交标记未写）
 *
 *   sink.commit()                 —— committed.log 追加提交标记（输出变可见）
 *
 *   [failpoint AFTER_COMMIT]       —— 输出已可见、快照旧版仍指向 prepared
 *
 *   提升快照：lastCommittedOffset=endOffset, committedCount++, prepared=null
 *   checkpoint.save()             ← 提交点快照
 * </pre>
 *
 * <h3>启动恢复（按接收器 committed.log 为权威对账）</h3>
 * <ul>
 *   <li>快照 prepared == null：快照版本与接收器必然一致（提交标记在两个 save 之间写入）；
 *       若接收器多出标记（提交后崩溃且新版快照丢失），以接收器为准前快照旧版推进。</li>
 *   <li>快照 prepared != null：
 *       <ul>
 *         <li>标记已存在（提交后崩溃）→ 用 prepared.stateAfter 提升快照；</li>
 *         <li>标记缺失（状态/输出落盘后、提交前崩溃）→ 丢弃 prepared，
 *             回到 stateCommitted / lastCommittedOffset，相关输出下次重放重建。</li>
 *       </ul>
 *   </li>
 * </ul>
 * 无论哪个窗口崩溃，已“可见”的输出集合只增不改且幂等提交；重放从
 * lastCommittedOffset+1 重新计算，因此接收器可见输出严格满足
 * “无遗漏、无重复”。
 */
public class Engine {

    /** 可注入的故障点，对应任务要求的四个阶段边界。 */
    public enum FailPoint {
        /** 1) 处理完成之后、状态落盘之前。 */
        AFTER_PROCESS,
        /** 2) 状态落盘之后（快照含 prepared）。 */
        AFTER_STATE_PERSIST,
        /** 3) 输出准备（sink prepare）之后、提交之前。与 2 实际同一持久化时刻之后。 */
        AFTER_OUTPUT_PREPARE,
        /** 4) 接收器提交之后、提交点快照提升之前。 */
        AFTER_COMMIT,
        /** 不注入故障。 */
        NONE
    }

    private final Path dataDir;
    private final InputLog inputLog;
    private final LocalTxnSink sink;
    private final Checkpoint.Store cpStore;
    private final WordCountOperator operator = new WordCountOperator();

    private Checkpoint checkpoint;

    public Engine(Path dataDir) throws IOException {
        this.dataDir = dataDir;
        Files.createDirectories(dataDir);
        this.inputLog = new InputLog(dataDir);
        this.sink = new LocalTxnSink(dataDir);
        this.cpStore = new Checkpoint.Store(dataDir);
        this.checkpoint = cpStore.load();
    }

    public synchronized Map<String, Object> recover() throws IOException {
        Map<String, Object> report = new LinkedHashMap<>();
        report.put("sink", sink.recover());

        List<String> actions = new ArrayList<>();
        Checkpoint cp = checkpoint;
        List<LocalTxnSink.Marker> markers = sink.readMarkers();
        long markerCount = markers.size();
        long markerEndOffset = markers.isEmpty() ? -1 : markers.get(markers.size() - 1).endOffset;

        if (cp.prepared != null) {
            Checkpoint.PreparedTxn p = cp.prepared;
            boolean committed = sink.isCommitted(p.txnId);
            if (committed) {
                // 窗口：sink.commit() 之后、提升快照之前崩溃
                // 校验提交标记里的 endOffset 与摘要，再提升快照
                LocalTxnSink.Marker m = markers.stream()
                        .filter(x -> x.txnId == p.txnId).findFirst()
                        .orElseThrow(() -> new IOException("内部错误：找不到已声明提交的 txn 标记"));
                if (m.endOffset != p.endOffset || !m.digest.equals(p.outputDigest)) {
                    throw new IOException("恢复对账失败：prepared txn " + p.txnId + " 提交标记与快照不一致");
                }
                promoteAfterCommit(cp, p);
                actions.add("检测到已提交但未提升的 prepared txn " + p.txnId
                        + "（commit 后崩溃）：已将快照提升至 offset " + cp.lastCommittedOffset);
            } else {
                // 窗口：状态/输出已准备并持久化，但提交标记未写 → 整体回滚到上个提交点
                actions.add("检测到未提交的 prepared txn " + p.txnId
                        + "（提交前崩溃）：丢弃 prepared，回滚快照至 offset " + cp.lastCommittedOffset
                        + "，该段输出将重放");
                cp.prepared = null;
                cpStore.save(cp);
            }
        }

        // 对账：快照（无 prepared）与接收器标记必须一致；不一致时以接收器为权威推进
        if (cp.prepared == null) {
            if (cp.committedCount < markerCount) {
                // 理论路径：commit 已完成、两个 save 都丢失（不可能在本机 fsync 协议下发生，
                // 但仍防御性处理）。无 prepared 就没有 stateAfter 可用，只能要求从输入重放——
                // 这里记录差异并拒绝猜测状态。
                throw new IOException("恢复对账失败：接收器有 " + markerCount
                        + " 个已提交事务，但快照只记录 " + cp.committedCount
                        + " 个，且无 prepared 可提升，无法安全恢复状态");
            }
            if (cp.committedCount > markerCount) {
                throw new IOException("恢复对账失败：快照记录 " + cp.committedCount
                        + " 个提交，但接收器只有 " + markerCount + " 个标记");
            }
            if (cp.committedCount > 0 && cp.lastCommittedOffset != markerEndOffset) {
                throw new IOException("恢复对账失败：快照 endOffset=" + cp.lastCommittedOffset
                        + " 与接收器标记 endOffset=" + markerEndOffset + " 不一致");
            }
        }

        report.put("checkpointActions", actions);
        report.put("lastCommittedOffset", checkpoint.lastCommittedOffset);
        report.put("committedCount", checkpoint.committedCount);
        return report;
    }

    /**
     * 处理从 lastCommittedOffset+1 开始的最多 maxRecords 条输入记录，
     * 封装成一个接收器事务。
     *
     * @param failPoint 本次处理在哪个有序边界注入崩溃（JVM Runtime.halt，模拟掉电/kill -9）
     */
    public synchronized Map<String, Object> process(int maxRecords, FailPoint failPoint) throws IOException {
        if (checkpoint.prepared != null) {
            throw new IllegalStateException("存在未决 prepared 事务，请先调用 /recover 完成恢复");
        }

        List<String> all = inputLog.readAll();
        long beginOffset = checkpoint.lastCommittedOffset + 1;
        if (beginOffset >= all.size()) {
            Map<String, Object> idle = new LinkedHashMap<>();
            idle.put("processed", 0);
            idle.put("beginOffset", beginOffset);
            idle.put("endOffset", checkpoint.lastCommittedOffset);
            idle.put("state", checkpoint.stateCommitted);
            idle.put("note", "没有待处理的新记录");
            return idle;
        }

        long endOffset = Math.min(beginOffset + Math.max(1, maxRecords) - 1, all.size() - 1);

        // ---- 阶段 1：处理（内存） ----
        Map<String, Object> state = WordCountOperator.deepCopy(checkpoint.stateCommitted);
        List<Map<String, Object>> outputs = new ArrayList<>();
        for (long off = beginOffset; off <= endOffset; off++) {
            outputs.add(operator.processOne(state, all.get((int) off), off));
        }

        Crash.maybeHalt(failPoint, FailPoint.AFTER_PROCESS, "AFTER_PROCESS");

        // ---- 阶段 2：输出准备（接收器暂存并原子发布，但尚无提交标记） ----
        long txnId = checkpoint.committedCount;
        byte[] outputBytes = encodeOutputs(outputs);
        String digest = sink.prepare(txnId, outputBytes);

        // ---- 阶段 3：状态落盘（原子快照，同时登记 prepared：状态+偏移+输出三者对齐） ----
        Checkpoint.PreparedTxn prepared = new Checkpoint.PreparedTxn();
        prepared.txnId = txnId;
        prepared.beginOffset = beginOffset;
        prepared.endOffset = endOffset;
        prepared.outputFile = LocalTxnSink.outputName(txnId);
        prepared.outputDigest = digest;
        prepared.stateAfter = state;
        checkpoint.prepared = prepared;
        cpStore.save(checkpoint); // 原子替换：要么看不到 prepared，要么看到完整的

        Crash.maybeHalt(failPoint, FailPoint.AFTER_STATE_PERSIST, "AFTER_STATE_PERSIST");
        Crash.maybeHalt(failPoint, FailPoint.AFTER_OUTPUT_PREPARE, "AFTER_OUTPUT_PREPARE");

        // ---- 阶段 4：接收器提交（提交标记 append+fsync；此调用之后输出“可见”） ----
        sink.commit(txnId, endOffset, digest);

        Crash.maybeHalt(failPoint, FailPoint.AFTER_COMMIT, "AFTER_COMMIT");

        // ---- 阶段 5：提升提交点快照 ----
        promoteAfterCommit(checkpoint, prepared);
        cpStore.save(checkpoint);

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("processed", endOffset - beginOffset + 1);
        result.put("beginOffset", beginOffset);
        result.put("endOffset", endOffset);
        result.put("txnId", txnId);
        result.put("state", checkpoint.stateCommitted);
        return result;
    }

    private void promoteAfterCommit(Checkpoint cp, Checkpoint.PreparedTxn p) {
        cp.lastCommittedOffset = p.endOffset;
        cp.committedCount = p.txnId + 1;
        cp.stateCommitted = p.stateAfter;
        cp.prepared = null;
    }

    private static byte[] encodeOutputs(List<Map<String, Object>> outputs) {
        StringBuilder sb = new StringBuilder();
        for (Map<String, Object> o : outputs) {
            sb.append(Json.write(o)).append('\n');
        }
        return sb.toString().getBytes(StandardCharsets.UTF_8);
    }

    // ---------- 查询视图 ----------

    public synchronized Map<String, Object> snapshotState() {
        Map<String, Object> view = new LinkedHashMap<>();
        long inputs;
        try {
            inputs = inputLog.countRecords();
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
        view.put("inputRecords", inputs);
        view.put("lastCommittedOffset", checkpoint.lastCommittedOffset);
        view.put("committedCount", checkpoint.committedCount);
        view.put("hasPrepared", checkpoint.prepared != null);
        view.put("stateCommitted", checkpoint.stateCommitted);
        return view;
    }

    /**
     * 接收器可见输出：仅含已提交事务（committed.log 为权威），按 txn 顺序。
     * 返回每行一个 JSON 字符串。
     */
    public synchronized List<String> visibleOutputs() throws IOException {
        List<String> lines = new ArrayList<>();
        for (byte[] txnBytes : sink.readCommittedOutputs()) {
            String text = new String(txnBytes, StandardCharsets.UTF_8);
            for (String line : text.split("\n", -1)) {
                if (!line.isEmpty()) {
                    lines.add(line);
                }
            }
        }
        return lines;
    }

    public synchronized List<Map<String, Object>> committedMarkers() throws IOException {
        List<Map<String, Object>> out = new ArrayList<>();
        for (LocalTxnSink.Marker m : sink.readMarkers()) {
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("txnId", m.txnId);
            row.put("endOffset", m.endOffset);
            row.put("digest", m.digest);
            out.add(row);
        }
        return out;
    }

    public InputLog inputLog() {
        return inputLog;
    }

    /** 运行时包一层，避免 checked exception 污染视图方法。 */
    static class UncheckedIOException extends RuntimeException {
        UncheckedIOException(IOException cause) {
            super(cause);
        }
    }
}
