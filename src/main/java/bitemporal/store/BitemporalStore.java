package bitemporal.store;

import bitemporal.error.OverlapRejectedException;
import bitemporal.error.RecordNotFoundException;
import bitemporal.error.ValidationException;
import bitemporal.model.ChangeRequest;
import bitemporal.model.CommitResult;
import bitemporal.model.Interval;
import bitemporal.model.QueryRequest;
import bitemporal.model.TemporalRecord;
import bitemporal.model.TransactionRequest;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * 双时态记录存储（纯内存、线程安全、无外部数据库）。
 *
 * <p>每个记录标识对应一组不可变版本行 {@link TemporalRecord}，每行带有
 * 业务有效时间与系统记录时间两条半开时间轴。修订采用“封口旧行 + 写入新行”，
 * 历史永不被覆盖或删除。</p>
 *
 * <p>写入以事务为单位：先在私有暂存副本上顺序应用全部变更，任一变更被拒绝则
 * 整体异常、库内状态不变；全部成功后才一次性发布。</p>
 */
public class BitemporalStore {

    private final Map<String, List<TemporalRecord>> records = new HashMap<>();
    private Instant lastCommitAt;

    // ============================= 写入 =============================

    /**
     * 原子提交一个事务。
     *
     * @throws ValidationException        字段缺失、时间非法或提交时刻不晚于上次提交
     * @throws OverlapRejectedException   insert（或同事务变更）与当前版本业务时间重叠
     * @throws RecordNotFoundException    revise 在目标区间内找不到当前版本
     */
    public synchronized CommitResult commit(TransactionRequest request) {
        if (request == null || request.changes() == null || request.changes().isEmpty()) {
            throw new ValidationException("transaction must contain at least one change");
        }

        Instant systemAt = request.committedAt() != null ? request.committedAt() : Instant.now();
        if (lastCommitAt != null && !systemAt.isAfter(lastCommitAt)) {
            throw new ValidationException(
                    "committedAt " + systemAt + " must be strictly after the previous commit at "
                            + lastCommitAt + " (half-open, system time is append-only)");
        }
        String txnId = (request.txnId() == null || request.txnId().isBlank())
                ? "txn-" + System.nanoTime()
                : request.txnId();

        // 私有暂存：浅复制每个版本列表（版本行本身不可变），失败时直接丢弃暂存。
        Map<String, List<TemporalRecord>> staged = new HashMap<>();
        records.forEach((id, rows) -> staged.put(id, new ArrayList<>(rows)));

        List<TemporalRecord> written = new ArrayList<>();
        for (ChangeRequest change : request.changes()) {
            applyChange(staged, change, systemAt, txnId, written);
        }

        records.clear();
        records.putAll(staged);
        lastCommitAt = systemAt;
        return new CommitResult(txnId, systemAt, List.copyOf(written));
    }

    private void applyChange(Map<String, List<TemporalRecord>> staged,
                             ChangeRequest change,
                             Instant systemAt,
                             String txnId,
                             List<TemporalRecord> written) {
        validateChange(change);
        switch (change.normalizedOp()) {
            case "insert" -> doInsert(staged, change, systemAt, txnId, written);
            case "revise" -> doRevise(staged, change, systemAt, txnId, written);
            default -> throw new ValidationException(
                    "unsupported op '" + change.op() + "', expected 'insert' or 'revise'");
        }
    }

    private void doInsert(Map<String, List<TemporalRecord>> staged,
                          ChangeRequest change,
                          Instant systemAt,
                          String txnId,
                          List<TemporalRecord> written) {
        Interval target = new Interval(change.validFrom(), change.validTo());
        List<TemporalRecord> current = currentRows(staged, change.recordId());
        for (TemporalRecord row : current) {
            if (row.validInterval().overlaps(target)) {
                throw new OverlapRejectedException(change.recordId(),
                        "insert rejected for record '" + change.recordId()
                                + "': valid interval " + format(target)
                                + " overlaps current version " + row.versionId()
                                + " on " + format(row.validInterval())
                                + " (half-open; adjacent intervals are allowed)");
            }
        }
        TemporalRecord row = new TemporalRecord(
                change.recordId(),
                nextVersionId(staged, change.recordId()),
                change.data(),
                target.from(), target.to(),
                systemAt, null, txnId);
        staged.computeIfAbsent(change.recordId(), k -> new ArrayList<>()).add(row);
        written.add(row);
    }

    /**
     * 追溯修订：用新值覆盖目标业务区间 [vf, vt)。
     *
     * <p>与目标相交的所有当前版本行在系统时间 {@code systemAt} 被封口；它们在目标
     * 之外的左右残段原样保留，目标区间写入一行新值。要求当前版本恰好铺满目标
     * 区间（中间不得有业务时间空隙），否则拒绝。</p>
     */
    private void doRevise(Map<String, List<TemporalRecord>> staged,
                          ChangeRequest change,
                          Instant systemAt,
                          String txnId,
                          List<TemporalRecord> written) {
        String recordId = change.recordId();
        Interval target = new Interval(change.validFrom(), change.validTo());

        List<TemporalRecord> current = currentRows(staged, recordId).stream()
                .sorted(Comparator.comparing(TemporalRecord::validFrom))
                .toList();
        List<TemporalRecord> touched = new ArrayList<>();
        Instant coverage = target.from();
        for (TemporalRecord row : current) {
            if (!row.validInterval().overlaps(target)) {
                continue;
            }
            if (row.validFrom().isAfter(coverage)) {
                // 当前版本与已覆盖区域之间出现空隙：没有事实可修订。
                throw new ValidationException(
                        "revise rejected for record '" + recordId + "': target " + format(target)
                                + " is not fully covered by current versions; uncovered gap at "
                                + coverage);
            }
            touched.add(row);
            if (Interval.endOrMax(row.validTo()).isAfter(coverage)) {
                coverage = Interval.endOrMax(row.validTo());
            }
            if (!coverage.isBefore(Interval.endOrMax(target.to()))) {
                break;
            }
        }
        if (touched.isEmpty()) {
            throw new RecordNotFoundException(
                    "revise rejected for record '" + recordId + "': no current version covers "
                            + format(target));
        }
        if (coverage.isBefore(Interval.endOrMax(target.to()))) {
            throw new ValidationException(
                    "revise rejected for record '" + recordId + "': target " + format(target)
                            + " extends past current knowledge (covered only to " + coverage + ")");
        }

        // 1) 封口所有受影响的当前版本行（用不可变副本替换）。
        //    若某行与本次提交有相同的 systemFrom（同事务内刚插入即被修订），
        //    它从未在任何系统观察时刻可见，封口会产生 [t,t) 零长度区间，
        //    因此直接丢弃而不是保留。
        List<TemporalRecord> list = staged.get(recordId);
        for (TemporalRecord old : touched) {
            if (old.systemFrom().equals(systemAt)) {
                list.remove(old);
                // 同事务内先前变更刚写入、尚未提交的行也记录在 written 中，一并撤回，
                // 保证提交结果只包含真正落库的行。
                written.remove(old);
            } else {
                CollectionsReplace(list, old, old.withSystemTo(systemAt));
            }
        }

        // 2) 目标之外的左/右残段保留原值；目标区间写入新值。全部为系统时间新行。
        for (TemporalRecord old : touched) {
            Instant a = old.validFrom();
            Instant b = old.validTo();
            if (a.isBefore(target.from())) {
                addNewVersion(staged, written, recordId, old.data(),
                        a, target.from(), systemAt, txnId);
            }
            if (target.to() != null
                    && (b == null || b.isAfter(target.to()))) {
                addNewVersion(staged, written, recordId, old.data(),
                        target.to(), b, systemAt, txnId);
            }
        }
        addNewVersion(staged, written, recordId, change.data(),
                target.from(), target.to(), systemAt, txnId);
    }

    // ============================= 查询 =============================

    /**
     * as-of 双维查询：返回在系统观察时刻 {@code systemAt} 被相信、且描述业务观察
     * 时刻 {@code validAt} 的版本行。
     */
    public synchronized List<TemporalRecord> asOf(QueryRequest query) {
        QueryRequest.Resolved at = query.resolve();
        List<TemporalRecord> hits = new ArrayList<>();
        for (Map.Entry<String, List<TemporalRecord>> entry : records.entrySet()) {
            if (query.recordId() != null && !query.recordId().equals(entry.getKey())) {
                continue;
            }
            for (TemporalRecord row : entry.getValue()) {
                if (row.systemInterval().contains(at.systemAt())
                        && row.validInterval().contains(at.validAt())) {
                    hits.add(row);
                }
            }
        }
        hits.sort(Comparator.comparing(TemporalRecord::recordId)
                .thenComparing(TemporalRecord::validFrom)
                .thenComparing(TemporalRecord::versionId));
        return List.copyOf(hits);
    }

    /** 某条记录的全部版本行（含已封口历史），按版本号排序。 */
    public synchronized List<TemporalRecord> history(String recordId) {
        List<TemporalRecord> rows = records.getOrDefault(recordId, List.of()).stream()
                .sorted(Comparator.comparingLong(TemporalRecord::versionId))
                .toList();
        return rows;
    }

    public synchronized List<String> recordIds() {
        return records.keySet().stream().sorted().toList();
    }

    public synchronized Optional<Instant> lastCommitAt() {
        return Optional.ofNullable(lastCommitAt);
    }

    /** 清空全部数据（用于 reset 命令与固定种子重装）。 */
    public synchronized void reset() {
        records.clear();
        lastCommitAt = null;
    }

    /** 全部版本行的扁平快照（含已封口历史），供持久化使用。 */
    protected synchronized List<TemporalRecord> allRows() {
        List<TemporalRecord> all = new ArrayList<>();
        records.values().forEach(all::addAll);
        all.sort(Comparator.comparing(TemporalRecord::recordId)
                .thenComparingLong(TemporalRecord::versionId));
        return List.copyOf(all);
    }

    /** 用快照内容整体替换库内状态，供进程启动时恢复使用。 */
    protected synchronized void loadSnapshot(List<TemporalRecord> rows, Instant lastCommit) {
        records.clear();
        for (TemporalRecord row : rows) {
            records.computeIfAbsent(row.recordId(), k -> new ArrayList<>()).add(row);
        }
        lastCommitAt = lastCommit;
    }

    // ============================= 内部工具 =============================

    private void validateChange(ChangeRequest change) {
        if (change == null) {
            throw new ValidationException("change must not be null");
        }
        if (change.recordId() == null || change.recordId().isBlank()) {
            throw new ValidationException("change.recordId must not be blank");
        }
        if (change.data() == null) {
            throw new ValidationException(
                    "change.data must not be null for record '" + change.recordId() + "'");
        }
        if (change.validFrom() == null) {
            throw new ValidationException(
                    "change.validFrom must not be null for record '" + change.recordId() + "'");
        }
        if (change.validTo() != null && !change.validTo().isAfter(change.validFrom())) {
            throw new ValidationException(
                    "change for record '" + change.recordId()
                            + "': validTo must be after validFrom (half-open interval)");
        }
    }

    /** 取在当前系统时间仍有效的版本行（systemTo 为空）。 */
    private List<TemporalRecord> currentRows(Map<String, List<TemporalRecord>> view, String recordId) {
        List<TemporalRecord> rows = view.get(recordId);
        if (rows == null) {
            return List.of();
        }
        return rows.stream().filter(r -> r.systemTo() == null).toList();
    }

    private long nextVersionId(Map<String, List<TemporalRecord>> view, String recordId) {
        List<TemporalRecord> rows = view.get(recordId);
        if (rows == null) {
            return 1L;
        }
        return rows.stream().mapToLong(TemporalRecord::versionId).max().orElse(0L) + 1L;
    }

    private void addNewVersion(Map<String, List<TemporalRecord>> staged,
                               List<TemporalRecord> written,
                               String recordId,
                               String data,
                               Instant validFrom,
                               Instant validTo,
                               Instant systemAt,
                               String txnId) {
        TemporalRecord row = new TemporalRecord(
                recordId, nextVersionId(staged, recordId), data,
                validFrom, validTo, systemAt, null, txnId);
        staged.get(recordId).add(row);
        written.add(row);
    }

    private static void CollectionsReplace(List<TemporalRecord> list,
                                           TemporalRecord oldRow,
                                           TemporalRecord newRow) {
        int index = list.indexOf(oldRow);
        if (index < 0) {
            throw new IllegalStateException("internal error: staged row missing");
        }
        list.set(index, newRow);
    }

    private static String format(Interval interval) {
        return "[" + interval.from() + ", " + (interval.to() != null ? interval.to() : "+∞") + ")";
    }
}
