package com.example.cdc;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.NavigableMap;
import java.util.TreeMap;
import java.util.concurrent.locks.ReentrantLock;

/**
 * 变更日志消费服务：把带事务边界的变更日志重建成表状态。
 *
 * <p>语义保证：
 * <ol>
 *   <li><b>事务边界 / 提交前不可见</b>：DATA 只按 txn 缓存，COMMIT 才整体生效；
 *       ROLLBACK 丢弃。查询只看到已提交数据。</li>
 *   <li><b>源位置幂等</b>：position 是全局唯一、连续递增的逻辑位置。
 *       position &lt;= 已消费水位的事件直接视为重复，原样忽略（不报错、不重复生效）。</li>
 *   <li><b>缺口暂停而非跳过</b>：只接受“紧邻水位的下一个位置”。出现更大位置时进入
 *       PAUSED，后续（即使更大）都进滞留区等待；补齐缺口后自动恢复 RUNNING。</li>
 *   <li><b>崩溃恢复</b>：每个事件先 {@code WAL.append + fsync} 再改内存；重启时重放
 *       WAL，未 COMMIT 的事务自然仍是 OPEN（半事务），行为与崩溃前一致。</li>
 * </ol>
 *
 * <p>所有公开方法在同一把锁上串行执行（单线程 HTTP executor 下实际无竞争）。
 */
public final class Engine {

    public static final String RUNNING = "RUNNING";
    public static final String PAUSED = "PAUSED";

    /** 未知事务（没有先收到 DATA 就 COMMIT/ROLLBACK 等）。 */
    public static final class ProtocolException extends RuntimeException {
        public ProtocolException(String msg) {
            super(msg);
        }
    }

    /** 一次 HTTP 投递的处理结果。 */
    public static final class IngestResult {
        public int accepted;
        public int duplicates;
        public int buffered;
        public boolean paused;
        public long watermark;
        public List<String> notes = new ArrayList<>();

        public Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("accepted", accepted);
            m.put("duplicates", duplicates);
            m.put("bufferedInGap", buffered);
            m.put("state", paused ? PAUSED : RUNNING);
            m.put("watermark", watermark);
            m.put("notes", notes);
            return m;
        }
    }

    /** 一个打开中的事务，按到达顺序保存其 DATA。 */
    private static final class Txn {
        final List<Event> events = new ArrayList<>();
    }

    private final Wal wal;
    private final ReentrantLock lock = new ReentrantLock();

    /** 已完整消费（含重复判定）的最高位置；下一个期望位置 = watermark + 1。 */
    private long watermark = 0;
    private final Map<String, Txn> openTxns = new LinkedHashMap<>();
    /** 表 -> 主键规范化串 -> 行（列值，含主键列）。 */
    private final Map<String, LinkedHashMap<String, Map<String, Object>>> tables = new LinkedHashMap<>();
    /** 缺口滞留区：position > expected 的事件，按位置排序。 */
    private final NavigableMap<Long, Event> pending = new TreeMap<>();
    /** 已提交变更台账（含表、主键、旧/新值、源位置），重放时重建。 */
    private final List<Map<String, Object>> ledger = new ArrayList<>();
    private boolean paused = false;

    public Engine(Wal wal) {
        this.wal = wal;
    }

    /** 启动：重放 WAL 重建内存状态。 */
    public void recover() {
        lock.lock();
        try {
            watermark = 0;
            openTxns.clear();
            tables.clear();
            pending.clear();
            ledger.clear();
            paused = false;
            // WAL 按“到达顺序”追加：缺口期间靠后的位置可能先落盘
            // （例如物理行 1,2,5,3,4）。position 是逻辑全序，因此重放前先按位置排序，
            // 再解释：连续前缀正式应用（含未结束事务=半事务）；
            // 仍大于水位+1 的（崩溃前滞留区事件）重建进 pending 并恢复 PAUSED。
            List<Event> logged = Wal.readAll(wal.path());
            logged.sort(java.util.Comparator.comparingLong(ev -> ev.position));
            for (Event e : logged) {
                if (e.position == watermark + 1) {
                    lifecycle(e);
                    watermark = e.position;
                } else if (e.position > watermark + 1) {
                    pending.put(e.position, e);
                    paused = true;
                }
                // position <= watermark 不应出现（去重在写 WAL 前），防御性忽略
            }
        } finally {
            lock.unlock();
        }
    }

    /**
     * 投递一批事件（按数组顺序逐条处理）。每条事件先 {@code WAL.append + fsync} 再改内存：
     * <ul>
     *   <li>position &le; 水位或已在滞留区 → 重复，幂等忽略；</li>
     *   <li>紧邻期望位置 → 先校验事务协议（如 COMMIT 未知事务会在落盘前拒绝），
     *       再持久化、应用，并尝试连续排空滞留区；</li>
     *   <li>大于期望位置 → 持久化后进滞留区，状态置 PAUSED。</li>
     * </ul>
     * 非法事件在写入 WAL 前抛出 {@link ProtocolException}，此前各条均已独立落盘生效。
     */
    public IngestResult ingest(List<Event> events) {
        IngestResult r = new IngestResult();
        lock.lock();
        try {
            long before = watermark;
            for (Event e : events) {
                if (e.position <= watermark) {
                    r.duplicates++;
                    r.notes.add("位置 " + e.position + " 已消费，按重复忽略");
                    continue;
                }
                if (pending.containsKey(e.position)) {
                    r.duplicates++;
                    r.notes.add("位置 " + e.position + " 已在滞留区，按重复忽略");
                    continue;
                }
                long expected = watermark + 1;
                if (e.position == expected) {
                    // 落盘前做事务边界校验，避免非法事件污染只能追加的 WAL
                    validateLifecycle(e);
                    wal.append(e.rawJson);
                    lifecycle(e);
                    watermark = e.position;
                    drainPending();
                } else {
                    // 缺口：事件仍然持久化，但暂停消费、绝不跳过缺口
                    wal.append(e.rawJson);
                    pending.put(e.position, e);
                    paused = true;
                }
            }
            r.accepted = (int) (watermark - before);
            r.buffered = pending.size();
            r.paused = paused;
            r.watermark = watermark;
            if (paused) {
                r.notes.add("检测到位置缺口：等待 " + (watermark + 1)
                        + "，暂停消费（不跳过）；滞留 " + pending.size() + " 条");
            }
            return r;
        } finally {
            lock.unlock();
        }
    }

    /** COMMIT/ROLLBACK 必须对应一个打开中的事务；DATA 总是合法。 */
    private void validateLifecycle(Event e) {
        if ((e.type.equals(Event.COMMIT) || e.type.equals(Event.ROLLBACK))
                && !openTxns.containsKey(e.txn)) {
            throw new ProtocolException("位置 " + e.position + ": " + e.type
                    + " 了未知或已结束的事务 '" + e.txn + "'");
        }
    }

    /** 应用紧邻位置后，尝试连续排空滞留区。 */
    private void drainPending() {
        while (true) {
            Map.Entry<Long, Event> next = pending.pollFirstEntry();
            if (next == null) {
                paused = false;
                return;
            }
            if (next.getKey() != watermark + 1) {
                pending.put(next.getKey(), next.getValue()); // 仍然缺，放回去
                paused = true;
                return;
            }
            lifecycle(next.getValue());
            watermark = next.getKey();
        }
    }

    /** 处理事件的事务语义（不含位置推进）。 */
    private void lifecycle(Event e) {
        switch (e.type) {
            case Event.DATA: {
                openTxns.computeIfAbsent(e.txn, k -> new Txn()).events.add(e);
                break;
            }
            case Event.COMMIT: {
                Txn t = openTxns.remove(e.txn);
                if (t == null) {
                    throw new ProtocolException(
                            "位置 " + e.position + ": COMMIT 了未知或已结束的事务 '" + e.txn + "'");
                }
                for (Event d : t.events) {
                    applyRowOp(d, e.position);
                }
                break;
            }
            case Event.ROLLBACK: {
                Txn t = openTxns.remove(e.txn);
                if (t == null) {
                    throw new ProtocolException(
                            "位置 " + e.position + ": ROLLBACK 了未知或已结束的事务 '" + e.txn + "'");
                }
                // 丢弃缓存的 DATA，不留任何痕迹
                break;
            }
            default:
                throw new ProtocolException("未知事件类型: " + e.type);
        }
    }

    /**
     * 把一条 DATA 应用到表状态，并追加台账。commitPos 为所属事务 COMMIT 的源位置，
     * 台账记录“何时变得可见”。
     */
    private void applyRowOp(Event d, long commitPos) {
        LinkedHashMap<String, Map<String, Object>> table =
                tables.computeIfAbsent(d.table, k -> new LinkedHashMap<>());

        List<Object> effectiveOldPk = d.oldPk != null ? d.oldPk : d.pk;
        Map<String, Object> before = table.get(Event.keyOf(effectiveOldPk));

        Map<String, Object> after;
        switch (d.op) {
            case Event.INSERT: {
                after = new LinkedHashMap<>();
                if (d.newValues != null) {
                    after.putAll(d.newValues);
                }
                table.put(Event.keyOf(d.pk), after);
                break;
            }
            case Event.UPDATE: {
                // 改主键 = 删旧键 + 写新键（同一事务内原子完成）
                if (d.oldPk != null) {
                    table.remove(Event.keyOf(d.oldPk));
                }
                after = new LinkedHashMap<>();
                Map<String, Object> current = table.get(Event.keyOf(d.pk));
                if (current != null) {
                    after.putAll(current);
                }
                if (d.newValues != null) {
                    after.putAll(d.newValues);
                }
                table.put(Event.keyOf(d.pk), after);
                break;
            }
            case Event.DELETE: {
                table.remove(Event.keyOf(d.pk));
                after = null;
                break;
            }
            default:
                throw new ProtocolException("未知 op: " + d.op);
        }

        Map<String, Object> rec = new LinkedHashMap<>();
        rec.put("commitPosition", commitPos);
        rec.put("dataPosition", d.position);
        rec.put("txn", d.txn);
        rec.put("table", d.table);
        rec.put("op", d.op);
        rec.put("oldPk", d.oldPk != null ? d.oldPk : null);
        rec.put("pk", d.pk);
        rec.put("oldValues", d.oldValues != null ? d.oldValues : (before != null ? before : null));
        rec.put("newValues", after);
        ledger.add(rec);
    }

    // ---------- 查询 ----------

    public Map<String, Object> status() {
        lock.lock();
        try {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("state", paused ? PAUSED : RUNNING);
            m.put("watermark", watermark);
            m.put("nextExpected", watermark + 1);
            m.put("openTransactions", new ArrayList<>(openTxns.keySet()));
            m.put("bufferedPositions", new ArrayList<>(pending.navigableKeySet()));
            m.put("bufferedCount", pending.size());
            return m;
        } finally {
            lock.unlock();
        }
    }

    public Map<String, Object> snapshot() {
        lock.lock();
        try {
            Map<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<String, LinkedHashMap<String, Map<String, Object>>> t : tables.entrySet()) {
                List<Object> rows = new ArrayList<>();
                for (Map<String, Object> row : t.getValue().values()) {
                    rows.add(new LinkedHashMap<>(row));
                }
                out.put(t.getKey(), rows);
            }
            return out;
        } finally {
            lock.unlock();
        }
    }

    public List<Map<String, Object>> ledger(String table) {
        lock.lock();
        try {
            List<Map<String, Object>> out = new ArrayList<>();
            for (Map<String, Object> rec : ledger) {
                if (table == null || table.equals(rec.get("table"))) {
                    out.add(new LinkedHashMap<>(rec));
                }
            }
            return out;
        } finally {
            lock.unlock();
        }
    }

    public Map<String, Object> openTxn(String txn) {
        lock.lock();
        try {
            Txn t = openTxns.get(txn);
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("txn", txn);
            m.put("open", t != null);
            m.put("dataCount", t == null ? 0 : t.events.size());
            if (t != null) {
                List<Object> evs = new ArrayList<>();
                for (Event e : t.events) {
                    Map<String, Object> em = new LinkedHashMap<>();
                    em.put("position", e.position);
                    em.put("table", e.table);
                    em.put("op", e.op);
                    em.put("pk", e.pk);
                    evs.add(em);
                }
                m.put("events", evs);
            }
            return m;
        } finally {
            lock.unlock();
        }
    }

    /** 清空全部状态与 WAL（/reset）。 */
    public void reset() {
        lock.lock();
        try {
            wal.reset();
            watermark = 0;
            openTxns.clear();
            tables.clear();
            pending.clear();
            ledger.clear();
            paused = false;
        } finally {
            lock.unlock();
        }
    }

    // ---------- 源事务解释器（参考实现） ----------

    /**
     * “源事务解释器”参考实现：按源日志独立解释一遍完整事件序列，
     * 返回每张表应当存在的行。消费器重放后的表状态应与此完全一致。
     *
     * <p>要求位置严格 1..N 连续（滞留区未排空时不调用）。
     */
    public static Map<String, LinkedHashMap<String, Map<String, Object>>> interpret(
            List<Event> log) {
        long expect = 1;
        Map<String, Txn> txns = new LinkedHashMap<>();
        Map<String, LinkedHashMap<String, Map<String, Object>>> result = new LinkedHashMap<>();
        for (Event e : log) {
            if (e.position != expect) {
                throw new ProtocolException("源日志不连续：期望 " + expect + "，实际 " + e.position);
            }
            expect++;
            switch (e.type) {
                case Event.DATA:
                    txns.computeIfAbsent(e.txn, k -> new Txn()).events.add(e);
                    break;
                case Event.COMMIT: {
                    Txn t = txns.remove(e.txn);
                    if (t == null) {
                        throw new ProtocolException("COMMIT 未知事务 " + e.txn);
                    }
                    for (Event d : t.events) {
                        applyRowOpStatic(result, d);
                    }
                    break;
                }
                case Event.ROLLBACK: {
                    if (txns.remove(e.txn) == null) {
                        throw new ProtocolException("ROLLBACK 未知事务 " + e.txn);
                    }
                    break;
                }
                default:
                    throw new ProtocolException("未知事件类型 " + e.type);
            }
        }
        return result;
    }

    private static void applyRowOpStatic(
            Map<String, LinkedHashMap<String, Map<String, Object>>> result, Event d) {
        LinkedHashMap<String, Map<String, Object>> table =
                result.computeIfAbsent(d.table, k -> new LinkedHashMap<>());
        List<Object> effectiveOldPk = d.oldPk != null ? d.oldPk : d.pk;
        switch (d.op) {
            case Event.INSERT: {
                Map<String, Object> row = new LinkedHashMap<>();
                if (d.newValues != null) {
                    row.putAll(d.newValues);
                }
                table.put(Event.keyOf(d.pk), row);
                break;
            }
            case Event.UPDATE: {
                if (d.oldPk != null) {
                    table.remove(Event.keyOf(d.oldPk));
                }
                Map<String, Object> row = new LinkedHashMap<>();
                Map<String, Object> cur = table.get(Event.keyOf(d.pk));
                if (cur != null) {
                    row.putAll(cur);
                }
                if (d.newValues != null) {
                    row.putAll(d.newValues);
                }
                table.put(Event.keyOf(d.pk), row);
                break;
            }
            case Event.DELETE:
                table.remove(Event.keyOf(d.pk));
                break;
            default:
                throw new ProtocolException("未知 op " + d.op);
        }
    }

    /**
     * 对账：用 WAL 全量事件跑源事务解释器，与当前表状态做深比较。
     * 返回 {match, differences:[...]}。
     */
    public Map<String, Object> reconcile() {
        lock.lock();
        try {
            List<Event> log = Wal.readAll(wal.path());
            // WAL 按到达顺序追加，缺口期间可能乱序；按逻辑位置排序后交解释器
            log.sort(java.util.Comparator.comparingLong(ev -> ev.position));
            Map<String, Object> expectedView = new LinkedHashMap<>();
            Map<String, LinkedHashMap<String, Map<String, Object>>> expected = interpret(log);
            for (Map.Entry<String, LinkedHashMap<String, Map<String, Object>>> t : expected.entrySet()) {
                List<Object> rows = new ArrayList<>();
                for (Map<String, Object> row : t.getValue().values()) {
                    rows.add(new LinkedHashMap<>(row));
                }
                expectedView.put(t.getKey(), rows);
            }
            Map<String, Object> actualView = snapshot();
            List<String> diffs = new ArrayList<>();
            java.util.Set<String> allTables = new java.util.LinkedHashSet<>();
            allTables.addAll(expectedView.keySet());
            allTables.addAll(actualView.keySet());
            for (String t : allTables) {
                String a = Json.write(expectedView.getOrDefault(t, new ArrayList<>()));
                String b = Json.write(actualView.getOrDefault(t, new ArrayList<>()));
                if (!a.equals(b)) {
                    diffs.add("表 " + t + " 不一致:\n  解释器=" + a + "\n  消费器=" + b);
                }
            }
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("match", diffs.isEmpty());
            out.put("sourceEventCount", log.size());
            out.put("watermark", watermark);
            out.put("differences", diffs);
            return out;
        } finally {
            lock.unlock();
        }
    }
}
