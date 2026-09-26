package com.example.bitemporal.engine;

import com.example.bitemporal.data.SeedData;
import com.example.bitemporal.model.BitemporalRecord;
import com.example.bitemporal.model.ChangeRequest;
import com.example.bitemporal.model.WriteMode;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

import java.time.LocalDate;
import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 基于固定种子数据的手算验收测试。
 * 种子（事务时间 2026-01-01）：
 * <pre>
 * E001: Engineering/Dev      [2025-01-01,2025-07-01)
 *       Engineering/TechLead [2025-07-01,+inf)
 * E002: Sales/Rep            [2025-01-01,2025-10-01)
 *       Sales/Manager        [2025-10-01,+inf)
 * E003: Finance/Analyst      [2025-03-01,+inf)
 * </pre>
 */
class BitemporalStoreTest {

    private static final LocalDate D2025_01_01 = LocalDate.of(2025, 1, 1);
    private static final LocalDate D2025_02_01 = LocalDate.of(2025, 2, 1);
    private static final LocalDate D2025_03_01 = LocalDate.of(2025, 3, 1);
    private static final LocalDate D2025_04_01 = LocalDate.of(2025, 4, 1);
    private static final LocalDate D2025_05_01 = LocalDate.of(2025, 5, 1);
    private static final LocalDate D2025_07_01 = LocalDate.of(2025, 7, 1);
    private static final LocalDate D2025_08_01 = LocalDate.of(2025, 8, 1);
    private static final LocalDate D2025_09_01 = LocalDate.of(2025, 9, 1);
    private static final LocalDate D2025_10_01 = LocalDate.of(2025, 10, 1);
    private static final LocalDate D2026_01_01 = LocalDate.of(2026, 1, 1);
    private static final LocalDate D2026_01_15 = LocalDate.of(2026, 1, 15);
    private static final LocalDate D2026_02_01 = LocalDate.of(2026, 2, 1);
    private static final LocalDate D2026_02_15 = LocalDate.of(2026, 2, 15);
    private static final LocalDate D2026_03_01 = LocalDate.of(2026, 3, 1);
    private static final LocalDate D2026_03_10 = LocalDate.of(2026, 3, 10);
    private static final LocalDate D2026_06_01 = LocalDate.of(2026, 6, 1);

    private BitemporalStore store;

    @BeforeEach
    void setUp() {
        store = SeedData.createSeededStore();
    }

    private Optional<String> roleAt(String entity, LocalDate biz, LocalDate obs) {
        return store.asOf(entity, biz, obs).map(BitemporalRecord::role);
    }

    @Nested
    @DisplayName("种子数据基线")
    class Baseline {

        @Test
        void seededTimelineForE001() {
            assertEquals("Dev", roleAt("E001", D2025_01_01, D2026_01_15).orElseThrow());
            assertEquals("Dev", roleAt("E001", LocalDate.of(2025, 6, 30), D2026_01_15).orElseThrow());
            // 半开边界：07-01 当天已属于 TechLead
            assertEquals("TechLead", roleAt("E001", D2025_07_01, D2026_01_15).orElseThrow());
            assertEquals("TechLead", roleAt("E001", LocalDate.of(2030, 1, 1), D2026_01_15).orElseThrow());
        }

        @Test
        void nothingKnownBeforeSeedTransaction() {
            // 观察时刻早于首次提交：系统里还没有任何记录
            assertTrue(roleAt("E001", D2025_03_01, LocalDate.of(2025, 12, 31)).isEmpty());
        }

        @Test
        void gapBeforeValidityReturnsEmpty() {
            assertTrue(roleAt("E001", LocalDate.of(2024, 12, 31), D2026_01_15).isEmpty());
        }
    }

    @Nested
    @DisplayName("追溯修订：2026-02-15 重述 E001 在 [2025-03-01,2025-09-01) 实为 Platform/SRE")
    class RetroactiveCorrection {

        @BeforeEach
        void correct() {
            store.commit(D2026_02_15, List.of(new ChangeRequest(
                    "E001", "Platform", "SRE",
                    D2025_03_01, D2025_09_01, WriteMode.CORRECTION)));
        }

        @Test
        void oldObservationsRemainReproducible() {
            // 修订前的观察时刻看到的仍是旧事实（历史行保留）
            assertEquals("Dev", roleAt("E001", D2025_03_01, D2026_01_15).orElseThrow());
            assertEquals("TechLead", roleAt("E001", LocalDate.of(2025, 8, 1), D2026_01_15).orElseThrow());
        }

        @Test
        void restatedTimelineAfterCorrection() {
            // 修订后观察：
            // Dev 残余 [01-01,03-01)；SRE [03-01,09-01)；TechLead 残余 [09-01,+inf)
            assertEquals("Dev", roleAt("E001", LocalDate.of(2025, 2, 28), D2026_06_01).orElseThrow());
            assertEquals("SRE", roleAt("E001", D2025_03_01, D2026_06_01).orElseThrow());
            assertEquals("SRE", roleAt("E001", LocalDate.of(2025, 5, 1), D2026_06_01).orElseThrow());
            assertEquals("SRE", roleAt("E001", LocalDate.of(2025, 8, 31), D2026_06_01).orElseThrow());
            // 半开边界：09-01 当天 SRE 区间已结束
            assertEquals("TechLead", roleAt("E001", D2025_09_01, D2026_06_01).orElseThrow());
            assertEquals("TechLead", roleAt("E001", LocalDate.of(2025, 10, 1), D2026_06_01).orElseThrow());
            assertEquals("Platform",
                    store.asOf("E001", D2025_03_01, D2026_06_01).orElseThrow().department());
        }

        @Test
        void observationExactlyAtCorrectionTxDateSeesNewVersion() {
            // 记录区间 [2026-02-15,+inf) 含起点
            assertEquals("SRE", roleAt("E001", D2025_03_01, D2026_02_15).orElseThrow());
            // 旧行记录区间 [2026-01-01,2026-02-15) 不含终点
            assertEquals("Dev", roleAt("E001", D2025_03_01, D2026_02_15.minusDays(1)).orElseThrow());
        }

        @Test
        void oldRowsAreClosedAndPreserved() {
            List<BitemporalRecord> closed = store.snapshot().stream()
                    .filter(r -> r.entityId().equals("E001"))
                    .filter(r -> r.recorded().to() != null)
                    .toList();
            assertEquals(2, closed.size(), "旧的两行都应被关闭保留");
            assertTrue(closed.stream().allMatch(r -> r.recorded().to().equals(D2026_02_15)));
            // 物理行数：原 5 行（全实体），E001 旧 2 行保留 + 新增 1 事实行 + 2 残余行
            assertEquals(8, store.snapshot().size());
        }

        @Test
        void currentTimelineIsContiguousViaHalfOpenAbutment() {
            List<BitemporalRecord> hist = store.history("E001", D2026_06_01);
            assertEquals(3, hist.size());
            assertEquals(D2025_01_01, hist.get(0).valid().from());
            assertEquals(D2025_03_01, hist.get(0).valid().to());
            assertEquals(D2025_03_01, hist.get(1).valid().from());
            assertEquals(D2025_09_01, hist.get(1).valid().to());
            assertEquals(D2025_09_01, hist.get(2).valid().from());
            assertEquals(java.util.Optional.empty(), java.util.Optional.ofNullable(hist.get(2).valid().to()));
        }
    }

    @Nested
    @DisplayName("INSERT 重叠拒绝")
    class InsertOverlap {

        @Test
        void rejectsOverlapInsideExisting() {
            BitemporalException ex = assertThrows(BitemporalException.class,
                    () -> store.commit(D2026_02_01, List.of(new ChangeRequest(
                            "E001", "X", "Y", LocalDate.of(2025, 6, 1),
                            LocalDate.of(2025, 8, 1), WriteMode.INSERT))));
            assertTrue(ex.getMessage().contains("overlap"));
            assertEquals(5, store.snapshot().size(), "拒绝后不得落任何行");
        }

        @Test
        void rejectsOverlapOnOpenEndedTail() {
            assertThrows(BitemporalException.class,
                    () -> store.commit(D2026_02_01, List.of(new ChangeRequest(
                            "E001", "X", "Y", D2025_07_01,
                            LocalDate.of(2025, 9, 1), WriteMode.INSERT))));
        }

        @Test
        void acceptsAbuttingHalfOpenInterval() {
            // [2024-06-01,2025-01-01) 与既有 [2025-01-01,...) 仅端点相接，合法
            store.commit(D2026_02_01, List.of(new ChangeRequest(
                    "E001", "Intern", "Trainee",
                    LocalDate.of(2024, 6, 1), D2025_01_01, WriteMode.INSERT)));
            assertEquals("Trainee", roleAt("E001", LocalDate.of(2024, 12, 31), D2026_06_01).orElseThrow());
            assertEquals("Dev", roleAt("E001", D2025_01_01, D2026_06_01).orElseThrow());
        }

        @Test
        void acceptsInsertForNewEntity() {
            store.commit(D2026_02_01, List.of(new ChangeRequest(
                    "E009", "Ops", "SRE", D2025_01_01, null, WriteMode.INSERT)));
            assertEquals("SRE", roleAt("E009", D2025_03_01, D2026_06_01).orElseThrow());
        }
    }

    @Nested
    @DisplayName("同一事务的多笔变更")
    class MultiChangeTransaction {

        @Test
        void correctionAndInsertForDifferentEntitiesCommitAtomically() {
            store.commit(D2026_03_01, List.of(
                    new ChangeRequest("E002", "Marketing", "Analyst",
                            D2025_02_01, D2025_04_01, WriteMode.CORRECTION),
                    new ChangeRequest("E003", "Finance", "Junior",
                            D2025_01_01, D2025_03_01, WriteMode.INSERT)));

            // E002：Rep 被切成 [01-01,02-01) 与 [04-01,10-01)，中间是 Marketing
            assertEquals("Rep", roleAt("E002", LocalDate.of(2025, 1, 31), D2026_06_01).orElseThrow());
            assertEquals("Analyst", roleAt("E002", D2025_03_01, D2026_06_01).orElseThrow());
            assertEquals("Marketing",
                    store.asOf("E002", D2025_03_01, D2026_06_01).orElseThrow().department());
            assertEquals("Rep", roleAt("E002", D2025_04_01, D2026_06_01).orElseThrow()); // 半开边界
            assertEquals("Manager", roleAt("E002", LocalDate.of(2025, 11, 1), D2026_06_01).orElseThrow());

            // E003：新插入 [01-01,03-01)，与原 [03-01,+inf) 相接
            assertEquals("Junior", roleAt("E003", LocalDate.of(2025, 2, 28), D2026_06_01).orElseThrow());
            assertEquals("Analyst", roleAt("E003", D2025_03_01, D2026_06_01).orElseThrow());

            // 两条新事实共享同一事务时间戳
            List<BitemporalRecord> newFacts = store.snapshot().stream()
                    .filter(r -> r.recorded().from().equals(D2026_03_01))
                    .filter(r -> r.role().equals("Analyst") && r.entityId().equals("E002")
                            || r.role().equals("Junior"))
                    .toList();
            assertEquals(2, newFacts.size());
        }

        @Test
        void twoCorrectionsSameEntityUseUnionSubtraction() {
            store.commit(D2026_03_01, List.of(
                    new ChangeRequest("E001", "Platform", "SRE",
                            D2025_02_01, D2025_04_01, WriteMode.CORRECTION),
                    new ChangeRequest("E001", "Security", "Auditor",
                            D2025_08_01, LocalDate.of(2025, 10, 1), WriteMode.CORRECTION)));

            assertEquals("Dev", roleAt("E001", LocalDate.of(2025, 1, 31), D2026_06_01).orElseThrow());
            assertEquals("SRE", roleAt("E001", D2025_03_01, D2026_06_01).orElseThrow());
            assertEquals("Dev", roleAt("E001", D2025_04_01, D2026_06_01).orElseThrow());
            assertEquals("TechLead", roleAt("E001", D2025_07_01, D2026_06_01).orElseThrow());
            assertEquals("Auditor", roleAt("E001", D2025_08_01, D2026_06_01).orElseThrow());
            assertEquals("TechLead", roleAt("E001", LocalDate.of(2025, 10, 1), D2026_06_01).orElseThrow());
            // 每个业务日期恰好一条当前版本
            for (LocalDate d = D2025_01_01; d.isBefore(D2026_01_01); d = d.plusDays(1)) {
                assertEquals(1, store.asOfAll(d, D2026_06_01).stream()
                        .filter(r -> r.entityId().equals("E001")).count(),
                        "uniqueness violated at " + d);
            }
        }

        @Test
        void oneRejectedChangeRollsBackWholeTransaction() {
            long rowsBefore = store.snapshot().size();
            assertThrows(BitemporalException.class, () -> store.commit(D2026_03_01, List.of(
                    new ChangeRequest("E009", "Ops", "SRE",
                            D2025_01_01, null, WriteMode.INSERT),          // 合法
                    new ChangeRequest("E001", "X", "Y",
                            D2025_03_01, D2025_04_01, WriteMode.INSERT)))); // 与 Dev 重叠
            assertEquals(rowsBefore, store.snapshot().size(), "事务回滚：合法的那笔也不得落库");
            assertTrue(store.asOf("E009", D2025_03_01, D2026_06_01).isEmpty());
            assertEquals(1, store.commitLog().size(), "只有种子事务一条日志");
        }

        @Test
        void overlappingTargetsInOneTransactionRejected() {
            assertThrows(BitemporalException.class, () -> store.commit(D2026_03_01, List.of(
                    new ChangeRequest("E001", "Platform", "SRE",
                            D2025_02_01, D2025_07_01, WriteMode.CORRECTION),
                    new ChangeRequest("E001", "Security", "Auditor",
                            D2025_03_01, D2025_08_01, WriteMode.CORRECTION))));
            assertEquals(5, store.snapshot().size());
        }
    }

    @Nested
    @DisplayName("事务时间规则")
    class TransactionTime {

        @Test
        void rejectsNonAdvancingTransactionTime() {
            store.commit(D2026_02_01, List.of(new ChangeRequest(
                    "E009", "Ops", "SRE", D2025_01_01, null, WriteMode.INSERT)));
            BitemporalException ex = assertThrows(BitemporalException.class,
                    () -> store.commit(D2026_02_01, List.of(new ChangeRequest(
                            "E010", "Ops", "SRE", D2025_01_01, null, WriteMode.INSERT))));
            assertTrue(ex.getMessage().contains("advance"));
        }

        @Test
        void secondCorrectionRestatesPreviousRestatement() {
            // 第一次重述：03-01~09-01 为 SRE
            store.commit(D2026_02_15, List.of(new ChangeRequest(
                    "E001", "Platform", "SRE", D2025_03_01, D2025_09_01, WriteMode.CORRECTION)));
            // 第二次重述：其实 05-01~07-01 是 Data/Eng
            store.commit(D2026_03_10, List.of(new ChangeRequest(
                    "E001", "Data", "Eng", D2025_05_01, D2025_07_01, WriteMode.CORRECTION)));

            // 观察 2026-03-01（两次修订之间）仍看到第一次重述
            assertEquals("SRE", roleAt("E001", D2025_05_01, D2026_03_01).orElseThrow());
            // 观察 2026-06-01 看到第二次重述，SRE 被切成两段
            assertEquals("SRE", roleAt("E001", D2025_04_01, D2026_06_01).orElseThrow());
            assertEquals("Eng", roleAt("E001", D2025_05_01, D2026_06_01).orElseThrow());
            assertEquals("Eng", roleAt("E001", LocalDate.of(2025, 6, 30), D2026_06_01).orElseThrow());
            assertEquals("SRE", roleAt("E001", D2025_07_01, D2026_06_01).orElseThrow());
        }
    }

    @Nested
    @DisplayName("输入校验")
    class Validation {

        @Test
        void rejectsInvertedInterval() {
            assertThrows(BitemporalException.class,
                    () -> store.commit(D2026_02_01, List.of(new ChangeRequest(
                            "E001", "X", "Y", D2025_07_01, D2025_01_01, WriteMode.INSERT))));
        }

        @Test
        void rejectsEmptyChangeList() {
            assertThrows(BitemporalException.class, () -> store.commit(D2026_02_01, List.of()));
        }
    }
}
