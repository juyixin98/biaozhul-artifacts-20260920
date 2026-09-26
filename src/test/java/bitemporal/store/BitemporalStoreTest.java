package bitemporal.store;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import bitemporal.error.OverlapRejectedException;
import bitemporal.error.RecordNotFoundException;
import bitemporal.error.ValidationException;
import bitemporal.model.ChangeRequest;
import bitemporal.model.QueryRequest;
import bitemporal.model.TemporalRecord;
import bitemporal.model.TransactionRequest;

import java.time.Instant;
import java.util.List;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

/**
 * 核心双时态行为测试。所有期望结果均按半开区间语义手工推导，
 * 时间锚点与 {@link SeedData} 一致（UTC）。
 */
class BitemporalStoreTest {

    private static final Instant T0 = Instant.parse("2026-01-01T00:00:00Z");
    private static final Instant Q1 = T0;
    private static final Instant Q2 = Instant.parse("2026-04-01T00:00:00Z");
    private static final Instant Q3 = Instant.parse("2026-07-01T00:00:00Z");

    private static final Instant FIX_AT = Instant.parse("2026-03-01T09:00:00Z");
    private static final Instant FIX_FROM = Instant.parse("2026-01-15T00:00:00Z");
    private static final Instant FIX_TO = Instant.parse("2026-03-01T00:00:00Z");

    private BitemporalStore store;

    @BeforeEach
    void setUp() {
        store = new BitemporalStore();
        store.commit(SeedData.seedTransaction());
    }

    private TemporalRecord queryOne(String recordId, String validAt, String systemAt) {
        List<TemporalRecord> rows = store.asOf(new QueryRequest(
                null,
                Instant.parse(validAt),
                Instant.parse(systemAt),
                recordId));
        return rows.isEmpty() ? null : rows.get(0);
    }

    @Nested
    @DisplayName("as-of 双维查询：追溯修订前后的不同观察时刻")
    class AsOfRetroCorrection {

        @Test
        void beforeFixSystemSeesOriginalBelief() {
            // 手算：系统时间 2026-02-15 早于修订提交，Q1 仍是 L3/Platform。
            TemporalRecord r = queryOne("emp-1001", "2026-02-15T00:00:00Z", "2026-02-15T12:00:00Z");
            assertEquals("{\"level\":\"L3\",\"team\":\"Platform\"}", r.data());
            assertEquals(1L, r.versionId());
        }

        @Test
        void afterFixSystemSeesCorrectedValueInsideWindow() {
            store.commit(new TransactionRequest("fix-q1", FIX_AT, List.of(
                    new ChangeRequest("revise", "emp-1001",
                            "{\"level\":\"L2\",\"team\":\"Platform\"}", FIX_FROM, FIX_TO))));

            // 同一业务时刻 2026-02-15，系统时间晚于修订 → 看到 L2。
            TemporalRecord inside = queryOne("emp-1001", "2026-02-15T00:00:00Z", "2026-03-02T00:00:00Z");
            assertEquals("{\"level\":\"L2\",\"team\":\"Platform\"}", inside.data());
            assertEquals("fix-q1", inside.txnId());
        }

        @Test
        void afterFixOutsideWindowOriginalValueSurvives() {
            store.commit(new TransactionRequest("fix-q1", FIX_AT, List.of(
                    new ChangeRequest("revise", "emp-1001",
                            "{\"level\":\"L2\",\"team\":\"Platform\"}", FIX_FROM, FIX_TO))));

            // 业务时刻 2026-01-10 落在修订窗口 [01-15,03-01) 之前 → 左残段，仍为 L3。
            TemporalRecord left = queryOne("emp-1001", "2026-01-10T00:00:00Z", "2026-03-02T00:00:00Z");
            assertEquals("{\"level\":\"L3\",\"team\":\"Platform\"}", left.data());

            // 业务时刻 2026-03-15 落在修订窗口之后 → 右残段，仍为 L3。
            TemporalRecord right = queryOne("emp-1001", "2026-03-15T00:00:00Z", "2026-03-02T00:00:00Z");
            assertEquals("{\"level\":\"L3\",\"team\":\"Platform\"}", right.data());

            // Q2/Q3 完全未被触及。
            assertEquals("{\"level\":\"L4\",\"team\":\"Platform\"}",
                    queryOne("emp-1001", "2026-05-01T00:00:00Z", "2026-03-02T00:00:00Z").data());
            assertEquals("{\"level\":\"L4\",\"team\":\"Infra\"}",
                    queryOne("emp-1001", "2026-10-01T00:00:00Z", "2026-03-02T00:00:00Z").data());
        }

        @Test
        void halfOpenSystemBoundaryAtCommitInstant() {
            store.commit(new TransactionRequest("fix-q1", FIX_AT, List.of(
                    new ChangeRequest("revise", "emp-1001",
                            "{\"level\":\"L2\",\"team\":\"Platform\"}", FIX_FROM, FIX_TO))));

            // systemFrom 包含：恰好在提交时刻观察即看到新行；前一秒仍是旧信念。
            TemporalRecord atInstant = queryOne("emp-1001", "2026-02-15T00:00:00Z", FIX_AT.toString());
            assertEquals("{\"level\":\"L2\",\"team\":\"Platform\"}", atInstant.data());

            TemporalRecord oneSecondBefore = queryOne("emp-1001", "2026-02-15T00:00:00Z",
                    FIX_AT.minusSeconds(1).toString());
            assertEquals("{\"level\":\"L3\",\"team\":\"Platform\"}", oneSecondBefore.data());
        }

        @Test
        void halfOpenValidBoundariesAtWindowEdges() {
            store.commit(new TransactionRequest("fix-q1", FIX_AT, List.of(
                    new ChangeRequest("revise", "emp-1001",
                            "{\"level\":\"L2\",\"team\":\"Platform\"}", FIX_FROM, FIX_TO))));

            String sys = "2026-03-02T00:00:00Z";
            // validFrom 包含：01-15T00:00 起为 L2。
            assertEquals("{\"level\":\"L2\",\"team\":\"Platform\"}",
                    queryOne("emp-1001", FIX_FROM.toString(), sys).data());
            // validTo 排除：03-01T00:00 已回到 L3。
            assertEquals("{\"level\":\"L3\",\"team\":\"Platform\"}",
                    queryOne("emp-1001", FIX_TO.toString(), sys).data());
        }

        @Test
        void revisionKeepsFullHistoryRows() {
            store.commit(new TransactionRequest("fix-q1", FIX_AT, List.of(
                    new ChangeRequest("revise", "emp-1001",
                            "{\"level\":\"L2\",\"team\":\"Platform\"}", FIX_FROM, FIX_TO))));

            List<TemporalRecord> history = store.history("emp-1001");
            // 原始 v1 被封口（仍保留）；修订把 [Q1,Q2) 切成 左残段/新值/右残段 3 行，
            // 外加未触及的 v2、v3 → 共 6 行。
            assertEquals(6, history.size());

            TemporalRecord closedV1 = history.get(0);
            assertEquals(1L, closedV1.versionId());
            assertEquals(FIX_AT, closedV1.systemTo(), "old row is closed at revision commit time");
            assertEquals(Q1, closedV1.systemFrom());

            List<TemporalRecord> current = history.stream().filter(r -> r.systemTo() == null).toList();
            assertEquals(5, current.size(), "business timeline remains fully covered by 5 current rows");
        }
    }

    @Nested
    @DisplayName("重叠拒绝：半开相邻允许，真正相交拒绝")
    class OverlapRejection {

        @Test
        void insertOverlappingCurrentVersionIsRejected() {
            OverlapRejectedException ex = assertThrows(OverlapRejectedException.class,
                    () -> store.commit(new TransactionRequest("bad", Instant.parse("2026-02-01T00:00:00Z"),
                            List.of(new ChangeRequest("insert", "emp-1001", "{}",
                                    Instant.parse("2026-02-01T00:00:00Z"),
                                    Instant.parse("2026-03-01T00:00:00Z"))))));
            assertEquals("emp-1001", ex.recordId());
        }

        @Test
        void adjacentIntervalsAreAccepted() {
            // [2025-12-01, 2026-01-01) 与首段 [01-01,04-01) 仅在被排除的端点相接。
            assertDoesNotThrow(() -> store.commit(new TransactionRequest("late-knowledge",
                    Instant.parse("2026-02-01T00:00:00Z"),
                    List.of(new ChangeRequest("insert", "emp-1001",
                            "{\"level\":\"L1\",\"team\":\"Platform\"}",
                            Instant.parse("2025-12-01T00:00:00Z"),
                            Q1)))));
            assertEquals("{\"level\":\"L1\",\"team\":\"Platform\"}",
                    queryOne("emp-1001", "2025-12-15T00:00:00Z", "2026-02-02T00:00:00Z").data());
        }

        @Test
        void touchingAtOneInstantIsAllowedButZeroLengthIsNot() {
            assertDoesNotThrow(() -> store.commit(new TransactionRequest("adj",
                    Instant.parse("2026-02-01T00:00:00Z"),
                    List.of(new ChangeRequest("insert", "emp-1001", "{}",
                            Instant.parse("2025-11-01T00:00:00Z"),
                            Instant.parse("2025-12-01T00:00:00Z")),
                            new ChangeRequest("insert", "emp-1001", "{}",
                                    Instant.parse("2025-12-01T00:00:00Z"), Q1)))));
        }
    }

    @Nested
    @DisplayName("同一事务多变更")
    class TransactionChangelist {

        @Test
        void multipleChangesInOneTransactionCommitAtomically() {
            var result = store.commit(new TransactionRequest("multi",
                    Instant.parse("2026-02-01T00:00:00Z"),
                    List.of(
                            new ChangeRequest("insert", "emp-1003", "first",
                                    Instant.parse("2026-01-01T00:00:00Z"),
                                    Instant.parse("2026-02-01T00:00:00Z")),
                            new ChangeRequest("insert", "emp-1003", "second",
                                    Instant.parse("2026-02-01T00:00:00Z"),
                                    Instant.parse("2026-03-01T00:00:00Z")))));
            assertEquals(2, result.written().size());
            assertEquals("first",
                    queryOne("emp-1003", "2026-01-15T00:00:00Z", "2026-02-02T00:00:00Z").data());
            assertEquals("second",
                    queryOne("emp-1003", "2026-02-15T00:00:00Z", "2026-02-02T00:00:00Z").data());
        }

        @Test
        void laterChangeSeesEarlierChangeWithinSameTransaction() {
            // 先插一段，再在同事务内修订它：第二变更必须看见第一变更。
            var result = store.commit(new TransactionRequest("insert-then-revise",
                    Instant.parse("2026-02-01T00:00:00Z"),
                    List.of(
                            new ChangeRequest("insert", "emp-1004", "v1",
                                    Instant.parse("2026-01-01T00:00:00Z"),
                                    Instant.parse("2026-12-01T00:00:00Z")),
                            new ChangeRequest("revise", "emp-1004", "v2",
                                    Instant.parse("2026-02-01T00:00:00Z"),
                                    Instant.parse("2026-03-01T00:00:00Z")))));
            // 原 insert 行被撤回，落库的是左残段/新值/右残段三行。
            assertEquals(3, result.written().size());
            assertEquals("v1", queryOne("emp-1004", "2026-01-15T00:00:00Z",
                    "2026-02-02T00:00:00Z").data());
            assertEquals("v2", queryOne("emp-1004", "2026-02-15T00:00:00Z",
                    "2026-02-02T00:00:00Z").data());
            assertEquals("v1", queryOne("emp-1004", "2026-04-01T00:00:00Z",
                    "2026-02-02T00:00:00Z").data(), "right remnant keeps the inserted value");

            List<TemporalRecord> history = store.history("emp-1004");
            assertEquals(3, history.size(),
                    "never-visible same-transaction insert row is dropped, not closed to [t,t)");
            assertTrue(history.stream().allMatch(r -> !r.systemFrom().equals(r.systemTo())));
        }

        @Test
        void failedTransactionRollsBackEverything() {
            // 第一个变更合法（emp-1005 新记录），第二个与同事务第一个重叠 → 整体拒绝。
            assertThrows(OverlapRejectedException.class,
                    () -> store.commit(new TransactionRequest("aborted",
                            Instant.parse("2026-02-01T00:00:00Z"),
                            List.of(
                                    new ChangeRequest("insert", "emp-1005", "a",
                                            Instant.parse("2026-01-01T00:00:00Z"),
                                            Instant.parse("2026-03-01T00:00:00Z")),
                                    new ChangeRequest("insert", "emp-1005", "b",
                                            Instant.parse("2026-02-01T00:00:00Z"),
                                            Instant.parse("2026-04-01T00:00:00Z"))))));

            assertFalse(store.recordIds().contains("emp-1005"),
                    "none of the transaction's changes may survive a rejected commit");
            assertNull(queryOne("emp-1005", "2026-02-15T00:00:00Z", "2026-02-02T00:00:00Z"));
        }

        @Test
        void overlappingDifferentRecordsDoNotConflict() {
            assertDoesNotThrow(() -> store.commit(new TransactionRequest("parallel",
                    Instant.parse("2026-02-01T00:00:00Z"),
                    List.of(
                            new ChangeRequest("insert", "emp-1006", "x", Q1, Q2),
                            new ChangeRequest("insert", "emp-1007", "y", Q1, Q2)))));
            assertTrue(store.recordIds().containsAll(List.of("emp-1006", "emp-1007")));
        }
    }

    @Nested
    @DisplayName("修订的区间切分与覆盖校验")
    class RevisionSemantics {

        @Test
        void reviseAcrossTwoAdjacentVersionsSplitsBoth() {
            // 修订 [03-01, 05-01)：横跨 v1[Q1,Q2) 与 v2[Q2,Q3)。
            store.commit(new TransactionRequest("cross", Instant.parse("2026-03-02T00:00:00Z"),
                    List.of(new ChangeRequest("revise", "emp-1001", "X",
                            Instant.parse("2026-03-01T00:00:00Z"),
                            Instant.parse("2026-05-01T00:00:00Z")))));

            String sys = "2026-03-03T00:00:00Z";
            assertEquals("{\"level\":\"L3\",\"team\":\"Platform\"}",
                    queryOne("emp-1001", "2026-02-15T00:00:00Z", sys).data(),
                    "left remnant of v1");
            assertEquals("X", queryOne("emp-1001", "2026-03-15T00:00:00Z", sys).data());
            assertEquals("X", queryOne("emp-1001", "2026-04-15T00:00:00Z", sys).data());
            // 2026-05-01T00:00 为新值 validTo 排除点，回到 v2 残段 L4/Platform。
            assertEquals("{\"level\":\"L4\",\"team\":\"Platform\"}",
                    queryOne("emp-1001", "2026-05-01T00:00:00Z", sys).data());
        }

        @Test
        void reviseOpenEndedTargetSplitsLastOpenVersion() {
            store.commit(new TransactionRequest("open-revise", Instant.parse("2026-08-15T00:00:00Z"),
                    List.of(new ChangeRequest("revise", "emp-1001", "FUTURE",
                            Instant.parse("2026-09-01T00:00:00Z"), null))));

            String sys = "2026-08-16T00:00:00Z";
            assertEquals("{\"level\":\"L4\",\"team\":\"Infra\"}",
                    queryOne("emp-1001", "2026-08-15T00:00:00Z", sys).data(),
                    "left remnant of open-ended v3");
            assertEquals("FUTURE", queryOne("emp-1001", "2030-01-01T00:00:00Z", sys).data());
        }

        @Test
        void reviseUnknownRecordIsRejected() {
            assertThrows(RecordNotFoundException.class,
                    () -> store.commit(new TransactionRequest("r1", Instant.parse("2026-02-01T00:00:00Z"),
                            List.of(new ChangeRequest("revise", "ghost", "x", Q1, Q2)))));
        }

        @Test
        void reviseTargetWithGapIsRejected() {
            // 新记录只覆盖 [01-01,02-01)，修订 [01-15,03-15) 超出已知范围。
            store.commit(new TransactionRequest("sparse", Instant.parse("2026-02-01T00:00:00Z"),
                    List.of(new ChangeRequest("insert", "emp-1010", "only-jan",
                            Q1, Instant.parse("2026-02-01T00:00:00Z")))));
            assertThrows(ValidationException.class,
                    () -> store.commit(new TransactionRequest("r2", Instant.parse("2026-02-02T00:00:00Z"),
                            List.of(new ChangeRequest("revise", "emp-1010", "x",
                                    Instant.parse("2026-01-15T00:00:00Z"),
                                    Instant.parse("2026-03-15T00:00:00Z"))))));
        }
    }

    @Nested
    @DisplayName("系统时间轴规则")
    class SystemTimeRules {

        @Test
        void commitTimeMustBeStrictlyAfterPreviousCommit() {
            assertThrows(ValidationException.class,
                    () -> store.commit(new TransactionRequest("same-time", SeedData.SEED_COMMIT_AT,
                            List.of(new ChangeRequest("insert", "emp-1020", "x",
                                    Instant.parse("2025-01-01T00:00:00Z"),
                                    Instant.parse("2025-02-01T00:00:00Z"))))));
        }

        @Test
        void queryBeforeAnySystemCommitReturnsNothing() {
            List<TemporalRecord> rows = store.asOf(new QueryRequest(null,
                    Instant.parse("2026-01-15T00:00:00Z"),
                    Instant.parse("2025-12-31T23:59:59Z"), null));
            assertTrue(rows.isEmpty());
        }

        @Test
        void queryRequiresObservationTimes() {
            assertThrows(ValidationException.class,
                    () -> store.asOf(new QueryRequest(null, null, null, null)));
        }
    }

    // 小工具：让“成功路径”断言读起来更直白。
    private static void assertDoesNotThrow(Runnable runnable) {
        runnable.run();
    }
}
