package com.example.itasset.service;

import com.example.itasset.AbstractIntegrationTest;
import com.example.itasset.domain.DepreciationEntry;
import com.example.itasset.domain.DepreciationMethod;
import com.example.itasset.domain.DepreciationPolicy;
import com.example.itasset.repo.DepreciationPolicyRepository;
import com.example.itasset.repo.DepreciationEntryRepository;
import com.example.itasset.web.Dtos;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

import java.math.BigDecimal;
import java.time.LocalDate;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 真实 MySQL：成本/年限/残值/方法的可追溯调整。
 * 验证旧参数保留、旧分录不变、新政策从生效期间按未来适用法重新计算。
 */
class AdjustmentIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    private AssetLifecycleService lifecycle;
    @Autowired
    private DepreciationService depreciation;
    @Autowired
    private DepreciationPolicyRepository policyRepo;
    @Autowired
    private DepreciationEntryRepository entryRepo;

    @Test
    void lifeChangeAppliesProspectivelyKeepsHistoryAndReason() {
        long id = lifecycle.create(new Dtos.CreateAssetRequest(
                "T-ADJ-1", "核心交换机", "网络部",
                new BigDecimal("12000.00"), BigDecimal.ZERO,
                LocalDate.of(2024, 1, 1), 12, DepreciationMethod.STRAIGHT_LINE),
                "manager").getId();

        // 计提 1-3 月：每月 1000.00
        for (int p = 202401; p <= 202403; p++) {
            depreciation.postPeriod(p);
        }
        DepreciationEntry march = entryRepo.findByAssetIdAndPeriod(id, 202403).orElseThrow();
        assertThat(march.getCharge()).isEqualByComparingTo("1000.00");
        assertThat(march.getClosingValue()).isEqualByComparingTo("9000.00");

        // 第 4 月起调整：剩余年限由 9 个月改为 30 个月，原因留痕
        DepreciationPolicy newPolicy = depreciation.adjust(id,
                new Dtos.AdjustRequest(202404, "设备状况良好，重新评估可使用至 2028-09",
                        null, null, 30, null), "finance");

        assertThat(newPolicy.getSequenceNo()).isEqualTo(2);
        assertThat(newPolicy.getOpeningBookValue()).isEqualByComparingTo("9000.00");
        assertThat(newPolicy.getRemainingLifeMonths()).isEqualTo(30);
        assertThat(newPolicy.getReason()).contains("重新评估");
        assertThat(newPolicy.getAdjustedBy()).isEqualTo("finance");

        depreciation.postPeriod(202404);
        DepreciationEntry april = entryRepo.findByAssetIdAndPeriod(id , 202404).orElseThrow();
        // 9000 / 30 = 300.00
        assertThat(april.getCharge()).isEqualByComparingTo("300.00");
        assertThat(april.getClosingValue()).isEqualByComparingTo("8700.00");
        assertThat(april.getPolicyId()).isEqualTo(newPolicy.getId());
        assertThat(april.getCalcDetail()).contains("直线法");

        // 历史分录未被重算；旧政策原样保留
        DepreciationEntry jan = entryRepo.findByAssetIdAndPeriod(id, 202401).orElseThrow();
        assertThat(jan.getCharge()).isEqualByComparingTo("1000.00");
        assertThat(policyRepo.findByAssetIdOrderBySequenceNoAsc(id)).hasSize(2);
        DepreciationPolicy oldPolicy = policyRepo.findByAssetIdOrderBySequenceNoAsc(id).get(0);
        assertThat(oldPolicy.getUsefulLifeMonths()).isEqualTo(12);
        // 建档政策的原因是建档侧写入的说明，非财务调整原因为空
        assertThat(oldPolicy.getReason()).isEqualTo("建档时直接启用");
    }

    @Test
    void effectivePeriodMustImmediatelyFollowLastPostedPeriod() {
        long id = lifecycle.create(new Dtos.CreateAssetRequest(
                "T-ADJ-3", "防火墙", "安全部",
                new BigDecimal("24000.00"), new BigDecimal("1200.00"),
                LocalDate.of(2024, 1, 1), 24, DepreciationMethod.STRAIGHT_LINE),
                "manager").getId();

        depreciation.postPeriod(202401); // 只计提 1 月

        // 跳到 202403 生效（中间 202402 未计提）必须拒绝，防止取错期初基数
        assertThatThrownBy(() -> depreciation.adjust(id,
                new Dtos.AdjustRequest(202403, "跳期调整", null, null, 30, null),
                "finance"))
                .isInstanceOfSatisfying(com.example.itasset.web.ApiException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo("EFFECTIVE_PERIOD_GAP"));

        // 紧接下一期 202402 生效则允许（即使 202402 尚未计提），期初为 1 月期末
        DepreciationPolicy p = depreciation.adjust(id,
                new Dtos.AdjustRequest(202402, "正常的未来调整", null, null, 30, null),
                "finance");
        // 直线 1 月计提 (24000-1200)/24 = 950.00
        assertThat(p.getOpeningBookValue()).isEqualByComparingTo("23050.00");
    }

    @Test
    void methodChangeFromStraightLineToDecliningBalance() {        long id = lifecycle.create(new Dtos.CreateAssetRequest(
                "T-ADJ-2", "存储阵列", "基础设施部",
                new BigDecimal("48000.00"), new BigDecimal("3000.00"),
                LocalDate.of(2024, 1, 1), 48, DepreciationMethod.STRAIGHT_LINE),
                "manager").getId();

        for (int p = 202401; p <= 202406; p++) {
            depreciation.postPeriod(p);
        }
        // 直线月折旧 (48000-3000)/48 = 937.50；六月末净值 48000-5625 = 42375
        assertThat(entryRepo.findByAssetIdAndPeriod(id , 202406).orElseThrow().getClosingValue())
                .isEqualByComparingTo("42375.00");

        // 202607 起切换余额递减法，剩余 24 个月窗口；月率沿用 2/原始48 = 0.041667
        DepreciationPolicy p2 = depreciation.adjust(id,
                new Dtos.AdjustRequest(202407, "技术迭代，改用加速折旧",
                        null, null, 24, DepreciationMethod.DECLINING_BALANCE), "finance");
        assertThat(p2.getDepreciationMethod()).isEqualTo(DepreciationMethod.DECLINING_BALANCE);

        depreciation.postPeriod(202407);
        DepreciationEntry july = entryRepo.findByAssetIdAndPeriod(id , 202407).orElseThrow();
        // 月率 = 2/48 ≈ 0.041667（6 位小数）；42375 × 0.041667 = 1765.639125 -> HALF_UP 1765.64
        assertThat(july.getCharge()).isEqualByComparingTo("1765.64");
        assertThat(july.getClosingValue()).isEqualByComparingTo("40609.36");
    }
}
