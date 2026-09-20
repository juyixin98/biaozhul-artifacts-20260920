package com.example.asset;

import com.example.asset.domain.*;
import com.example.asset.repository.AssetAdjustmentRepository;
import com.example.asset.repository.DepreciationEntryRepository;
import com.example.asset.service.*;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.MediaType;
import org.springframework.security.test.context.support.WithMockUser;

import java.math.BigDecimal;
import java.time.YearMonth;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.*;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

/** 折旧计提：重复计提、残值边界、期间关闭、参数调整、并发处置、导出。 */
class DepreciationIT extends AbstractIntegrationTest {

    @Autowired
    private DepreciationService depreciationService;
    @Autowired
    private AdjustmentService adjustmentService;
    @Autowired
    private PeriodService periodService;
    @Autowired
    private AssetService assetService;
    @Autowired
    private DepreciationEntryRepository entryRepository;
    @Autowired
    private AssetAdjustmentRepository adjustmentRepository;

    private List<DepreciationEntry> entriesOf(Asset asset) {
        return entryRepository.findByAssetIdOrderByPeriodAsc(asset.getId());
    }

    @Test
    void duplicateRunDoesNotDoubleBook() {
        Asset asset = newAsset("12000.00", "600.00", 36, DepreciationMethod.STRAIGHT_LINE,
                AssetStatus.IN_USE, "2026-01-01");
        YearMonth period = YearMonth.of(2026, 2);

        var first = depreciationService.run(period);
        var second = depreciationService.run(period);
        depreciationService.run(period);

        List<DepreciationEntry> entries = entriesOf(asset);
        assertThat(entries).hasSize(1);
        assertThat(first.created()).isGreaterThanOrEqualTo(1);
        // 12000-600 / 36 = 316.67（HALF_UP）
        assertThat(entries.get(0).getAmount()).isEqualByComparingTo("316.67");
        assertThat(entries.get(0).getOpeningValue()).isEqualByComparingTo("12000.00");
        assertThat(entries.get(0).getClosingValue()).isEqualByComparingTo("11683.33");
        assertThat(second.entries()).noneMatch(e -> e.getAssetId().equals(asset.getId()));
    }

    @Test
    void straightLineNeverBelowSalvageAndEndsExactly() {
        // 100.00 / 3 期：33.33、33.33、33.34，期末恰好 0.00（=残值）
        Asset asset = newAsset("100.00", "0.00", 3, DepreciationMethod.STRAIGHT_LINE,
                AssetStatus.IN_USE, "2026-01-01");
        depreciationService.run(YearMonth.of(2026, 2));
        depreciationService.run(YearMonth.of(2026, 3));
        depreciationService.run(YearMonth.of(2026, 4));
        depreciationService.run(YearMonth.of(2026, 5)); // 已提足，不再产生明细

        List<DepreciationEntry> entries = entriesOf(asset);
        assertThat(entries).hasSize(3);
        // 每期按 (期初-残值)/剩余月数 重新计算并 HALF_UP：66.67/2=33.335 -> 33.34
        assertThat(entries.get(0).getAmount()).isEqualByComparingTo("33.33");
        assertThat(entries.get(1).getAmount()).isEqualByComparingTo("33.34");
        assertThat(entries.get(2).getAmount()).isEqualByComparingTo("33.33");
        assertThat(entries.get(2).getClosingValue()).isEqualByComparingTo("0.00");
        assertThat(entries).allSatisfy(e ->
                assertThat(e.getClosingValue()).isGreaterThanOrEqualTo(BigDecimal.ZERO));
    }

    @Test
    void decliningBalanceEndsExactlyAtSalvage() {
        Asset asset = newAsset("10000.00", "500.00", 12, DepreciationMethod.DECLINING_BALANCE,
                AssetStatus.IN_USE, "2026-01-01");
        YearMonth first = YearMonth.of(2026, 2);
        for (int i = 0; i < 12; i++) {
            depreciationService.run(first.plusMonths(i));
        }
        List<DepreciationEntry> entries = entriesOf(asset);
        assertThat(entries).hasSize(12);
        BigDecimal salvage = new BigDecimal("500.00");
        assertThat(entries).allSatisfy(e ->
                assertThat(e.getClosingValue()).isGreaterThanOrEqualTo(salvage));
        assertThat(entries.get(11).getClosingValue()).isEqualByComparingTo("500.00");
        // 期初期末勾稽：每期 closing = opening - amount
        assertThat(entries).allSatisfy(e ->
                assertThat(e.getOpeningValue().subtract(e.getAmount()))
                        .isEqualByComparingTo(e.getClosingValue()));
    }

    @Test
    void closedPeriodCannotBeRecalculated() {
        Asset asset = newAsset("5000.00", "200.00", 24, DepreciationMethod.STRAIGHT_LINE,
                AssetStatus.IN_USE, "2026-01-01");
        YearMonth period = YearMonth.of(2027, 3);
        depreciationService.run(period);
        int before = entriesOf(asset).size();

        periodService.close(period, "finance");
        // 关账幂等
        periodService.close(period, "finance");

        assertThatThrownBy(() -> depreciationService.run(period))
                .isInstanceOf(ApiException.Conflict.class)
                .hasMessageContaining("closed");
        assertThat(entriesOf(asset)).hasSize(before);
        assertThat(periodService.get(period).isClosed()).isTrue();
    }

    @Test
    void adjustmentAppliesProspectivelyAndKeepsAuditTrail() {
        Asset asset = newAsset("12000.00", "600.00", 36, DepreciationMethod.STRAIGHT_LINE,
                AssetStatus.IN_USE, "2026-01-01");
        depreciationService.run(YearMonth.of(2027, 5));
        DepreciationEntry first = entriesOf(asset).get(0);
        assertThat(first.getAmount()).isEqualByComparingTo("316.67");

        // 财务调整：成本 12000 -> 14000，年限 36 -> 48 个月
        AssetAdjustment adjustment = adjustmentService.adjust(asset.getId(),
                new BigDecimal("14000.00"), 48, "资产改良追加投入", "finance");

        assertThat(adjustment.getOldCost()).isEqualByComparingTo("12000.00");
        assertThat(adjustment.getNewCost()).isEqualByComparingTo("14000.00");
        assertThat(adjustment.getOldUsefulLifeMonths()).isEqualTo(36);
        assertThat(adjustment.getNewUsefulLifeMonths()).isEqualTo(48);
        assertThat(adjustment.getReason()).isEqualTo("资产改良追加投入");
        assertThat(adjustment.getBookValueDelta()).isEqualByComparingTo("2000.00");

        depreciationService.run(YearMonth.of(2027, 6));
        List<DepreciationEntry> entries = entriesOf(asset);
        assertThat(entries).hasSize(2);

        DepreciationEntry second = entries.get(1);
        // 期初 = 上期期末 11683.33 + 未入账成本调整 2000.00
        assertThat(second.getOpeningValue()).isEqualByComparingTo("13683.33");
        assertThat(second.getAdjustmentDelta()).isEqualByComparingTo("2000.00");
        // 未来适用：(13683.33 - 600) / 剩余 47 个月 = 278.37
        assertThat(second.getAmount()).isEqualByComparingTo("278.37");
        assertThat(second.getClosingValue()).isEqualByComparingTo("13404.96");

        // 已计提期间不被回溯修改
        DepreciationEntry firstReloaded = entriesOf(asset).get(0);
        assertThat(firstReloaded.getAmount()).isEqualByComparingTo("316.67");
        assertThat(firstReloaded.getClosingValue()).isEqualByComparingTo("11683.33");

        // 调整差额只入账一次
        depreciationService.run(YearMonth.of(2027, 7));
        assertThat(entriesOf(asset).get(2).getAdjustmentDelta()).isEqualByComparingTo("0.00");
        assertThat(adjustmentRepository.findByAssetIdOrderByIdAsc(asset.getId())).hasSize(1);
    }

    @Test
    void disposedAssetStopsDepreciating() {
        Asset asset = newAsset("3000.00", "100.00", 24, DepreciationMethod.STRAIGHT_LINE,
                AssetStatus.IN_USE, "2026-01-01");
        depreciationService.run(YearMonth.of(2028, 2));
        assertThat(entriesOf(asset)).hasSize(1);

        Asset current = assetService.get(asset.getId());
        assetService.transition(asset.getId(), AssetStatus.RETIRED, current.getVersion(),
                "retire-" + UUID.randomUUID(), "manager");
        current = assetService.get(asset.getId());
        assetService.transition(asset.getId(), AssetStatus.DISPOSED, current.getVersion(),
                "dispose-" + UUID.randomUUID(), "manager");

        depreciationService.run(YearMonth.of(2028, 3));
        depreciationService.run(YearMonth.of(2028, 4));
        assertThat(entriesOf(asset)).hasSize(1);
    }

    @Test
    void concurrentRetireAndDepreciationRunStayConsistent() throws Exception {
        Asset asset = newAsset("8000.00", "400.00", 36, DepreciationMethod.STRAIGHT_LINE,
                AssetStatus.IN_USE, "2026-01-01");
        YearMonth period = YearMonth.of(2029, 6);

        ExecutorService pool = Executors.newFixedThreadPool(2);
        CountDownLatch gate = new CountDownLatch(1);
        Future<?> runFuture = pool.submit(() -> {
            await(gate);
            depreciationService.run(period);
        });
        Future<?> retireFuture = pool.submit(() -> {
            await(gate);
            // 与计提并发：版本可能被计提事务推进，冲突时取最新版本重试一次
            try {
                Asset a = assetService.get(asset.getId());
                assetService.transition(asset.getId(), AssetStatus.RETIRED, a.getVersion(),
                        "race-retire-" + UUID.randomUUID(), "manager");
            } catch (ApiException.Conflict e) {
                Asset a = assetService.get(asset.getId());
                assetService.transition(asset.getId(), AssetStatus.RETIRED, a.getVersion(),
                        "race-retire-" + UUID.randomUUID(), "manager");
            }
        });
        gate.countDown();
        runFuture.get(30, TimeUnit.SECONDS);
        retireFuture.get(30, TimeUnit.SECONDS);
        pool.shutdown();

        Asset reloaded = assetService.get(asset.getId());
        assertThat(reloaded.getStatus()).isEqualTo(AssetStatus.RETIRED);

        List<DepreciationEntry> entries = entriesOf(asset);
        // 两种串行结果都一致：计提先于退役（有 1 条明细）或退役先于计提（无明细）
        assertThat(entries.size()).isLessThanOrEqualTo(1);
        // 重跑同一期间：退役资产绝不再产生新明细
        depreciationService.run(period);
        assertThat(entriesOf(asset)).hasSameSizeAs(entries);
        // 若已入账，账目自身勾稽一致
        entries.forEach(e -> assertThat(e.getOpeningValue().subtract(e.getAmount()))
                .isEqualByComparingTo(e.getClosingValue()));
    }

    @Test
    @WithMockUser(username = "finance", roles = "FINANCE")
    void exportCsvExplainsOpeningAmountAndClosing() throws Exception {
        Asset asset = newAsset("6000.00", "300.00", 24, DepreciationMethod.STRAIGHT_LINE,
                AssetStatus.IN_USE, "2026-01-01");
        YearMonth period = YearMonth.of(2030, 8);
        depreciationService.run(period);

        String csv = mvc.perform(get("/api/depreciation/export").param("period", period.toString()))
                .andExpect(status().isOk())
                .andExpect(header().string("Content-Type", org.hamcrest.Matchers.containsString("text/csv")))
                .andReturn().getResponse().getContentAsString();

        assertThat(csv).contains("asset_code,asset_name,department,method,period," +
                "opening_value,adjustment_delta,depreciation_amount,closing_value");
        assertThat(csv).contains(asset.getAssetCode());
        // 6000-300 / 24 = 237.50
        assertThat(csv).contains("237.50");
        assertThat(csv).contains("5762.50");
    }

    private static void await(CountDownLatch latch) {
        try {
            latch.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }
}
