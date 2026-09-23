package cep;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 复杂事件序列匹配引擎：模式 A -> B -> C。
 *
 * 语义（README 中有同样的描述，二者必须保持一致）：
 *
 * 1. 按实体分组：每个 entityId 拥有独立的部分匹配状态，不同实体之间绝不串配。
 * 2. 十秒窗口：一次匹配要求 C.timestamp - A.timestamp &lt;= {@link #WINDOW_MS}
 *    （即 10000 毫秒，边界值合法：恰好 10.000 秒算命中，10.001 秒不算）。
 * 3. 跳过无关事件：类型不是 A/B/C 的事件可以正常写入，只更新该实体的时间水位，
 *    不参与、也不破坏任何部分匹配。
 * 4. 支持重叠匹配：每个 A 都会作为候选保留；一个 B 与窗口内所有存活 A 组合；
 *    一个 C 与窗口内所有存活的 (A,B) 组合。因此 A,A,B,C 产生 2 个匹配，
 *    各事件可同时属于多个匹配，引擎不会“消费”事件。
 * 5. 相同时间按输入序号排序：事件时间戳相同的事件，以接收时分配的全局单调递增
 *    seq（输入序号）决定先后；seq 由引擎在进入批次时按数组顺序分配。
 * 6. 乱序拒绝：同一实体下事件必须按非递减时间戳到达；晚到事件（时间戳小于
 *    该实体已见水位）直接抛出 {@link LateEventException}，由 HTTP 层返回 409，
 *    绝不静默重排或丢弃。
 *
 * 组合爆炸的处理是“显式失败”而不是静默截断：单个 C 事件命中的候选对数量一旦
 * 超过 {@link #maxCombines}，抛出 {@link CombinationLimitException}，本批次
 * 整体不生效（预检在状态副本上完成）。
 */
public final class Engine {

    /** 模式窗口长度，单位毫秒：10 秒。 */
    public static final long WINDOW_MS = 10_000L;

    /** 单个 C 事件允许命中的候选 (A,B) 对上限，防止组合爆炸耗尽内存。 */
    public final int maxCombines;

    /** 每个实体当前的部分匹配状态。LinkedHashMap 让状态接口输出稳定有序。 */
    private final Map<String, EntityState> entities = new LinkedHashMap<>();

    /** 自引擎启动以来产生的全部完整匹配，追加顺序即产生顺序。 */
    private final List<Match> matches = new ArrayList<>();

    /** 下一个待分配的全局输入序号。 */
    private long nextSeq = 0;

    public Engine() {
        this(100_000);
    }

    public Engine(int maxCombines) {
        if (maxCombines < 1) {
            throw new IllegalArgumentException("maxCombines 必须 >= 1");
        }
        this.maxCombines = maxCombines;
    }

    // -------------------------------------------------------------- 异常

    /** 晚到事件：同一实体下时间戳小于已见水位。 */
    public static final class LateEventException extends RuntimeException {
        public final String entityId;
        public final long incomingTs;
        public final long watermark;

        LateEventException(String entityId, long incomingTs, long watermark) {
            super("实体 " + entityId + " 收到晚到事件: 事件时间 " + incomingTs
                    + " 早于该实体已见水位 " + watermark
                    + "（事件必须按非递减时间戳到达；相同时间戳按输入序号排序）");
            this.entityId = entityId;
            this.incomingTs = incomingTs;
            this.watermark = watermark;
        }
    }

    /** 组合数显式上限：宁可报错也不静默截断组合。 */
    public static final class CombinationLimitException extends RuntimeException {
        public final int candidateCount;
        public final int limit;

        CombinationLimitException(int candidateCount, int limit) {
            super("单个 C 事件可组成 " + candidateCount + " 个匹配，超过上限 " + limit
                    + "。拒绝本批次以避免静默截断组合；"
                    + "如需放宽，可通过 --max-combines 调整上限");
            this.candidateCount = candidateCount;
            this.limit = limit;
        }
    }

    // ------------------------------------------------------- 内部状态结构

    /** 一个已经形成的部分匹配 A->B。 */
    static final class AB {
        final Event a;
        final Event b;

        AB(Event a, Event b) {
            this.a = a;
            this.b = b;
        }
    }

    /** 单实体状态：存活的候选 A、存活的部分匹配 A->B、时间水位。 */
    static final class EntityState {
        final List<Event> as = new ArrayList<>();
        final List<AB> abs = new ArrayList<>();
        long watermark = Long.MIN_VALUE;

        EntityState() {
        }

        EntityState copy() {
            EntityState c = new EntityState();
            c.as.addAll(this.as);
            for (AB ab : this.abs) {
                c.abs.add(new AB(ab.a, ab.b));
            }
            c.watermark = this.watermark;
            return c;
        }

        /**
         * 推进到时间 now：清除 A 锚点已落在 10 秒窗口之外的候选 A 与 A->B。
         * 边界保留：差恰好等于窗口的候选仍然合法。
         */
        void purge(long now, long windowMs) {
            as.removeIf(a -> now - a.timestamp > windowMs);
            abs.removeIf(ab -> now - ab.a.timestamp > windowMs);
        }
    }

    /** 一次批次写入的结果。 */
    public static final class IngestResult {
        /** 实际接收的事件（已分配输入序号），顺序即输入顺序。 */
        public final List<Event> accepted;
        /** 本次批次新产生的完整匹配。 */
        public final List<Match> created;
        /** 写入后全引擎累计匹配总数。 */
        public final long totalMatches;

        IngestResult(List<Event> accepted, List<Match> created, long totalMatches) {
            this.accepted = accepted;
            this.created = created;
            this.totalMatches = totalMatches;
        }
    }

    /**
     * 预检通过、等待提交的批次。持有已分配序号的事件与“如果提交将会产生的匹配”，
     * 供 HTTP 层实现“先 fsync 预写日志，再提交内存”的两阶段写入。
     */
    static final class PreparedBatch {
        final List<Event> assigned;
        final List<Match> wouldCreate;

        PreparedBatch(List<Event> assigned, List<Match> wouldCreate) {
            this.assigned = assigned;
            this.wouldCreate = wouldCreate;
        }
    }

    // ------------------------------------------------------------ 写入路径

    /**
     * 不经过预写日志直接写入（测试 / 嵌入式使用）。
     * 整个批次要么全部生效，要么全部不生效（晚到 / 超组合上限时抛异常且状态不变）。
     */
    public synchronized IngestResult ingest(List<Event> events) {
        return commit(ingestDryRun(events));
    }

    /**
     * 第一阶段：校验晚到事件、按输入顺序分配序号，并在状态副本上模拟整批，
     * 检查组合上限。任何失败都抛出异常且现状完全不变。
     */
    synchronized PreparedBatch ingestDryRun(List<Event> events) {
        if (events.isEmpty()) {
            throw new IllegalArgumentException("事件批次不能为空");
        }
        List<Event> assigned = new ArrayList<>(events.size());
        long seq = nextSeq;
        for (Event raw : events) {
            EntityState st = entities.get(raw.entityId);
            if (st != null && raw.timestamp < st.watermark) {
                throw new LateEventException(raw.entityId, raw.timestamp, st.watermark);
            }
            if (raw.seq != -1) {
                throw new IllegalArgumentException(
                        "待写入事件不得自带 seq（seq 由引擎统一分配）");
            }
            assigned.add(raw.withSeq(seq++));
        }

        // 在状态副本上预检组合上限；超限则现状完全不变。
        Map<String, EntityState> scratch = copyStates();
        List<Match> preview = new ArrayList<>();
        for (Event e : assigned) {
            int createdHere = applyTo(scratch, e, preview);
            if (createdHere > maxCombines) {
                throw new CombinationLimitException(createdHere, maxCombines);
            }
        }
        return new PreparedBatch(assigned, preview);
    }

    /**
     * 第二阶段：把预检通过的批次正式应用到内存状态。
     * 调用方必须已将 prepared.assigned 持久化到预写日志并完成 fsync。
     */
    synchronized IngestResult commit(PreparedBatch prepared) {
        List<Match> created = new ArrayList<>(prepared.wouldCreate.size());
        for (Event e : prepared.assigned) {
            applyTo(entities, e, created);
        }
        if (!created.equals(prepared.wouldCreate)) {
            // 预检结果与提交结果必须一致；不一致说明引擎有缺陷，绝不能静默继续。
            throw new IllegalStateException(
                    "提交阶段产生的匹配与预检结果不一致（引擎内部错误）");
        }
        matches.addAll(created);
        if (!prepared.assigned.isEmpty()) {
            nextSeq = prepared.assigned.get(prepared.assigned.size() - 1).seq + 1;
        }
        return new IngestResult(prepared.assigned, created, matches.size());
    }

    /**
     * 将单个事件应用到给定状态表上；返回该事件产生的完整匹配数量
     * （仅 C 事件可能大于 0），产生的匹配追加到 out。
     */
    private int applyTo(Map<String, EntityState> table, Event e, List<Match> out) {
        EntityState st = table.get(e.entityId);
        if (st == null) {
            st = new EntityState();
            table.put(e.entityId, st);
        }
        st.purge(e.timestamp, WINDOW_MS);

        if (e.isA()) {
            st.as.add(e);
        } else if (e.isB()) {
            for (Event a : st.as) {
                st.abs.add(new AB(a, e));
            }
        } else if (e.isC()) {
            int before = out.size();
            for (AB ab : st.abs) {
                if (e.timestamp - ab.a.timestamp <= WINDOW_MS) {
                    out.add(new Match(ab.a, ab.b, e));
                }
            }
            st.watermark = e.timestamp;
            return out.size() - before;
        }
        // A、B 以及无关事件：仅推进水位（无关事件不参与匹配）。
        st.watermark = e.timestamp;
        return 0;
    }

    private Map<String, EntityState> copyStates() {
        Map<String, EntityState> copy = new LinkedHashMap<>();
        for (Map.Entry<String, EntityState> en : entities.entrySet()) {
            copy.put(en.getKey(), en.getValue().copy());
        }
        return copy;
    }

    // ------------------------------------------------------- 故障恢复路径

    /**
     * 用预写日志重放出来的批次重建内存状态。重放事件已带有序号；
     * 重放结果必须与崩溃前完全一致（同样的事件序列 -> 同样的匹配集合）。
     */
    public synchronized void recover(List<List<Event>> loggedBatches) {
        long maxSeq = -1L;
        for (List<Event> batch : loggedBatches) {
            for (Event e : batch) {
                EntityState st = entities.get(e.entityId);
                if (st != null && e.timestamp < st.watermark) {
                    throw new IllegalStateException(
                            "预写日志内部顺序矛盾: 实体 " + e.entityId
                                    + " 重放到晚于水位的事件，日志已损坏");
                }
                applyTo(entities, e, matches);
                maxSeq = Math.max(maxSeq, e.seq);
            }
        }
        nextSeq = maxSeq + 1;
    }

    // ------------------------------------------------------------ 查询路径

    /** 匹配的确定性排序：实体 -> C 时间/序号 -> B -> A。 */
    private static final Comparator<Match> MATCH_ORDER =
            Comparator.comparing((Match m) -> m.entityId())
                    .thenComparingLong(m -> m.c.timestamp)
                    .thenComparingLong(m -> m.c.seq)
                    .thenComparingLong(m -> m.b.timestamp)
                    .thenComparingLong(m -> m.b.seq)
                    .thenComparingLong(m -> m.a.timestamp)
                    .thenComparingLong(m -> m.a.seq);

    /** 查询全部匹配，确定性排序。 */
    public synchronized List<Match> queryMatches() {
        List<Match> sorted = new ArrayList<>(matches);
        sorted.sort(MATCH_ORDER);
        return sorted;
    }

    /** 查询单个实体的匹配（不存在则空列表）。 */
    public synchronized List<Match> queryMatches(String entityId) {
        List<Match> out = new ArrayList<>();
        for (Match m : matches) {
            if (m.entityId().equals(entityId)) {
                out.add(m);
            }
        }
        out.sort(MATCH_ORDER);
        return out;
    }

    /** 某实体部分匹配状态的快照（给 /state 与测试用）。 */
    public synchronized EntityStateView viewState(String entityId) {
        EntityState st = entities.get(entityId);
        if (st == null) {
            return new EntityStateView(entityId, Long.MIN_VALUE,
                    new ArrayList<>(), new ArrayList<>());
        }
        List<Event> as = new ArrayList<>(st.as);
        List<ABView> abs = new ArrayList<>();
        for (AB ab : st.abs) {
            abs.add(new ABView(ab.a, ab.b));
        }
        return new EntityStateView(entityId, st.watermark, as, abs);
    }

    /** 全部实体的部分匹配状态快照。 */
    public synchronized List<EntityStateView> viewAllStates() {
        List<EntityStateView> out = new ArrayList<>();
        for (String entityId : entities.keySet()) {
            out.add(viewState(entityId));
        }
        return out;
    }

    /** 实体状态的只读视图。 */
    public static final class EntityStateView {
        public final String entityId;
        public final long watermark;
        public final List<Event> waitingAs;
        public final List<ABView> waitingABs;

        EntityStateView(String entityId, long watermark,
                        List<Event> waitingAs, List<ABView> waitingABs) {
            this.entityId = entityId;
            this.watermark = watermark;
            this.waitingAs = waitingAs;
            this.waitingABs = waitingABs;
        }
    }

    /** A->B 部分匹配的只读视图。 */
    public static final class ABView {
        public final Event a;
        public final Event b;

        ABView(Event a, Event b) {
            this.a = a;
            this.b = b;
        }
    }

    public synchronized long totalMatches() {
        return matches.size();
    }

    public synchronized long nextSeq() {
        return nextSeq;
    }

    /** 清空全部内存状态（实体、匹配、序号）。对应 POST /reset。 */
    public synchronized void resetAll() {
        entities.clear();
        matches.clear();
        nextSeq = 0;
    }
}
