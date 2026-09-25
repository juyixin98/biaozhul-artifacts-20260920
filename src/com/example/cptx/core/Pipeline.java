package com.example.cptx.core;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 流计算管线（单分区参考实现）。协调检查点屏障协议：
 *
 * <pre>
 *   处理事件 → 策略判定插入屏障 →
 *     ① 算子状态+偏移快照原子写检查点文件        (可注入 STATE_WRITE)
 *     ② 输出事务写暂存文件并 fsync              (可注入 OUTPUT_STAGE)
 *     ③ 暂存事务原子 rename 提交                (可注入 OUTPUT_COMMIT)
 *     ④ 由已提交事务日志重建并原子重写本地汇总表  (可注入 TABLE_APPLY)
 * </pre>
 *
 * 不变量：
 *  - 检查点文件的出现严格早于对应输出事务的提交；
 *  - 事务号 == 检查点号，恢复后重放产生同号、同内容的事务；
 *  - 已提交事务日志是汇总表的唯一权威来源，汇总表始终可幂等重建；
 *  - 汇总表相对检查点可能短暂滞后，但不会出现未提交的行。
 */
public final class Pipeline {

    private final Path baseDir;
    private final InputLog inputLog;
    private final CheckpointStore checkpointStore;
    private final OutputLog outputLog;
    private final SummaryTable summaryTable;
    private final KeyedAggregate aggregate;
    private final CheckpointPolicy policy;
    private final Clock clock;
    private final FaultSpec faults;

    private long nextOffset;       // 下一条待处理事件的偏移（== 已处理条数）
    private long lastCheckpointId;
    private long sinceCheckpoint;  // 自上次检查点以来处理的事件数

    public Pipeline(Path baseDir, CheckpointPolicy policy, Clock clock, FaultSpec faults) throws IOException {
        this.baseDir = baseDir;
        this.policy = policy;
        this.clock = clock;
        this.faults = faults != null ? faults : new FaultSpec();
        this.inputLog = new InputLog(baseDir);
        this.checkpointStore = new CheckpointStore(baseDir);
        this.outputLog = new OutputLog(baseDir.resolve("output"));
        this.summaryTable = new SummaryTable(baseDir);
        this.aggregate = new KeyedAggregate();

        // 恢复第 1 步：丢弃所有未提交暂存输出。
        outputLog.abortStaged();

        // 恢复第 2 步：确定“完整检查点”水位。
        // 协议中状态快照(①)先于输出提交(③)。若崩溃发生在两者之间，磁盘上会存在
        // checkpoint-N 但没有已提交的 txn-N——该检查点是未走完协议的孤儿，不能作为恢复点；
        // 回滚到“检查点文件与已提交事务文件同时存在”的最大编号 K，重放时重新生成 K+1..。
        CheckpointStore.Checkpoint latest = latestCompleteCheckpoint();
        if (latest != null) {
            this.nextOffset = latest.nextOffset;
            this.lastCheckpointId = latest.id;
            aggregate.restore(latest.state);
        }

        // 恢复第 3 步：汇总表始终以已提交事务日志为准重建（这是“无重复提交”的落点）。
        Map<String, Map<String, Object>> view = summaryTable.rebuildFrom(outputLog.readCommitted());
        summaryTable.rewriteAtomically(view, false, 0L);
    }

    /**
     * 找到最大的 K，使 checkpoint-K.json 与 output/txn-K.json 同时存在（且编号一致）。
     * 任何编号更大的检查点文件都是“输出未提交”的孤儿，删除后由重放确定性重建。
     */
    private CheckpointStore.Checkpoint latestCompleteCheckpoint() throws IOException {
        java.util.List<Long> cpIds = checkpointStore.committedIds();
        java.util.Set<Long> txnIds = new java.util.TreeSet<>(outputLog.committedIds());
        long k = -1;
        for (Long id : cpIds) {
            if (txnIds.contains(id)) k = Math.max(k, id);
        }
        // 删除编号大于 K 的孤儿检查点文件（它们对应的输出从未提交）。
        for (Long id : cpIds) {
            if (id > k) {
                Files.deleteIfExists(baseDir.resolve(
                        "checkpoint-" + String.format("%06d", id) + ".json"));
            }
        }
        if (k < 0) return null;
        return checkpointStore.loadLatest(); // 删除孤儿后最大的即 K
    }

    public Path baseDir() {
        return baseDir;
    }

    /**
     * 接收一批事件：先持久化到输入日志（fsync），再驱动处理循环。
     * 已处理范围内的重放事件会被跳过。
     */
    public synchronized List<Event> appendAndProcess(List<Map<String, Object>> payloads) throws IOException {
        List<Event> events = inputLog.appendAll(payloads);
        pump(events);
        return events;
    }

    /** 处理输入日志中所有未处理事件（恢复/重启后使用）。 */
    public synchronized long resume() throws IOException {
        List<Event> all = inputLog.readAll();
        pump(all);
        return nextOffset;
    }

    private void pump(List<Event> events) throws IOException {
        for (Event e : events) {
            if (e.offset < nextOffset) continue; // 已检查点覆盖的事件：跳过
            aggregate.process(e);
            nextOffset++;
            sinceCheckpoint++;
            if (policy.shouldCheckpoint(sinceCheckpoint, nextOffset, clock)) {
                checkpoint(false);
            }
        }
    }

    /** 强制在当前偏移处做一次检查点（无未处理事件时为 no-op，返回最近检查点号）。 */
    public synchronized long flushCheckpoint() throws IOException {
        if (sinceCheckpoint > 0) {
            checkpoint(true);
        }
        return lastCheckpointId;
    }

    private void checkpoint(boolean forced) throws IOException {
        long id = lastCheckpointId + 1;
        long snapshotOffset = nextOffset;
        Map<String, Map<String, Object>> stateSnapshot = aggregate.snapshot();
        List<Map<String, Object>> rows = aggregate.rows();

        // 预先判定本次检查点各阶段是否注入（isArmed 一次性消耗）。存储层在恰好的注入点
        // 抛出 InjectedFaultException；异常抛出后本方法立即终止，内部水位不会推进。

        boolean crashStateWrite = faults.isArmed(FaultPhase.STATE_WRITE, id);
        boolean crashOutputStage = faults.isArmed(FaultPhase.OUTPUT_STAGE, id);
        boolean crashOutputCommit = faults.isArmed(FaultPhase.OUTPUT_COMMIT, id);
        boolean crashTableApply = faults.isArmed(FaultPhase.TABLE_APPLY, id);

        // ① 状态+偏移原子写盘（tmp 已 fsync、rename 之前）
        checkpointStore.write(id, snapshotOffset, stateSnapshot, rows.size(), clock, crashStateWrite);

        // ② 输出事务暂存（staged 文件已 fsync、commit 之前）
        outputLog.stage(id, snapshotOffset, rows, clock, crashOutputStage);

        // ③ 输出事务提交（rename 之后、应用到汇总表之前）
        outputLog.commit(id, crashOutputCommit);

        // ④ 由已提交日志重建并原子重写汇总表（rename 之后；此处崩溃无害，启动时会重建）
        Map<String, Map<String, Object>> view = summaryTable.rebuildFrom(outputLog.readCommitted());
        summaryTable.rewriteAtomically(view, crashTableApply, id);

        // 协议完整走完后才推进内部水位
        lastCheckpointId = id;
        sinceCheckpoint = 0;
        policy.onCheckpoint(clock);
    }

    /** 对外状态视图（验收比较的“最终偏移和汇总”来自这里）。 */
    public synchronized Map<String, Object> status() throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("lastCheckpointId", lastCheckpointId);
        m.put("nextOffset", nextOffset);
        m.put("inputEvents", inputLog.count());
        m.put("committedTxns", outputLog.committedIds());
        m.put("summary", new TreeMap<>(summaryTable.read()));
        m.put("policy", policy.toString());
        List<String> armed = new ArrayList<>();
        for (Map.Entry<FaultPhase, Long> e : faults.arms().entrySet()) {
            armed.add(e.getKey() + "@" + (e.getValue() <= 0 ? "next" : e.getValue()));
        }
        m.put("armedFaults", armed);
        return m;
    }

    public synchronized long nextOffset() {
        return nextOffset;
    }

    public synchronized long lastCheckpointId() {
        return lastCheckpointId;
    }
}
