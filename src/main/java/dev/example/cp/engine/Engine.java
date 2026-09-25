package dev.example.cp.engine;

import dev.example.cp.core.Event;
import dev.example.cp.core.KeyedSumOperator;
import dev.example.cp.fail.CrashPoint;
import dev.example.cp.fail.FailureInjector;
import dev.example.cp.storage.OutputStore;
import dev.example.cp.storage.SourceLog;
import dev.example.cp.storage.StateStore;
import dev.example.cp.storage.SummaryTable;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.TreeMap;

/**
 * 单线程事件流引擎：消费 {@link SourceLog}，驱动 {@link KeyedSumOperator}，
 * 并按检查点协议把“输入偏移 + 算子状态 + 本地输出”原子地推进。
 *
 * <h2>检查点事务（每个 epoch）</h2>
 * <ol>
 *   <li>把本 epoch 的输出写入 {@code output/committing/chk-&lt;id&gt;.pending}（fsync + 原子发布）</li>
 *   <li>把算子状态与 lastConsumedOffset 写入 {@code state/latest.json}（tmp→latest 原子发布）</li>
 *   <li>pending → committed（原子 rename，输出事务提交点）</li>
 *   <li>把完整聚合快照应用进受控汇总表 {@code output/table.json}（原子重写 + appliedEpoch 推进）</li>
 * </ol>
 * 每一步前后都可注入故障；{@link #recover()} 负责让下一次启动收敛到与无故障执行相同的结果。
 */
public final class Engine {

    /**
     * “外部副作用”演示钩子：在状态已落盘、但本地输出事务<b>尚未提交</b>时触发。
     * 恢复路径为了补完同一 epoch 会再次触发它 —— 这正是重复外部副作用的来源。
     * 生产系统必须用幂等键 / 两阶段提交 / 事务性 sink 替换它，本协议不做保证。
     */
    @FunctionalInterface
    public interface UnsafeExternalSideEffect {
        void fire(OutputRecord rec);
    }

    private final Path dataDir;
    private final SourceLog source;
    private final KeyedSumOperator operator;
    private final StateStore states;
    private final OutputStore outputs;
    private final SummaryTable table;
    private final FailureInjector faults;
    private final Clock clock;
    private final CheckpointScheduler scheduler;

    private long lastConsumedOffset;
    private long latestEpochId;
    private long processedInRun;
    private final List<OutputRecord.Delta> epochDeltas = new ArrayList<>();
    private UnsafeExternalSideEffect unsafeHook;

    private Engine(Path dataDir, CheckpointScheduler scheduler, Clock clock) {
        this.dataDir = dataDir;
        this.scheduler = scheduler;
        this.clock = clock;
        this.faults = new FailureInjector(dataDir);
        this.source = new SourceLog(dataDir);
        this.operator = new KeyedSumOperator();
        this.states = new StateStore(dataDir, faults);
        this.outputs = new OutputStore(dataDir, faults);
        this.table = new SummaryTable(dataDir, faults);
    }

    /** 打开（或恢复）一个引擎实例。崩溃后的旧实例不可复用，必须重新 open。 */
    public static Engine open(Path dataDir, CheckpointScheduler scheduler, Clock clock) {
        Engine e = new Engine(dataDir, scheduler, clock);
        e.recover();
        return e;
    }

    public Engine withUnsafeSideEffect(UnsafeExternalSideEffect hook) {
        this.unsafeHook = hook;
        return this;
    }

    public FailureInjector faults() {
        return faults;
    }

    public SourceLog source() {
        return source;
    }

    // ---------------- 恢复 ----------------

    /**
     * 启动恢复（{@code open} 时自动执行）：
     * <pre>
     *   state/latest.json 存在？
     *     是 → 恢复算子状态与 lastConsumedOffset，stateEpoch = id
     *     否 → 空状态，stateEpoch = -1
     *   清理 *.write-tmp / latest.tmp 等崩溃残片
     *   pending 文件：
     *     id == stateEpoch → 状态在、输出未提交（COMMIT_RENAME 点崩溃）→ 补提交 + 补应用
     *     id &gt; stateEpoch → 状态未发布（STATE_WRITE/PENDING_WRITE 点崩溃）→ 丢弃整个 epoch
     *     id &lt; stateEpoch → 不应存在，防御性丢弃
     *   committed 文件按 id 升序，把 table 落后的部分幂等补应用
     * </pre>
     */
    synchronized void recover() {
        states.pruneTemps();
        outputs.pruneTemps();

        CheckpointState cp = states.loadLatest();
        long stateEpoch;
        if (cp != null) {
            operator.restore(cp.operator());
            lastConsumedOffset = cp.lastConsumedOffset();
            stateEpoch = cp.epochId();
        } else {
            operator.reset();
            lastConsumedOffset = -1L;
            stateEpoch = 0L; // 尚无检查点；第一个 epoch 将编号为 1
        }
        latestEpochId = stateEpoch;

        long tableEpoch = table.load().appliedEpoch();

        // 1) 已提交但汇总表尚未应用（TABLE_APPLY / AFTER_COMMIT 点崩溃）：升序幂等补应用。
        for (OutputRecord rec : outputs.readAllCommitted()) {
            if (rec.epochId() > tableEpoch) {
                table.apply(rec);
                tableEpoch = rec.epochId();
            }
        }

        // 2) 处理 pending：只有 id == stateEpoch 的那个对应“状态在、提交未完成”，需要补完。
        List<Long> pendingIds = listPendingIds();
        for (long id : pendingIds) {
            OutputRecord rec = outputs.readPending(id);
            if (id == stateEpoch && rec != null) {
                // 注意：外部副作用钩子在补提交时会再次触发 —— 见 README“适用边界”。
                if (unsafeHook != null) {
                    unsafeHook.fire(rec);
                }
                outputs.commit(id);
                table.apply(rec);
            } else {
                // 状态从未发布，整个 epoch 作废（其事件恢复后会被重放）。
                outputs.discardPending(id);
            }
        }

        // 3) 收敛断言：表已应用到 stateEpoch（有检查点时）；状态与表偏移一致。
        SummaryTable.TableState finalTable = table.load();
        if (cp != null && finalTable.appliedEpoch() != stateEpoch) {
            throw new IllegalStateException("recovery failed to converge: stateEpoch=" + stateEpoch
                    + " tableEpoch=" + finalTable.appliedEpoch());
        }
        if (cp != null && finalTable.appliedEpoch() >= 0
                && finalTable.lastConsumedOffset() != cp.lastConsumedOffset()) {
            throw new IllegalStateException("recovery offset mismatch: stateOffset=" + cp.lastConsumedOffset()
                    + " tableOffset=" + finalTable.lastConsumedOffset());
        }
    }

    private List<Long> listPendingIds() {
        List<Long> ids = new ArrayList<>();
        Path dir = dataDir.resolve("output").resolve("committing");
        if (!Files.exists(dir)) {
            return ids;
        }
        try (var paths = Files.newDirectoryStream(dir, "chk-*.pending")) {
            for (Path p : paths) {
                String name = p.getFileName().toString();
                ids.add(Long.parseLong(name.substring("chk-".length(), name.length() - ".pending".length())));
            }
        } catch (Exception e) {
            throw new IllegalStateException("list pending outputs failed", e);
        }
        ids.sort(Long::compareTo);
        return ids;
    }

    // ---------------- 运行 ----------------

    public record RunReport(long eventsProcessed, long checkpointsCompleted) {
    }

    /**
     * 消费输入日志中所有尚未处理的事件；按调度器策略自动做检查点。
     * 若注入的故障在本方法内触发，调用方会收到 {@link dev.example.cp.fail.InjectedCrash}，
     * 本实例即告作废，需要重新 {@link #open} 恢复。
     */
    public synchronized RunReport runUntilDrained() {
        long events = 0;
        long cps = 0;
        List<Event> all = source.readAll();
        while (true) {
            long idx = lastConsumedOffset + 1;
            if (idx < 0 || idx >= all.size()) {
                break;
            }
            Event event = all.get((int) idx);
            KeyedSumOperator.Output out = operator.process(event);
            epochDeltas.add(new OutputRecord.Delta(event.offset(), event.key(), out.sumAfter()));
            lastConsumedOffset = event.offset();
            processedInRun++;
            events++;
            // 调度器看到的是全局位置（偏移从 0 起），保证检查点边界跨恢复确定。
            if (scheduler.shouldCheckpointAfterEvent(lastConsumedOffset, clock)) {
                completeCheckpoint();
                cps++;
            }
        }
        return new RunReport(events, cps);
    }

    /**
     * 处理事件直到调度器触发并完成<b>下一个</b>检查点后立即返回（精确测试用）。
     *
     * <p>恢复场景下尤其有用：恢复后调用本方法只会把“当前应完成的那一个 epoch”提交，
     * 不会顺手把未来 epoch 也提交掉，从而可以在下一个 epoch 再次注入故障。
     *
     * @return 本进程处理的事件数与完成的检查点数（0/1）；输入耗尽且无新检查点则返回 (0,0)
     */
    public synchronized RunReport runUntilNextCheckpoint() {
        long events = 0;
        List<Event> all = source.readAll();
        while (true) {
            long idx = lastConsumedOffset + 1;
            if (idx < 0 || idx >= all.size()) {
                return new RunReport(events, 0);
            }
            Event event = all.get((int) idx);
            KeyedSumOperator.Output out = operator.process(event);
            epochDeltas.add(new OutputRecord.Delta(event.offset(), event.key(), out.sumAfter()));
            lastConsumedOffset = event.offset();
            processedInRun++;
            events++;
            if (scheduler.shouldCheckpointAfterEvent(lastConsumedOffset, clock)) {
                completeCheckpoint();
                return new RunReport(events, 1);
            }
        }
    }

    /**
     * 从上次位置开始处理事件，直到消费了 {@code maxEvents} 条本进程内的新事件
     * （主要供精确测试使用：构造“若干 epoch 已完成、崩溃发生在下一个 epoch”的现场）。
     */
    public synchronized RunReport runUntilEventCount(long maxEvents) {        long events = 0;
        long cps = 0;
        List<Event> all = source.readAll();
        while (events < maxEvents) {
            long idx = lastConsumedOffset + 1;
            if (idx < 0 || idx >= all.size()) {
                break;
            }
            Event event = all.get((int) idx);
            KeyedSumOperator.Output out = operator.process(event);
            epochDeltas.add(new OutputRecord.Delta(event.offset(), event.key(), out.sumAfter()));
            lastConsumedOffset = event.offset();
            processedInRun++;
            events++;
            if (scheduler.shouldCheckpointAfterEvent(lastConsumedOffset, clock)) {
                completeCheckpoint();
                cps++;
            }
        }
        return new RunReport(events, cps);
    }

    /**
     * 排空后若还有已处理但未提交的事件，补一个检查点，
     * 保证 CLI “一次 run”结束时所有输入都已进入受控汇总表。
     */
    public synchronized RunReport runUntilDrainedAndCommitted() {
        RunReport r = runUntilDrained();
        if (!epochDeltas.isEmpty()) {
            completeCheckpoint();
            r = new RunReport(r.eventsProcessed(), r.checkpointsCompleted() + 1);
        }
        return r;
    }

    /** 同 {@link #runUntilDrainedAndCommitted()}，但限定本进程最多处理 maxEvents 条（CLI/测试用）。 */
    public synchronized RunReport runUntilEventCountAndCommitted(long maxEvents) {
        RunReport r = runUntilEventCount(maxEvents);
        if (!epochDeltas.isEmpty()) {
            completeCheckpoint();
            r = new RunReport(r.eventsProcessed(), r.checkpointsCompleted() + 1);
        }
        return r;
    }

    /** 显式屏障：若有未提交内容则完成一个检查点；空操作不推进 epoch（HTTP POST /checkpoints）。 */
    public synchronized long triggerCheckpoint() {
        if (epochDeltas.isEmpty()) {
            return latestEpochId;
        }
        completeCheckpoint();
        return latestEpochId;
    }

    private void completeCheckpoint() {
        long epochId = latestEpochId + 1;
        OutputRecord rec = new OutputRecord(
                epochId, lastConsumedOffset, operator.currentSums(), List.copyOf(epochDeltas));

        // 1) pending 输出（PENDING_WRITE 故障点在内部）
        outputs.writePending(rec);
        // 2) 算子状态 + 输入偏移（STATE_WRITE 故障点在内部）
        states.persist(new CheckpointState(epochId, lastConsumedOffset, operator.snapshot()));
        // 3) 危险区：外部副作用在本地事务提交“之前”发生，崩溃后恢复会重复 —— 仅用于演示。
        if (unsafeHook != null) {
            unsafeHook.fire(rec);
        }
        // 4) 输出事务提交（COMMIT_RENAME 故障点在内部）
        outputs.commit(epochId);
        // 5) 应用到受控汇总表（TABLE_APPLY 故障点在内部）
        table.apply(rec);
        // 6) 提交后崩溃点
        faults.crashIfArmed(CrashPoint.AFTER_COMMIT, epochId);

        latestEpochId = epochId;
        epochDeltas.clear();
    }

    // ---------------- 观测 ----------------

    /** 当前可比较状态（连续执行与恢复执行的最终偏移 / 汇总都取自这里）。 */
    public synchronized Status status() {
        SummaryTable.TableState t = table.load();
        return new Status(
                latestEpochId,
                lastConsumedOffset,
                t.appliedEpoch(),
                t.lastConsumedOffset(),
                source.count(),
                operator.processedCount(),
                new TreeMap<>(t.sums()));
    }

    /** 引擎对外的可比较状态快照。 */
    public record Status(long latestEpoch, long lastConsumedOffset, long appliedEpoch,
                         long committedOffset, long sourceEvents, long processedCount,
                         TreeMap<String, Long> sums) {
    }
}
