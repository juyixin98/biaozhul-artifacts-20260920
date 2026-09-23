package cdcrebuild.engine;

import cdcrebuild.codec.Json;
import cdcrebuild.model.Event;
import cdcrebuild.model.ValidationException;

import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 变更流消费 / 状态重建引擎。
 *
 * 位置语义（pos 从 1 开始严格递增）：
 *   lastDurablePos ：已写入 WAL 并顺序应用的最大位置；nextExpected = lastDurablePos + 1
 *   lastVisiblePos ：已做出“可见性决定”的最大位置（COMMIT/ROLLBACK 发生处）。
 *                    未提交事务（BEGIN/DATA）虽已持久化，却不出现在表快照中。
 *
 * 投递规则：
 *   pos < nextExpected         ：重复投递，按内容规范化比对，一致则幂等返回（HTTP 200）
 *   pos == nextExpected        ：先做语义校验 → fsync 落 WAL → 改内存状态；随后尝试排空缓冲区
 *   pos > nextExpected         ：发现缺口，事件进入内存缓冲区并进入 PAUSED，
 *                                 绝不跳过缺口；只有 pos==nextExpected 的事件到达才能解除
 * 排空中若事件语义非法（如未 BEGIN 就 DATA）：剔除该事件、保持 PAUSED 并给出阻塞原因，
 * 等待客户端以同一 pos 重投修正后的事件。
 */
public final class CdcEngine implements AutoCloseable {

    /** 内存中保留的已应用事务记录条数上限（/v1/log 查询用）。 */
    private static final int LOG_CAPACITY = 10_000;

    public static final class IngestResult {
        public final long pos;
        public final String status;      // DURABLE | DUPLICATE | BUFFERED
        public final boolean visible;
        public final String state;       // RUNNING | PAUSED
        public final long nextExpectedPos;
        public final long lastVisiblePos;
        public final Long firstMissingPos;
        public final String gapError;

        IngestResult(long pos, String status, boolean visible, String state,
                     long nextExpectedPos, long lastVisiblePos, Long firstMissingPos, String gapError) {
            this.pos = pos;
            this.status = status;
            this.visible = visible;
            this.state = state;
            this.nextExpectedPos = nextExpectedPos;
            this.lastVisiblePos = lastVisiblePos;
            this.firstMissingPos = firstMissingPos;
            this.gapError = gapError;
        }

        public Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("pos", pos);
            m.put("status", status);
            m.put("visible", visible);
            m.put("state", state);
            m.put("nextExpectedPos", nextExpectedPos);
            m.put("lastVisiblePos", lastVisiblePos);
            m.put("firstMissingPos", firstMissingPos);
            m.put("gapError", gapError);
            return m;
        }
    }

    /** 一条已结束事务（提交或回滚），供 /v1/log 审计。 */
    public static final class TxnRecord {
        public final long beginPos;
        public final long endPos;
        public final String txId;
        public final boolean committed;
        public final List<Event> events;

        TxnRecord(long beginPos, long endPos, String txId, boolean committed, List<Event> events) {
            this.beginPos = beginPos;
            this.endPos = endPos;
            this.txId = txId;
            this.committed = committed;
            this.events = events;
        }
    }

    private static final class OpenTxn {
        final long beginPos;
        final List<Event> staged = new ArrayList<>();

        OpenTxn(long beginPos) {
            this.beginPos = beginPos;
        }
    }

    private final Path dataDir;
    private final String pkColumn;
    private final Wal wal;

    private long lastDurablePos = 0;
    private long highestSeenPos = 0;
    private long lastVisiblePos = 0;

    private final TreeMap<Long, Event> buffered = new TreeMap<>();
    private final Map<Long, String> durableCanonical = new TreeMap<>();
    private final Map<String, OpenTxn> openTxns = new LinkedHashMap<>();
    private final TreeMap<String, TreeMap<String, Map<String, Object>>> tables = new TreeMap<>();
    private final List<TxnRecord> txnLog = new ArrayList<>();

    private Long firstMissingPos = null;
    private String gapError = null;
    private boolean closed = false;

    private CdcEngine(Path dataDir, String pkColumn, Wal wal) {
        this.dataDir = dataDir;
        this.pkColumn = pkColumn;
        this.wal = wal;
    }

    /** 打开（或恢复）一个引擎：重放 WAL，未结束事务保持为 open，重现半事务状态。 */
    public static CdcEngine open(Path dataDir, String pkColumn) throws IOException {
        Path walPath = dataDir.resolve("events.wal");
        List<Map<String, Object>> replay = new ArrayList<>();
        Wal wal = Wal.openAndRecover(walPath, replay);
        CdcEngine engine = new CdcEngine(dataDir, pkColumn, wal);
        for (Map<String, Object> raw : replay) {
            Event event = Event.fromMap(raw);
            if (event.pos != engine.lastDurablePos + 1) {
                throw new IOException("WAL 位置不连续: 期望 "
                        + (engine.lastDurablePos + 1) + " 实际 " + event.pos);
            }
            engine.durableCanonical.put(event.pos, event.canonical());
            engine.mutate(event);
            engine.lastDurablePos = event.pos;
            engine.highestSeenPos = event.pos;
        }
        return engine;
    }

    // ------------------------------------------------------------ 摄入入口

    public synchronized IngestResult ingest(Event e) {
        if (closed) {
            throw new IllegalStateException("引擎已关闭");
        }
        highestSeenPos = Math.max(highestSeenPos, e.pos);

        // 1) 重复位置：必须与已持久化内容逐字节（规范化后）一致
        if (e.pos <= lastDurablePos) {
            String stored = durableCanonical.get(e.pos);
            if (stored == null || !stored.equals(e.canonical())) {
                throw new SemanticException("位置 " + e.pos
                        + " 已被另一条事件占用（重复投递内容不一致，拒绝覆盖）");
            }
            return new IngestResult(e.pos, "DUPLICATE", e.pos <= lastVisiblePos,
                    currentState(), lastDurablePos + 1, lastVisiblePos,
                    firstMissingPos, gapError);
        }

        // 2) 越过下一个期望位置 => 缺口（或缺口期间继续到达的更后事件）
        if (e.pos > lastDurablePos + 1) {
            Event existing = buffered.get(e.pos);
            if (existing != null) {
                if (!existing.canonical().equals(e.canonical())) {
                    throw new SemanticException("位置 " + e.pos
                            + " 在缓冲区中已有内容不同的事件，拒绝替换（请重发与首次一致的内容）");
                }
            } else {
                buffered.put(e.pos, e);
            }
            markGap("检测到位置缺口: 期望 " + (lastDurablePos + 1)
                    + "，先收到 " + e.pos + "；暂停消费，等待缺口事件补齐");
            return new IngestResult(e.pos, "BUFFERED", false, "PAUSED",
                    lastDurablePos + 1, lastVisiblePos, firstMissingPos, gapError);
        }

        // 3) 恰好是下一条：落入排空流程（它会继续吃掉缓冲区里的连续事件）
        buffered.put(e.pos, e);
        drainBuffer();

        boolean visible = e.pos <= lastVisiblePos;
        return new IngestResult(e.pos, "DURABLE", visible, currentState(),
                lastDurablePos + 1, lastVisiblePos, firstMissingPos, gapError);
    }

    /**
     * 从缓冲区顺序取出 nextExpected 事件：语义校验 → fsync 落盘 → 改状态。
     * 语义非法的事件不落盘、保留在缓冲区中并直接抛出 SemanticException（HTTP 409），
     * 消费停在该位置（PAUSED）；客户端以同一 pos 重投修正后的事件即可覆盖并恢复。
     */
    private void drainBuffer() {
        while (true) {
            long want = lastDurablePos + 1;
            Event e = buffered.get(want);
            if (e == null) {
                break;
            }
            try {
                check(e);
            } catch (RuntimeException bad) {
                // 语义冲突（409）或结构校验（400）都不落盘；事件保留在缓冲区，
                // 消费停在该位置（PAUSED），客户端以同一 pos 重投修正事件覆盖即可
                markGap("位置 " + want + " 事件校验失败，等待同位置重投修正: " + bad.getMessage());
                throw bad;
            }
            buffered.remove(want);
            try {
                wal.append(e.toMap());
            } catch (IOException ioe) {
                // 落盘失败按致命错误处理：状态未推进，重启后从 WAL 重建。
                throw new IllegalStateException("WAL 写入失败（位置 " + want + "）", ioe);
            }
            durableCanonical.put(e.pos, e.canonical());
            mutate(e);
            lastDurablePos = e.pos;
        }
        // 连续段全部排空：若无未来事件积压，消费恢复 RUNNING
        if (buffered.isEmpty()) {
            firstMissingPos = null;
            gapError = null;
        } else {
            markGap("位置缺口: 等待 " + (lastDurablePos + 1)
                    + "，缓冲区已有 " + buffered.firstKey() + ".." + buffered.lastKey());
            firstMissingPos = lastDurablePos + 1;
        }
    }

    private void markGap(String reason) {
        firstMissingPos = lastDurablePos + 1;
        gapError = reason;
    }

    private String currentState() {
        return paused() ? "PAUSED" : "RUNNING";
    }

    /** 缓冲区非空即暂停：既包括“未来事件待排空”，也包括“当前位置是非法事件等待重投”。 */
    private boolean paused() {
        return !buffered.isEmpty();
    }

    // ------------------------------------------------------------ 状态机

    /** 纯语义校验，不修改任何状态。 */
    private void check(Event e) {
        switch (e.type) {
            case BEGIN:
                if (openTxns.containsKey(e.txId)) {
                    throw new SemanticException("事务 " + e.txId + " 已 BEGIN，不能重复开始");
                }
                break;
            case DATA:
                requireTxn(e);
                validateDataShape(e);
                break;
            case COMMIT:
            case ROLLBACK:
                if (!openTxns.containsKey(e.txId)) {
                    throw new SemanticException("没有进行中的事务 " + e.txId
                            + "，不能 " + e.type);
                }
                break;
            default:
                throw new SemanticException("未知事件类型 " + e.type);
        }
    }

    private OpenTxn requireTxn(Event e) {
        OpenTxn tx = openTxns.get(e.txId);
        if (tx == null) {
            throw new SemanticException("DATA 事件属于未 BEGIN 的事务 " + e.txId);
        }
        return tx;
    }

    private void validateDataShape(Event e) {
        Map<String, Object> oldRow = e.oldRow;
        Map<String, Object> newRow = e.newRow;
        switch (e.op) {
            case INSERT:
                rowKey(newRow, "new");
                break;
            case DELETE:
                rowKey(oldRow, "old");
                break;
            case UPDATE: {
                String oldKey = rowKey(oldRow, "old");
                String newKey = rowKey(newRow, "new");
                boolean actuallyChanged = !oldKey.equals(newKey);
                if (actuallyChanged != e.pkChanged) {
                    throw new SemanticException("主键变更标记不一致: old/new 主键"
                            + (actuallyChanged ? "不同但 pkChanged=false" : "相同但 pkChanged=true"));
                }
                break;
            }
            default:
                throw new SemanticException("未知 op " + e.op);
        }
    }

    private String rowKey(Map<String, Object> row, String label) {
        if (row == null || !row.containsKey(pkColumn)) {
            throw new ValidationException("行 " + label + " 缺少主键列 '" + pkColumn + "'");
        }
        Object v = row.get(pkColumn);
        if (v == null) {
            throw new ValidationException("行 " + label + " 的主键不能为 null");
        }
        return Json.canonical(v);
    }

    /** 应用已校验、已落盘的事件到内存状态。 */
    private void mutate(Event e) {
        switch (e.type) {
            case BEGIN:
                openTxns.put(e.txId, new OpenTxn(e.pos));
                break;
            case DATA:
                openTxns.get(e.txId).staged.add(e);
                break;
            case COMMIT: {
                OpenTxn tx = openTxns.remove(e.txId);
                for (Event d : tx.staged) {
                    applyData(d);
                }
                appendRecord(new TxnRecord(tx.beginPos, e.pos, e.txId, true,
                        List.copyOf(tx.staged)));
                lastVisiblePos = e.pos;
                break;
            }
            case ROLLBACK: {
                OpenTxn tx = openTxns.remove(e.txId);
                appendRecord(new TxnRecord(tx.beginPos, e.pos, e.txId, false,
                        List.copyOf(tx.staged)));
                lastVisiblePos = e.pos;
                break;
            }
            default:
                throw new SemanticException("未知事件类型 " + e.type);
        }
    }

    private void applyData(Event e) {
        TreeMap<String, Map<String, Object>> rows =
                tables.computeIfAbsent(e.table, t -> new TreeMap<>());
        switch (e.op) {
            case INSERT: {
                String key = rowKey(e.newRow, "new");
                if (rows.containsKey(key)) {
                    throw new SemanticException("INSERT 违反主键唯一: 表 " + e.table
                            + " 键 " + key + " 已存在（位置 " + e.pos + "）");
                }
                rows.put(key, new LinkedHashMap<>(e.newRow));
                break;
            }
            case DELETE: {
                String key = rowKey(e.oldRow, "old");
                if (rows.remove(key) == null) {
                    throw new SemanticException("DELETE 目标不存在: 表 " + e.table
                            + " 键 " + key + "（位置 " + e.pos + "）");
                }
                break;
            }
            case UPDATE: {
                String oldKey = rowKey(e.oldRow, "old");
                String newKey = rowKey(e.newRow, "new");
                Map<String, Object> existing = rows.get(oldKey);
                if (existing == null) {
                    throw new SemanticException("UPDATE 目标不存在: 表 " + e.table
                            + " 键 " + oldKey + "（位置 " + e.pos + "）");
                }
                if (!oldKey.equals(newKey)) {
                    if (rows.containsKey(newKey)) {
                        throw new SemanticException("主键变更目标键已存在: 表 " + e.table
                                + " 键 " + newKey + "（位置 " + e.pos + "）");
                    }
                    rows.remove(oldKey);
                }
                rows.put(newKey, new LinkedHashMap<>(e.newRow));
                break;
            }
            default:
                throw new SemanticException("未知 op " + e.op);
        }
    }

    private void appendRecord(TxnRecord r) {
        txnLog.add(r);
        if (txnLog.size() > LOG_CAPACITY) {
            txnLog.remove(0);
        }
    }

    // ------------------------------------------------------------ 查询

    public synchronized Map<String, Object> status() {
        Map<String, Object> m = new LinkedHashMap<>();
        boolean paused = paused();
        m.put("state", paused ? "PAUSED" : "RUNNING");
        m.put("lastDurablePos", lastDurablePos);
        m.put("nextExpectedPos", lastDurablePos + 1);
        m.put("highestSeenPos", highestSeenPos);
        m.put("lastVisiblePos", lastVisiblePos);
        m.put("firstMissingPos", paused ? lastDurablePos + 1 : null);
        m.put("gapError", paused ? gapError : null);
        m.put("bufferedCount", buffered.size());
        m.put("openTxCount", openTxns.size());
        m.put("openTxIds", new ArrayList<>(openTxns.keySet()));
        m.put("walLengthBytes", wal.lengthBytes());
        Map<String, Integer> rowCounts = new LinkedHashMap<>();
        for (var en : tables.entrySet()) {
            rowCounts.put(en.getKey(), en.getValue().size());
        }
        m.put("tables", rowCounts);
        return m;
    }

    /** 深拷贝所有表的当前已提交快照（未提交事务的数据不可见）。 */
    public synchronized Map<String, Object> snapshot() {
        Map<String, Object> out = new LinkedHashMap<>();
        for (var en : tables.entrySet()) {
            List<Object> rows = new ArrayList<>();
            for (Map<String, Object> row : en.getValue().values()) {
                rows.add(new LinkedHashMap<>(row));
            }
            out.put(en.getKey(), rows);
        }
        return out;
    }

    /** 深拷贝单表快照；表不存在返回 null。 */
    public synchronized List<Map<String, Object>> tableSnapshot(String table) {
        TreeMap<String, Map<String, Object>> rows = tables.get(table);
        if (rows == null) {
            return null;
        }
        List<Map<String, Object>> out = new ArrayList<>();
        for (Map<String, Object> row : rows.values()) {
            out.add(new LinkedHashMap<>(row));
        }
        return Collections.unmodifiableList(out);
    }

    /** 以 JSON 文本形式给出主键值，如 1 或 "alice"，规范化后查行。找不到返回 null。 */
    public synchronized Map<String, Object> rowByKey(String table, String keyJson) {
        TreeMap<String, Map<String, Object>> rows = tables.get(table);
        if (rows == null) {
            return null;
        }
        String key;
        try {
            key = Json.canonical(Json.parse(keyJson));
        } catch (RuntimeException ex) {
            throw new ValidationException("key 必须是合法 JSON 值（如 1 或 \"alice\"）: " + ex.getMessage());
        }
        Map<String, Object> row = rows.get(key);
        return row == null ? null : new LinkedHashMap<>(row);
    }

    /** 查询结束位置（COMMIT/ROLLBACK 的 pos）落在 [fromPos, toPos] 的事务记录。 */
    public synchronized List<TxnRecord> queryLog(long fromPos, long toPos) {
        if (fromPos < 0 || toPos < fromPos) {
            throw new ValidationException("非法位置范围");
        }
        List<TxnRecord> out = new ArrayList<>();
        for (TxnRecord r : txnLog) {
            if (r.endPos >= fromPos && r.endPos <= toPos) {
                out.add(r);
            }
        }
        return out;
    }

    public synchronized Map<String, Object> recordToMap(TxnRecord r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("txId", r.txId);
        m.put("committed", r.committed);
        m.put("beginPos", r.beginPos);
        m.put("endPos", r.endPos);
        List<Object> evs = new ArrayList<>();
        for (Event e : r.events) {
            evs.add(e.toMap());
        }
        m.put("events", evs);
        return m;
    }

    public String pkColumn() {
        return pkColumn;
    }

    @Override
    public synchronized void close() {
        closed = true;
        try {
            wal.close();
        } catch (IOException e) {
            throw new IllegalStateException("关闭 WAL 失败: " + e, e);
        }
    }
}
