package com.example.bitemporal.engine;

import com.example.bitemporal.model.BitemporalRecord;
import com.example.bitemporal.model.ChangeRequest;
import com.example.bitemporal.model.Interval;
import com.example.bitemporal.model.WriteMode;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.stream.Collectors;

/**
 * 双时态表：只追加（append-only）存储与双时态规则计算。
 *
 * <ul>
 *   <li>业务有效时间（valid time）：事实在现实中生效的日期区间；</li>
 *   <li>系统记录时间（recorded/transaction time）：该版本被数据库知晓的日期区间。</li>
 * </ul>
 *
 * <p>所有区间均为半开 {@code [from, to)}。修订永不更新或删除旧行：
 * 旧当前行的记录时间在修订事务时刻被关闭并原样保留，新行携带新的记录区间，
 * 因此任意历史观察时刻的查询结果都可重现。
 */
public class BitemporalStore {

    private final List<BitemporalRecord> rows = new ArrayList<>();
    private final List<CommitLogEntry> commitLog = new ArrayList<>();
    private long nextRowId = 1;
    private LocalDate lastCommitDate;

    /** 不可变快照，供测试与序列化使用。 */
    public synchronized List<BitemporalRecord> snapshot() {
        return List.copyOf(rows);
    }

    public synchronized List<CommitLogEntry> commitLog() {
        return List.copyOf(commitLog);
    }

    /**
     * 双维 as-of 查询：在观察时刻 {@code observationDate}（系统时间维），
     * 业务日期 {@code businessDate}（有效时间维）该实体的记录是什么。
     *
     * <p>半开边界：记录时间 {@code [rf, rt)} 要求 {@code rf <= obs < rt}；
     * 有效时间 {@code [vf, vt)} 要求 {@code vf <= biz < vt}。
     * 正确维护的数据至多命中一条。
     */
    public synchronized Optional<BitemporalRecord> asOf(
            String entityId, LocalDate businessDate, LocalDate observationDate) {
        require(entityId != null && !entityId.isBlank(), "entityId is required");
        require(businessDate != null, "businessDate is required");
        require(observationDate != null, "observationDate is required");
        return rows.stream()
                .filter(r -> r.entityId().equals(entityId))
                .filter(r -> r.isCurrentAt(observationDate))
                .filter(r -> r.isValidAt(businessDate))
                .reduce((a, b) -> {
                    throw new BitemporalException(
                            "invariant violated: two current records for entity "
                                    + entityId + " at business date " + businessDate
                                    + " observed " + observationDate + " (rows "
                                    + a.rowId() + ", " + b.rowId() + ")");
                });
    }

    /** 同一观察时刻、全部实体在某业务日期的 as-of 结果。 */
    public synchronized List<BitemporalRecord> asOfAll(
            LocalDate businessDate, LocalDate observationDate) {
        require(businessDate != null, "businessDate is required");
        require(observationDate != null, "observationDate is required");
        return rows.stream()
                .filter(r -> r.isCurrentAt(observationDate))
                .filter(r -> r.isValidAt(businessDate))
                .sorted(Comparator.comparing(BitemporalRecord::entityId))
                .toList();
    }

    /** 在指定观察时刻看到的某实体完整有效时间历史（按有效起点排序）。 */
    public synchronized List<BitemporalRecord> history(String entityId, LocalDate observationDate) {
        require(entityId != null && !entityId.isBlank(), "entityId is required");
        require(observationDate != null, "observationDate is required");
        return rows.stream()
                .filter(r -> r.entityId().equals(entityId))
                .filter(r -> r.isCurrentAt(observationDate))
                .sorted(Comparator.comparing(r -> r.valid().from()))
                .toList();
    }

    /**
     * 在一个事务内原子提交多笔变更：所有变更共享同一个事务时间戳。
     * 先做全部校验（任一不通过则整笔事务不落库），再统一落库。
     *
     * <p>同实体的多笔 CORRECTION 按目标区间并集一次性扣除后重述，
     * 不产生仅在事务内短暂存活的中间残余行。
     *
     * @return 本事务新写入的物理行（含拆分续存行与新事实行）
     */
    public synchronized CommitResult commit(LocalDate transactionDate, List<ChangeRequest> requests) {
        require(transactionDate != null, "transactionDate is required");
        require(requests != null && !requests.isEmpty(), "requests must not be empty");
        if (lastCommitDate != null && !transactionDate.isAfter(lastCommitDate)) {
            throw new BitemporalException(
                    "transaction time must advance: requested " + transactionDate
                            + ", last commit was " + lastCommitDate);
        }
        validateRequests(requests);
        validateIntraTransaction(requests);
        requests.stream().filter(r -> r.mode() == WriteMode.INSERT).forEach(this::validateInsert);

        List<BitemporalRecord> snapshotBefore = List.copyOf(rows);
        try {
            List<BitemporalRecord> written = new ArrayList<>();
            for (ChangeRequest req : requests.stream().filter(r -> r.mode() == WriteMode.INSERT).toList()) {
                written.add(insertRow(req, Interval.openEnded(transactionDate)));
            }
            Map<String, List<ChangeRequest>> correctionsByEntity = requests.stream()
                    .filter(r -> r.mode() == WriteMode.CORRECTION)
                    .collect(Collectors.groupingBy(ChangeRequest::entityId));
            correctionsByEntity.values().forEach(group ->
                    written.addAll(applyCorrections(group, transactionDate)));

            lastCommitDate = transactionDate;
            commitLog.add(new CommitLogEntry(transactionDate, requests.size(),
                    requests.stream().map(CommitLogEntry::summarize).toList()));
            return new CommitResult(transactionDate, List.copyOf(written));
        } catch (RuntimeException e) {
            // 兜底：落库阶段失败时回滚到事务前状态，保证原子性
            rows.clear();
            rows.addAll(snapshotBefore);
            throw e;
        }
    }

    // ---- 校验 -------------------------------------------------------------

    private void validateRequests(List<ChangeRequest> requests) {
        for (ChangeRequest req : requests) {
            require(req.entityId() != null && !req.entityId().isBlank(),
                    "each request requires entityId");
            require(req.department() != null && !req.department().isBlank(),
                    "each request requires department");
            require(req.role() != null && !req.role().isBlank(),
                    "each request requires role");
            require(req.validFrom() != null, "validFrom is required");
            require(req.mode() != null, "mode must be INSERT or CORRECTION");
            try {
                req.validInterval(); // 半开区间自身约束（to > from）
            } catch (IllegalArgumentException e) {
                throw new BitemporalException(e.getMessage());
            }
        }
    }

    /** 同一事务内、同一实体的多笔变更，其目标有效区间不得相交，否则落库顺序会影响结果。 */
    private void validateIntraTransaction(List<ChangeRequest> requests) {
        for (int i = 0; i < requests.size(); i++) {
            for (int j = i + 1; j < requests.size(); j++) {
                ChangeRequest a = requests.get(i);
                ChangeRequest b = requests.get(j);
                if (a.entityId().equals(b.entityId()) && a.validInterval().overlaps(b.validInterval())) {
                    throw new BitemporalException(
                            "changes in one transaction must not target overlapping valid-time "
                                    + "intervals for entity " + a.entityId() + ": "
                                    + a.validInterval() + " vs " + b.validInterval());
                }
            }
        }
    }

    /** INSERT：目标有效区间与任一“当前版本”重叠即拒绝（半开相接不算重叠）。 */
    private void validateInsert(ChangeRequest req) {
        rows.stream()
                .filter(r -> r.entityId().equals(req.entityId()))
                .filter(r -> r.recorded().to() == null)
                .filter(r -> r.valid().overlaps(req.validInterval()))
                .findFirst()
                .ifPresent(r -> {
                    throw new BitemporalException(
                            "INSERT rejected: overlapping current record (rowId=" + r.rowId()
                                    + ") for entity " + req.entityId() + " over valid interval "
                                    + r.valid() + "; new interval " + req.validInterval()
                                    + " overlaps it (use CORRECTION to restate history)");
                });
    }

    // ---- 落库 -------------------------------------------------------------

    private BitemporalRecord insertRow(ChangeRequest req, Interval recorded) {
        return newRow(req.entityId(), req.department(), req.role(), req.validInterval(), recorded);
    }

    /**
     * 对同一实体的一批（互不重叠的）修订：
     * <ol>
     *   <li>受影响的当前版本记录时间统一在 {@code txDate} 关闭（旧行原样保留）；</li>
     *   <li>旧有效区间扣除全部目标区间后的碎片，作为续存行拆分写入；</li>
     *   <li>每个目标区间各写一条新事实行。</li>
     * </ol>
     */
    private List<BitemporalRecord> applyCorrections(List<ChangeRequest> group, LocalDate txDate) {
        String entityId = group.get(0).entityId();
        List<Interval> targets = group.stream()
                .map(ChangeRequest::validInterval)
                .sorted(Comparator.comparing(Interval::from))
                .toList();

        List<BitemporalRecord> written = new ArrayList<>();
        List<BitemporalRecord> affected = rows.stream()
                .filter(r -> r.entityId().equals(entityId))
                .filter(r -> r.recorded().to() == null)
                .filter(r -> targets.stream().anyMatch(t -> t.overlaps(r.valid())))
                .toList();

        for (BitemporalRecord old : affected) {
            closeRecord(old, txDate);
            for (Interval piece : subtractAll(old.valid(), targets)) {
                written.add(newRow(old.entityId(), old.department(), old.role(),
                        piece, Interval.openEnded(txDate)));
            }
        }
        for (ChangeRequest req : group) {
            written.add(newRow(req.entityId(), req.department(), req.role(),
                    req.validInterval(), Interval.openEnded(txDate)));
        }
        return written;
    }

    /**
     * 从半开区间 {@code src} 中扣除若干互不重叠、按起点排序的切口区间，
     * 返回剩余的零个或多个半开区间（相接边界不产生空区间）。
     */
    static List<Interval> subtractAll(Interval src, List<Interval> sortedCuts) {
        List<Interval> cuts = sortedCuts.stream().filter(src::overlaps).toList();
        List<Interval> out = new ArrayList<>();
        LocalDate cursor = src.from();
        LocalDate srcEnd = src.to();

        for (Interval cut : cuts) {
            if (cut.from().isAfter(cursor)) {
                LocalDate gapEnd = srcEnd == null
                        ? cut.from()
                        : (cut.from().isBefore(srcEnd) ? cut.from() : srcEnd);
                if (gapEnd.isAfter(cursor)) {
                    out.add(new Interval(cursor, gapEnd));
                }
            }
            if (cut.to() == null) {
                return out; // 开放切口吞掉后续全部
            }
            cursor = cut.to().isAfter(cursor) ? cut.to() : cursor;
            if (srcEnd != null && !cursor.isBefore(srcEnd)) {
                return out;
            }
        }
        if (srcEnd == null) {
            out.add(Interval.openEnded(cursor));
        } else if (srcEnd.isAfter(cursor)) {
            out.add(new Interval(cursor, srcEnd));
        }
        return out;
    }

    private void closeRecord(BitemporalRecord old, LocalDate txDate) {
        int idx = rows.indexOf(old);
        rows.set(idx, old.withRecordedTo(txDate));
    }

    private BitemporalRecord newRow(
            String entityId, String department, String role,
            Interval valid, Interval recorded) {
        BitemporalRecord row = new BitemporalRecord(
                nextRowId++, entityId, department, role, valid, recorded);
        rows.add(row);
        return row;
    }

    private static void require(boolean condition, String message) {
        if (!condition) {
            throw new BitemporalException(message);
        }
    }
}
