package com.example.itasset.service;

import com.example.itasset.AbstractIntegrationTest;
import com.example.itasset.domain.DepreciationMethod;
import com.example.itasset.domain.DepreciationEntry;
import com.example.itasset.repo.DepreciationEntryRepository;
import com.example.itasset.web.ApiException;
import com.example.itasset.web.Dtos;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

import java.math.BigDecimal;
import java.time.LocalDate;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 真实 MySQL：重复计提幂等 + 残值边界。
 */
class DepreciationPostingIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    private AssetLifecycleService lifecycle;
    @Autowired
    private DepreciationService depreciation;
    @Autowired
    private DepreciationEntryRepository entryRepo;

    private long createAsset(String code, String cost, String salvage, int life,
                             DepreciationMethod method, LocalDate placedInService) {
        return lifecycle.create(new Dtos.CreateAssetRequest(
                code, "测试设备-" + code, "测试部",
                new BigDecimal(cost), new BigDecimal(salvage),
                placedInService, life, method), "manager").getId();
    }

    @Test
    void repostSamePeriodDoesNotDoubleCount() {
        long id = createAsset("T-DUP-1", "12000.00", "0.00", 36,
                DepreciationMethod.STRAIGHT_LINE, LocalDate.of(2021, 1, 1));

        var first = depreciation.postPeriod(202101);
        var second = depreciation.postPeriod(202101);

        var f = first.stream().filter(r -> r.assetId() == id).findFirst().orElseThrow();
        var s2 = second.stream().filter(r -> r.assetId() == id).findFirst().orElseThrow();
        assertThat(f.outcome()).isEqualTo("POSTED");
        assertThat(f.charge()).isEqualByComparingTo("333.33");
        assertThat(s2.outcome()).isEqualTo("ALREADY_POSTED");
        assertThat(s2.charge()).isEqualByComparingTo("333.33");

        // DB 中仍然只有一行
        assertThat(entryRepo.findByAssetIdOrderByPeriodAsc(id)).hasSize(1);
    }

    @Test
    void sequentialPeriodsRequired_noGapAllowed() {
        long id = createAsset("T-DUP-2", "6000.00", "0.00", 12,
                DepreciationMethod.STRAIGHT_LINE, LocalDate.of(2021, 5, 1));

        // 该资产政策自 202605 生效；跳过 202605 直接计提 202606 必须被拒绝
        var results = depreciation.postPeriod(202106);
        var mine = results.stream().filter(r -> r.assetId() == id).findFirst().orElseThrow();
        assertThat(mine.outcome()).isEqualTo("REJECTED");
        assertThat(mine.detail()).contains("MISSING_PRIOR_PERIOD");

        // 顺序补提 202605 后，202606 首次计提成功；再调一次幂等
        var may = depreciation.postPeriod(202105);
        assertThat(may.stream().filter(r -> r.assetId() == id).findFirst().orElseThrow().outcome())
                .isEqualTo("POSTED");
        var jun = depreciation.postPeriod(202106);
        assertThat(jun.stream().filter(r -> r.assetId() == id).findFirst().orElseThrow().outcome())
                .isEqualTo("POSTED");
        var junAgain = depreciation.postPeriod(202106);
        assertThat(junAgain.stream().filter(r -> r.assetId() == id).findFirst().orElseThrow().outcome())
                .isEqualTo("ALREADY_POSTED");
    }

    @Test
    void salvageBoundary_bookValueNeverBelowSalvage() {
        // 成本=残值：任何月份计提均为 0
        long id = createAsset("T-SAL-1", "1000.00", "1000.00", 12,
                DepreciationMethod.STRAIGHT_LINE, LocalDate.of(2021, 1, 1));

        for (int p = 202101; p <= 202112; ) {
            depreciation.postPeriod(p);
            p = com.example.itasset.support.Periods.plusMonths(p, 1);
        }
        var entries = entryRepo.findByAssetIdOrderByPeriodAsc(id);
        assertThat(entries).hasSize(12);
        for (DepreciationEntry e : entries) {
            assertThat(e.getCharge()).isEqualByComparingTo("0.00");
            assertThat(e.getClosingValue()).isEqualByComparingTo("1000.00");
        }
    }

    @Test
    void invalidPeriodEncodingRejected() {
        // 202113 不是合法期间（13 月），批处理入口直接拒绝
        assertThatThrownBy(() -> depreciation.postPeriod(202113))
                .isInstanceOf(ApiException.class);
    }

    @Test
    void straightLineTwelveMonthsEndsExactlyAtSalvage() {
        long id = createAsset("T-SAL-2", "1200.00", "0.00", 12,
                DepreciationMethod.STRAIGHT_LINE, LocalDate.of(2021, 1, 1));

        for (int p = 202101; p <= 202112; ) {
            depreciation.postPeriod(p);
            p = com.example.itasset.support.Periods.plusMonths(p, 1);
        }
        var last = entryRepo.findByAssetIdAndPeriod(id, 202112).orElseThrow();
        assertThat(last.getClosingValue()).isEqualByComparingTo("0.00");

        // 超过年限的月份：不再计提，维持残值
        depreciation.postPeriod(202201);
        assertThat(entryRepo.findByAssetIdOrderByPeriodAsc(id)).hasSize(12);
    }

    @Test
    void decliningBalanceStaysAtOrAboveSalvageAcrossFullLife() {
        long id = createAsset("T-DB-1", "4800.00", "500.00", 24,
                DepreciationMethod.DECLINING_BALANCE, LocalDate.of(2021, 1, 1));

        for (int p = 202101; p <= 202212; ) {
            depreciation.postPeriod(p);
            p = com.example.itasset.support.Periods.plusMonths(p, 1);
        }
        var entries = entryRepo.findByAssetIdOrderByPeriodAsc(id);
        assertThat(entries).hasSize(24);
        for (DepreciationEntry e : entries) {
            assertThat(e.getClosingValue().compareTo(new BigDecimal("500.00")))
                    .as("期间 %s 期末 %s 不得低于残值", e.getPeriod(), e.getClosingValue())
                    .isGreaterThanOrEqualTo(0);
            assertThat(e.getCharge().signum()).isGreaterThanOrEqualTo(0);
        }
        assertThat(entries.get(23).getClosingValue()).isEqualByComparingTo("500.00");
    }
}
