package txsnapshot;

import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.locks.ReentrantLock;

/**
 * 单线程串行处理的流算子引擎，在“处理 → 状态落盘 → 输出准备 → 提交”之间
 * 维护状态、输入偏移与输出提交标记的一致快照。
 *
 * 每个输入批次（本实现一条输入 = 一个批次）的提交顺序：
 *
 *   1. 输入先 fsync 进 input.log（在入队之前完成，保证不丢）
 *   2. 纯计算（可崩溃：无任何落盘，重放即可）
 *   3. sink.prepare：输出记录写入暂存段（可崩溃：残段恢复时回滚，不可见）
 *   4. 快照原子发布：{state, offset, pendingCommit=txnId}（可崩溃：见下）
 *   5. sink.commit：提交标记写 commit.log（可崩溃：半行截断 = 未提交）
 *
 * 恢复协议：
 *   a. commit.log 截尾，读到已提交事务集合；
 *   b. 快照中的 pendingCommit：若未提交则补提交标记（暂存段在快照之前已落盘）；
 *      若已提交则天然幂等；暂存段缺失则拒绝启动（宁可暴露损坏也不猜测）；
 *   c. 其余暂存段全部回滚删除；
 *   d. 从快照 offset+1 开始重放 input.log。
 */
public final class StreamEngine implements AutoCloseable {

    private static final class Ticket {
        final long offset;
        final long value;
        final CompletableFuture<Void> done = new CompletableFuture<>();

        Ticket(long offset, long value) {
            this.offset = offset;
            this.value = value;
        }
    }

    private final Path dataDir;
    private final InputLog inputLog;
    private final CheckpointStore checkpointStore;
    private final LocalTransactionalSink sink;
    private final Fault fault;

    private final ReentrantLock commitLock = new ReentrantLock();
    private long state;
    private long durableOffset;       // 快照所代表的已完成偏移
    private long epoch;
    private volatile boolean crashed;

    private final Deque<Ticket> queue = new ArrayDeque<>();
    private final Object queueLock = new Object();
    private Thread worker;
    private volatile boolean running;

    private StreamEngine(Path dataDir, InputLog inputLog, CheckpointStore checkpointStore,
                         LocalTransactionalSink sink, Fault fault,
                         long state, long durableOffset, long epoch) {
        this.dataDir = dataDir;
        this.inputLog = inputLog;
        this.checkpointStore = checkpointStore;
        this.sink = sink;
        this.fault = fault;
        this.state = state;
        this.durableOffset = durableOffset;
        this.epoch = epoch;
    }

    /** 打开引擎：加载快照、恢复接收器、重放未入快照的输入，然后启动处理线程。 */
    public static StreamEngine open(Path dataDir, Fault fault) throws IOException {
        DurableFiles.ensureDir(dataDir);
        InputLog log = InputLog.open(dataDir);
        CheckpointStore store = new CheckpointStore(dataDir.resolve("checkpoints"));
        CheckpointStore.Snapshot snap = store.loadLatest();

        long state = 0L;
        long durableOffset = -1L;
        long epoch = 0L;
        String pending = null;
        if (snap != null) {
            state = snap.state;
            durableOffset = snap.offset;
            epoch = snap.epoch;
            pending = snap.pendingCommit;
        }

        // 接收器恢复：悬空事务在此处被补提交或回滚
        LocalTransactionalSink sink = LocalTransactionalSink.open(dataDir.resolve("sink"), pending);

        StreamEngine engine = new StreamEngine(dataDir, log, store, sink, fault,
                state, durableOffset, epoch);

        // 重放：snapshot.offset 之后的所有输入（故障点不武装）
        List<long[]> replay = InputLog.readFrom(dataDir, durableOffset + 1);
        for (long[] rec : replay) {
            engine.processOne(rec[0], rec[1], null);
        }
        engine.startWorker();
        return engine;
    }

    /** 追加一条输入（fsync 后入队），返回其偏移并阻塞到处理完成。 */
    public long ingest(long value) throws IOException, ExecutionException, InterruptedException {
        if (crashed) throw new IllegalStateException("engine is in crashed state; restart required");
        long offset = inputLog.append(value);
        Ticket ticket = new Ticket(offset, value);
        synchronized (queueLock) {
            queue.add(ticket);
            queueLock.notifyAll();
        }
        ticket.done.get();
        if (crashed) throw new IllegalStateException("engine crashed while processing; restart required");
        return offset;
    }

    /** 只读状态视图。 */
    public Map<String, Object> status() {
        Map<String, Object> m = new LinkedHashMap<>();
        synchronized (this) {
            m.put("offset", durableOffset);
            m.put("state", state);
            m.put("epoch", epoch);
        }
        m.put("committedOutputs", sink.visibleCount());
        m.put("queued", queue.size());
        m.put("nextInputOffset", inputLog.nextOffset());
        m.put("crashed", crashed);
        return m;
    }

    public List<Map<String, Object>> outputs() throws IOException {
        return sink.readVisible();
    }

    public Fault fault() {
        return fault;
    }

    // ---- 内部 ----

    private void startWorker() {
        running = true;
        worker = new Thread(this::workerLoop, "stream-worker");
        worker.setDaemon(true);
        worker.start();
    }

    private void workerLoop() {
        while (running) {
            Ticket t;
            synchronized (queueLock) {
                while (running && queue.isEmpty()) {
                    try { queueLock.wait(); } catch (InterruptedException e) { return; }
                }
                if (!running) return;
                t = queue.pollFirst();
            }
            if (t == null) continue;
            try {
                processOne(t.offset, t.value, fault);
                t.done.complete(null);
            } catch (Fault.InjectedFault f) {
                // EXCEPTION 模式：语义上等同于此刻进程崩溃，进程内引擎不再可信
                crashed = true;
                t.done.completeExceptionally(f);
                List<Ticket> rest;
                synchronized (queueLock) {
                    rest = new ArrayList<>(queue);
                    queue.clear();
                }
                for (Ticket q : rest) q.done.completeExceptionally(f);
                System.err.println("[engine] fault injected (" + f.point
                        + "); engine marked crashed, restart to recover");
                return;
            } catch (Throwable e) {
                t.done.completeExceptionally(e);
            }
        }
    }

    /**
     * 处理单个批次。可注入故障。包级可见以便单元测试直接调用。
     */
    void processOne(long offset, long value, Fault injected) throws IOException {
        String txnId = LocalTransactionalSink.txnIdForOffset(offset);

        // 2) 纯计算
        long newState = state + value;
        long result = value * value;
        if (injected != null) injected.fire(Fault.Point.DURING_PROCESSING, offset);

        // 3) prepare（DURING_PREPARE 可在此触发）
        sink.prepare(txnId, offset, value, injected);

        // 4) 一致快照原子发布（offset 与 pendingCommit 一起生效）
        long newEpoch;
        synchronized (this) {
            newEpoch = epoch + 1;
            epoch = newEpoch;
            state = newState;
        }
        checkpointStore.save(newEpoch, offset, txnId, newState);

        // 快照落盘之后、提交标记之前（崩溃后走“悬空事务补提交”路径）
        if (injected != null) injected.fire(Fault.Point.AFTER_STATE_PERSISTED, offset);

        // 5) commit（DURING_COMMIT 可在此触发，可能留下半行标记）
        sink.commit(txnId, offset, injected);

        synchronized (this) {
            durableOffset = offset;
        }
        // result 仅用于说明算子语义，实际记录已在 prepare 阶段写入
        if (result == Long.MIN_VALUE) throw new AssertionError();
    }

    @Override
    public void close() throws IOException {
        running = false;
        synchronized (queueLock) {
            queueLock.notifyAll();
        }
        if (worker != null) worker.interrupt();
        try {
            if (worker != null) worker.join(2000);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        inputLog.close();
        sink.close();
    }
}
