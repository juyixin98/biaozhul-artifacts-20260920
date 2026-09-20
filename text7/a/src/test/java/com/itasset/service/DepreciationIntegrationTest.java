package com.itasset.service;
import com.itasset.AbstractIntegrationTest;

import com.itasset.domain.Asset;
import com.itasset.domain.DepreciationEntry;
import com.itasset.domain.DepreciationMethod;
import com.itasset.repo.DepreciationEntryRepository;
import com.itasset.web.CreateAssetRequest;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.List;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** 重复计提幂等、残值边界（含末月补差）、跳期拒绝、期间连续性。 */
class DepreciationIntegrationTest extends AbstractIntegrationTest {

    static String DB = newDatabase();

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        register(r, DB);
    }

    @Autowired AssetService assets;
    @Autowired DepreciationService depreciation;
    @Autowired DepreciationEntryRepository entryRepo;

    private Asset create(String code, BigDecimal cost, BigDecimal salvage, int life,
                         DepreciationMethod method, BigDecimal rate, LocalDate inService) {
        return assets.create(new CreateAssetRequest(code, "资产-" + code, cost, salvage,
                inService, life, "IT部", method, rate));
    }

    @Test
    void straightLine_threeMonthsEndsExactlyAtSalvage() {
        // 成本 1000，残值 10，3 个月：每月 (990/3)=330.00，末月后期末恰好 10
        Asset a = create("DEP-SL-" + UUID.randomUUID(),
                new BigDecimal("1000.00"), new BigDecimal("10.00"), 3,
                DepreciationMethod.STRAIGHT_LINE, null, LocalDate.of(2023, 12, 10));

        var r = depreciation.runDepreciation(a.getId(), "202401", "202403",
                UUID.randomUUID().toString());
        assertThat(r.createdEntries()).hasSize(3);
        List<DepreciationEntry> es = entryRepo.findByAssetIdOrderByPeriodAsc(a.getId());
        assertThat(es).extracting(e -> e.getDepreciationAmount().stripTrailingZeros())
                .containsExactly(bd("330.00").stripTrailingZeros(),
                        bd("330.00").stripTrailingZeros(),
                        bd("330.00").stripTrailingZeros());
        assertThat(es.get(0).getOpeningBookValue()).isEqualByComparingTo("1000.0000");
        assertThat(es.get(2).getClosingBookValue()).isEqualByComparingTo("10.0000");
        // 金额恒等式：期初 - 计提 = 期末
        for (DepreciationEntry e : es) {
            assertThat(e.getOpeningBookValue().subtract(e.getDepreciationAmount()))
                    .isEqualByComparingTo(e.getClosingBookValue());
            assertThat(e.getClosingBookValue()).isGreaterThanOrEqualTo(a.getSalvageValue());
        }
    }

    @Test
    void decliningBalance_salvageFloorAndFinalPlug() {
        // 年率 90%（月率 7.5%）：75.00、69.38，末月补差到残值 100
        Asset a = create("DEP-DB-" + UUID.randomUUID(),
                new BigDecimal("1000.00"), new BigDecimal("100.00"), 3,
                DepreciationMethod.DECLINING_BALANCE, new BigDecimal("90.0000"),
                LocalDate.of(2023, 12, 1));

        depreciation.runDepreciation(a.getId(), "202401", "202403",
                UUID.randomUUID().toString());
        List<DepreciationEntry> es = entryRepo.findByAssetIdOrderByPeriodAsc(a.getId());
        assertThat(es.get(0).getDepreciationAmount()).isEqualByComparingTo("75.00");
        assertThat(es.get(1).getDepreciationAmount()).isEqualByComparingTo("69.38");
        assertThat(es.get(2).getDepreciationAmount()).isEqualByComparingTo("755.62");
        assertThat(es.get(2).getClosingBookValue()).isEqualByComparingTo("100.0000");
    }

    @Test
    void rerunSamePeriodsIsIdempotentAndNeverDoublePosts() {
        Asset a = create("DEP-IDEM-" + UUID.randomUUID(),
                new BigDecimal("1200.00"), BigDecimal.ZERO, 12,
                DepreciationMethod.STRAIGHT_LINE, null, LocalDate.of(2023, 12, 1));

        String req = UUID.randomUUID().toString();
        var first = depreciation.runDepreciation(a.getId(), "202401", "202402", req);
        assertThat(first.createdEntries()).hasSize(2);

        // 同 requestId 重放：返回首次分录，零新增
        var replay = depreciation.runDepreciation(a.getId(), "202401", "202402", req);
        assertThat(replay.createdEntries()).hasSize(2);

        // 新 requestId 重跑同一区间：全部跳过
        var rerun = depreciation.runDepreciation(a.getId(), "202401", "202402",
                UUID.randomUUID().toString());
        assertThat(rerun.createdEntries()).isEmpty();
        assertThat(rerun.skippedPeriods()).containsExactly("202401", "202402");

        assertThat(entryRepo.countByAssetId(a.getId())).isEqualTo(2);
    }

    @Test
    void gapPeriodsAndBeforeServiceRejected() {
        Asset a = create("DEP-GAP-" + UUID.randomUUID(),
                new BigDecimal("1200.00"), BigDecimal.ZERO, 12,
                DepreciationMethod.STRAIGHT_LINE, null, LocalDate.of(2023, 12, 1));

        // 启用次月之前
        assertThatThrownBy(() -> depreciation.runDepreciation(a.getId(), "202312", "202312",
                UUID.randomUUID().toString()))
                .isInstanceOf(BusinessRuleException.class);

        depreciation.runDepreciation(a.getId(), "202401", "202401",
                UUID.randomUUID().toString());
        // 跳期 202402 直接计提 202403
        assertThatThrownBy(() -> depreciation.runDepreciation(a.getId(), "202403", "202403",
                UUID.randomUUID().toString()))
                .isInstanceOf(ConflictException.class)
                .hasMessageContaining("不连续");
    }

    private static BigDecimal bd(String v) {
        return new BigDecimal(v);
    }
}
