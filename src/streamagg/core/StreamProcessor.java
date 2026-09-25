package streamagg.core;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;

/**
 * 流式纠错聚合引擎（纯内存、单线程语义、所有公开方法加锁，线程安全）。
 *
 * <p>支持按事件 ID 的三类操作：
 * <ul>
 *   <li>ADD：新增事件（键、值）；</li>
 *   <li>RETRACT：撤销事件（带值的撤销被禁止）；</li>
 *   <li>CORRECT：更正事件（可改值，也可通过带新 key 改键）。</li>
 * </ul>
 *
 * <p>版本与乱序：显式 {@code version}（从 1 起单调递增）按版本排序；乱序到达且存在版本
 * 空洞时操作进入缓存，待前置版本补齐后级联排空。未带版本的操作由引擎按摄入顺序赋予
 * 合成版本（“先撤销后新增”也安全：撤销先占 v2 缓存，新增到 v1 后级联生效）。
 *
 * <p>幂等：同一 opId 的重复提交整体忽略；同一事件同一版本内容相同也忽略。
 * 计数安全：撤销不存在/未到达的事件是空操作，count 永不低于 0，否则抛
 * {@link IllegalStateException}（禁止计数负漂移）。
 */
public final class StreamProcessor {

    /** 单个事件的全部生命周期状态。 */
    private static final class EventState {
        /** 已应用版本 -> 操作（版本必为 1..k 的连续前缀）。 */
        final TreeMap<Long, EventOp> applied = new TreeMap<>();
        /** 因版本空洞缓存的操作。 */
        final TreeMap<Long, EventOp> pending = new TreeMap<>();

        boolean active;
        String currentKey;
        BigDecimal currentValue;

        boolean hasAppliedAdd() {
            return applied.values().stream().anyMatch(o -> o.type() == OpType.ADD);
        }
    }

    private final Clock clock;
    private final Scheduler scheduler;
    private final Map<String, EventState> states = new LinkedHashMap<>();
    private final Map<String, KeyStats> aggregates = new LinkedHashMap<>();
    private final Map<String, LedgerEntry> ledger = new LinkedHashMap<>();
    private final List<JournalEntry> journal = new ArrayList<>();
    /** opId -> eventId，跨事件复用同一 opId 属于客户端错误。 */
    private final Map<String, String> seenOpIds = new HashMap<>();

    private long journalSeq;
    private Cancellable reconciliationTask;
    private volatile ReconciliationReport lastReconciliation;

    public StreamProcessor() {
        this(new SystemClock(), null, 0);
    }

    public StreamProcessor(Clock clock) {
        this(clock, null, 0);
    }

    /**
     * @param clock                可注入时钟
     * @param scheduler            可注入调度器；非空且 {@code reconcilePeriodMillis > 0}
     *                             时周期执行“增量状态 vs 账本重算”对账
     * @param reconcilePeriodMillis 对账周期（毫秒）
     */
    public StreamProcessor(Clock clock, Scheduler scheduler, long reconcilePeriodMillis) {
        this.clock = Objects.requireNonNull(clock);
        this.scheduler = scheduler;
        if (scheduler != null && reconcilePeriodMillis > 0) {
            this.reconciliationTask = scheduler.scheduleAtFixedRate(
                    () -> {
                        try {
                            lastReconciliation = reconcile();
                        } catch (RuntimeException e) {
                            lastReconciliation = new ReconciliationReport(
                                    false, "对账任务异常: " + e, Map.of(), Map.of());
                        }
                    },
                    reconcilePeriodMillis, reconcilePeriodMillis);
        }
    }

    // ------------------------------------------------------------------
    // 提交入口
    // ------------------------------------------------------------------

    /** 提交一条事件操作。 */
    public synchronized ApplyResult submit(EventOp op) {
        Objects.requireNonNull(op);

        // 1) opId 级幂等
        if (op.opId() != null) {
            String owner = seenOpIds.get(op.opId());
            if (owner != null) {
                if (!owner.equals(op.eventId())) {
                    throw new IllegalArgumentException(
                            "opId=" + op.opId() + " 已用于其他事件: " + owner);
                }
                EventState st0 = states.get(op.eventId());
                long known = st0 == null ? 0L : Math.max(
                        st0.applied.isEmpty() ? 0L : st0.applied.lastKey(),
                        st0.pending.isEmpty() ? 0L : st0.pending.lastKey());
                return ApplyResult.duplicate(op.eventId(), known);
            }
        }

        EventState st = states.computeIfAbsent(op.eventId(), k -> new EventState());

        // 2) 版本规范化
        long v = resolveVersion(op, st);

        // 3) 已应用版本：重复幂等 / 同版本冲突拒绝
        EventOp existing = st.applied.get(v);
        if (existing != null) {
            if (samePayload(existing, op)) {
                if (op.opId() != null) {
                    seenOpIds.put(op.opId(), op.eventId());
                }
                return ApplyResult.duplicate(op.eventId(), v);
            }
            return ApplyResult.staleConflict(op.eventId(), v,
                    "版本 " + v + " 已应用不同操作（陈旧消息）");
        }

        // 4) 已缓存版本：同样幂等 / 冲突拒绝
        EventOp buffered = st.pending.get(v);
        if (buffered != null) {
            if (samePayload(buffered, op)) {
                if (op.opId() != null) {
                    seenOpIds.put(op.opId(), op.eventId());
                }
                return ApplyResult.duplicate(op.eventId(), v);
            }
            return ApplyResult.staleConflict(op.eventId(), v,
                    "版本 " + v + " 已缓存不同操作（冲突消息）");
        }

        // 登记 opId（先登记，保证后续重复提交被幂等拦截）
        if (op.opId() != null) {
            seenOpIds.put(op.opId(), op.eventId());
        }

        // 5) 版本空洞 -> 缓存等待
        long expected = (long) st.applied.size() + 1;
        if (v > expected) {
            st.pending.put(v, op);
            journal.add(new JournalEntry(++journalSeq, op, v, clock.nowMillis()));
            return ApplyResult.buffered(op.eventId(), v);
        }
        if (v != expected) {
            // 理论不可达：applied 中不存在又小于 expected
            throw new IllegalStateException("内部错误: 版本解析异常 v=" + v + " expected=" + expected);
        }

        // 6) 应用并级联排空缓存
        st.applied.put(v, op);
        journal.add(new JournalEntry(++journalSeq, op, v, clock.nowMillis()));
        applyOne(st, op);

        long lastApplied = v;
        while (true) {
            long next = lastApplied + 1;
            EventOp drain = st.pending.remove(next);
            if (drain == null) {
                break;
            }
            st.applied.put(next, drain);
            applyOne(st, drain);
            lastApplied = next;
        }
        refreshLedger(op.eventId(), st);
        return ApplyResult.applied(op.eventId(), lastApplied);
    }

    /**
     * 解析操作的规范版本。显式版本直接使用；无版本操作按摄入顺序赋合成版本：
     * ADD 取当前最小未占版本；RETRACT/CORRECT 在事件生命周期尚未开始（无任何已应用
     * 操作）时保留 v1 给未来的 ADD，从 v2 起寻找空位。
     */
    private long resolveVersion(EventOp op, EventState st) {
        if (op.version() != null) {
            if (op.version() < 1) {
                throw new IllegalArgumentException("version 必须 >= 1: " + op.version());
            }
            return op.version();
        }
        long start;
        if (op.type() == OpType.ADD) {
            start = 1;
        } else {
            start = st.applied.isEmpty() ? 2 : firstFree(st, 1);
        }
        return firstFree(st, start);
    }

    private long firstFree(EventState st, long start) {
        long v = start;
        while (st.applied.containsKey(v) || st.pending.containsKey(v)) {
            v++;
        }
        return v;
    }

    private boolean samePayload(EventOp a, EventOp b) {
        if (a.type() != b.type()) {
            return false;
        }
        if (!Objects.equals(a.key(), b.key())) {
            return false;
        }
        if (a.value() == null || b.value() == null) {
            return a.value() == null && b.value() == null;
        }
        return a.value().compareTo(b.value()) == 0;
    }

    // ------------------------------------------------------------------
    // 增量状态变更
    // ------------------------------------------------------------------

    private void applyOne(EventState st, EventOp op) {
        switch (op.type()) {
            case ADD -> {
                if (op.key() == null || op.value() == null) {
                    throw new IllegalArgumentException("ADD 操作必须带 key 与 value");
                }
                if (!st.active) {
                    addToAggregate(op.key(), op.value());
                    st.active = true;
                } else {
                    // 对已存活事件再次 ADD：按 upsert 处理（等价于更正值/键），计数不重复增加
                    moveOrReplace(st, op.key(), op.value());
                }
                st.currentKey = op.key();
                st.currentValue = op.value();
            }
            case RETRACT -> {
                if (op.value() != null) {
                    throw new IllegalArgumentException("RETRACT 不允许带 value");
                }
                if (st.active) {
                    removeFromAggregate(st.currentKey, st.currentValue);
                    st.active = false;
                    st.currentKey = null;
                    st.currentValue = null;
                }
                // 撤销不存在/尚未新增的事件：空操作，绝不产生负计数
            }
            case CORRECT -> {
                if (op.value() == null) {
                    throw new IllegalArgumentException("CORRECT 必须带新 value");
                }
                String newKey = op.key() != null ? op.key() : st.currentKey;
                if (newKey == null) {
                    throw new IllegalArgumentException(
                            "CORRECT 无法继承 key：事件尚无 key（eventId=" + op.eventId() + "）");
                }
                if (!st.active) {
                    // 对已撤销（或尚未真正新增成功）事件的更正：按新值重新计入
                    addToAggregate(newKey, op.value());
                    st.active = true;
                } else {
                    moveOrReplace(st, newKey, op.value());
                }
                st.currentKey = newKey;
                st.currentValue = op.value();
            }
        }
    }

    private void moveOrReplace(EventState st, String newKey, BigDecimal newValue) {
        if (st.currentKey.equals(newKey)) {
            KeyStats cur = aggregates.get(st.currentKey);
            aggregates.put(st.currentKey, cur.replace(st.currentValue, newValue));
        } else {
            removeFromAggregate(st.currentKey, st.currentValue);
            addToAggregate(newKey, newValue);
        }
    }

    private void addToAggregate(String key, BigDecimal value) {
        KeyStats cur = aggregates.get(key);
        aggregates.put(key, cur == null ? new KeyStats(1L, value) : cur.add(value));
    }

    private void removeFromAggregate(String key, BigDecimal value) {
        KeyStats cur = aggregates.get(key);
        if (cur == null) {
            // 防御性检查：正常流程不可达
            throw new IllegalStateException("禁止计数负漂移: key=" + key + " 无聚合记录却要撤销");
        }
        KeyStats next = cur.remove(value); // count<0 时 KeyStats 构造直接抛异常
        if (next.count() == 0 && next.sum().signum() == 0) {
            aggregates.remove(key);
        } else {
            aggregates.put(key, next);
        }
    }

    private void refreshLedger(String eventId, EventState st) {
        if (st.active) {
            long lastVersion = st.applied.lastKey();
            ledger.put(eventId, new LedgerEntry(
                    eventId, st.currentKey, st.currentValue, lastVersion, true));
        } else {
            ledger.remove(eventId);
        }
    }

    // ------------------------------------------------------------------
    // 查询
    // ------------------------------------------------------------------

    /** 单个键的当前聚合；不存在返回零值。 */
    public synchronized KeyStats statsOf(String key) {
        return aggregates.getOrDefault(key, KeyStats.ZERO);
    }

    /** 全部键聚合快照（深拷贝）。 */
    public synchronized Map<String, KeyStats> allStats() {
        Map<String, KeyStats> copy = new LinkedHashMap<>();
        for (var e : aggregates.entrySet()) {
            copy.put(e.getKey(), new KeyStats(e.getValue().count(), e.getValue().sum()));
        }
        return copy;
    }

    /** 最终事件账本（仅存活事件）。 */
    public synchronized List<LedgerEntry> ledger() {
        return List.copyOf(ledger.values());
    }

    /** 原始输入日志（按接收顺序，含被缓存的操作）。 */
    public synchronized List<JournalEntry> journal() {
        return List.copyOf(journal);
    }

    /** 某事件当前被乱序缓存的操作（版本升序）。 */
    public synchronized List<EventOp> pendingOf(String eventId) {
        EventState st = states.get(eventId);
        if (st == null) {
            return List.of();
        }
        return List.copyOf(st.pending.values());
    }

    // ------------------------------------------------------------------
    // 账本重算 / 对账 / 重放
    // ------------------------------------------------------------------

    /** 用最终事件账本做一次小数据精确重算（参考实现，独立于增量状态）。 */
    public static Map<String, KeyStats> recomputeFromLedger(List<LedgerEntry> entries) {
        Map<String, BigDecimal[]> raw = new LinkedHashMap<>();
        // 先用 long 计数，负值直接暴露
        Map<String, long[]> counts = new LinkedHashMap<>();
        for (LedgerEntry e : entries) {
            if (!e.active() || e.key() == null || e.value() == null) {
                continue;
            }
            raw.computeIfAbsent(e.key(), k -> new BigDecimal[]{BigDecimal.ZERO})[0]
                    = raw.get(e.key())[0].add(e.value());
            counts.computeIfAbsent(e.key(), k -> new long[]{0})[0]++;
        }
        Map<String, KeyStats> out = new LinkedHashMap<>();
        for (var e : raw.entrySet()) {
            long c = counts.get(e.getKey())[0];
            if (c < 0) {
                throw new IllegalStateException("禁止计数负漂移: key=" + e.getKey());
            }
            out.put(e.getKey(), new KeyStats(c, e.getValue()[0]));
        }
        return out;
    }

    /** 对账：增量聚合 与 账本重算结果逐键比较。 */
    public synchronized ReconciliationReport reconcile() {
        Map<String, KeyStats> incremental = allStats();
        Map<String, KeyStats> recomputed = recomputeFromLedger(List.copyOf(ledger.values()));
        String detail = diffMaps(incremental, recomputed);
        return new ReconciliationReport(detail == null, detail, incremental, recomputed);
    }

    private static String diffMaps(Map<String, KeyStats> a, Map<String, KeyStats> b) {
        java.util.Set<String> keys = new java.util.LinkedHashSet<>();
        keys.addAll(a.keySet());
        keys.addAll(b.keySet());
        for (String k : keys) {
            KeyStats x = a.get(k);
            KeyStats y = b.get(k);
            if (x == null) {
                return "key=" + k + " 增量缺失，账本重算为 " + y;
            }
            if (y == null) {
                return "key=" + k + " 增量为 " + x + "，账本重算缺失";
            }
            if (x.count() != y.count() || x.sum().compareTo(y.sum()) != 0) {
                return "key=" + k + " 增量 " + x + " != 重算 " + y;
            }
        }
        return null;
    }

    /** 最近一次调度对账的结果（未配置周期对账时为 null）。 */
    public ReconciliationReport lastReconciliation() {
        return lastReconciliation;
    }

    /**
     * 重放：用原始输入日志在一个全新引擎上按序回放，与当前增量状态比较。
     * 重放时以日志中记录的规范版本为准，证明日志自足且结果确定。
     *
     * @return 对账式报告，consistent=true 表示重放结果与当前状态一致
     */
    public synchronized ReconciliationReport replay() {
        StreamProcessor replayed = new StreamProcessor(new ManualClock(0L));
        for (JournalEntry je : journal) {
            EventOp raw = je.op();
            EventOp canonical = new EventOp(
                    raw.eventId(), raw.type(), raw.key(), raw.value(),
                    je.canonicalVersion(), raw.opId());
            replayed.submit(canonical);
        }
        Map<String, KeyStats> current = allStats();
        Map<String, KeyStats> afterReplay = replayed.allStats();
        String detail = diffMaps(current, afterReplay);

        // 账本也要一致
        if (detail == null) {
            Map<String, LedgerEntry> a = new LinkedHashMap<>(this.ledger);
            Map<String, LedgerEntry> b = new LinkedHashMap<>();
            for (LedgerEntry e : replayed.ledger()) {
                b.put(e.eventId(), e);
            }
            if (!a.equals(b)) {
                detail = "账本不一致: current=" + a + " replay=" + b;
            }
        }
        return new ReconciliationReport(detail == null, detail, current, afterReplay);
    }

    /** 停止周期对账任务。 */
    public synchronized void shutdown() {
        if (reconciliationTask != null) {
            reconciliationTask.cancel();
            reconciliationTask = null;
        }
    }
}
