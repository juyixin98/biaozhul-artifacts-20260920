package com.itasset.service;
import com.itasset.AbstractIntegrationTest;

import com.itasset.domain.Asset;
import com.itasset.domain.DepreciationEntry;
import com.itasset.domain.DepreciationMethod;
import com.itasset.domain.ParameterAdjustment;
import com.itasset.repo.DepreciationEntryRepository;
import com.itasset.repo.ParameterAdjustmentRepository;
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

/**
 * 参数调整：只影响未关账/未计提期间，旧参数与原因留痕；
 * 调整开启新折旧段，后续期间以调整时点账面价值为基数。
 */
class ParameterAdjustmentIntegrationTest extends AbstractIntegrationTest {

    static String DB = newDatabase();

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        register(r, DB);
    }

    @Autowired AssetService assets;
    @Autowired DepreciationService depreciation;
    @Autowired PeriodCloseService closes;
    @Autowired ParameterAdjustmentService adjustments;
    @Autowired DepreciationEntryRepository entryRepo;
    @Autowired ParameterAdjustmentRepository adjustmentRepo;

    @Test
    void adjustmentOnlyAffectsFuturePeriodsAndKeepsOldParameters() {
        // 成本 1200，残值 0，12 月，2023-12 启用 -> 首期间 202401，每月 100
        Asset a = assets.create(new CreateAssetRequest("ADJ-" + UUID.randomUUID(), "测试机",
                new BigDecimal("1200.00"), BigDecimal.ZERO, LocalDate.of(2023, 12, 1),
                12, "IT部", DepreciationMethod.STRAIGHT_LINE, null));

        depreciation.runDepreciation(a.getId(), "202401", "202403",
                UUID.randomUUID().toString());
        List<DepreciationEntry> before = entryRepo.findByAssetIdOrderByPeriodAsc(a.getId());
        assertThat(before).hasSize(3);
        assertThat(before.get(2).getClosingBookValue()).isEqualByComparingTo("900.0000");

        // 关账 202401..202403 后尝试把生效期间放在 202403 → 拒绝
        closes.close("202401", "finance");
        closes.close("202402", "finance");
        closes.close("202403", "finance");
        assertThatThrownBy(() -> adjustments.adjust(a.getId(),
                new ParameterAdjustmentService.AdjustmentCommand("202403",
                        new BigDecimal("600.00"), null, 6, null, null,
                        "错误尝试", UUID.randomUUID().toString())))
                .isInstanceOf(ConflictException.class)
                .hasMessageContaining("已关账");

        // 正确：从下一未计提期间 202404 起，新寿命 18 月（已用 3，段月数 15），
        // 新残值 0；基数 900 在 15 个月内平摊 = 60/月
        ParameterAdjustment rec = adjustments.adjust(a.getId(),
                new ParameterAdjustmentService.AdjustmentCommand("202404",
                        null, BigDecimal.ZERO, 18, null, null,
                        "使用强度低于预期，延长寿命", UUID.randomUUID().toString()));

        assertThat(rec.getOldUsefulLifeMonths()).isEqualTo(12);
        assertThat(rec.getNewUsefulLifeMonths()).isEqualTo(18);
        assertThat(rec.getSegmentMonths()).isEqualTo(15);
        assertThat(rec.getElapsedMonths()).isEqualTo(3);
        assertThat(rec.getBookValueAtAdjustment()).isEqualByComparingTo("900.0000");
        assertThat(rec.getReason()).isEqualTo("使用强度低于预期，延长寿命");

        depreciation.runDepreciation(a.getId(), "202404", "202405",
                UUID.randomUUID().toString());
        List<DepreciationEntry> after = entryRepo.findByAssetIdOrderByPeriodAsc(a.getId());
        DepreciationEntry apr = after.get(3);
        DepreciationEntry may = after.get(4);
        assertThat(apr.getOpeningBookValue()).isEqualByComparingTo("900.0000");
        assertThat(apr.getDepreciationAmount()).isEqualByComparingTo("60.00");
        assertThat(apr.getClosingBookValue()).isEqualByComparingTo("840.0000");
        assertThat(apr.getPeriodIndex()).isEqualTo(1); // 新段第 1 月
        assertThat(may.getPeriodIndex()).isEqualTo(2);

        // 历史 3 期未被改写
        assertThat(before.get(0).getDepreciationAmount()).isEqualByComparingTo("100.00");
        assertThat(adjustmentRepo.findByAssetIdOrderByIdAsc(a.getId())).hasSize(1);
    }

    @Test
    void cannotAdjustAtPeriodAlreadyPosted() {
        Asset a = assets.create(new CreateAssetRequest("ADJ2-" + UUID.randomUUID(), "测试机",
                new BigDecimal("1200.00"), BigDecimal.ZERO, LocalDate.of(2023, 12, 1),
                12, "IT部", DepreciationMethod.STRAIGHT_LINE, null));
        depreciation.runDepreciation(a.getId(), "202401", "202402",
                UUID.randomUUID().toString());

        // 生效期间落在已计提的 202402 → 拒绝（只能从 202403 起）
        assertThatThrownBy(() -> adjustments.adjust(a.getId(),
                new ParameterAdjustmentService.AdjustmentCommand("202402",
                        null, null, 24, null, null, "x", UUID.randomUUID().toString())))
                .isInstanceOf(ConflictException.class)
                .hasMessageContaining("下一个未计提期间 202403");
    }

    @Test
    void adjustmentIdempotentByRequestId() {
        Asset a = assets.create(new CreateAssetRequest("ADJ3-" + UUID.randomUUID(), "测试机",
                new BigDecimal("1200.00"), BigDecimal.ZERO, LocalDate.of(2023, 12, 1),
                12, "IT部", DepreciationMethod.STRAIGHT_LINE, null));
        String req = UUID.randomUUID().toString();
        var cmd = new ParameterAdjustmentService.AdjustmentCommand("202401",
                null, null, 24, null, null, "延长", req);
        ParameterAdjustment first = adjustments.adjust(a.getId(), cmd);
        ParameterAdjustment again = adjustments.adjust(a.getId(), cmd);
        assertThat(again.getId()).isEqualTo(first.getId());
        assertThat(adjustmentRepo.findByAssetIdOrderByIdAsc(a.getId())).hasSize(1);
    }
}
