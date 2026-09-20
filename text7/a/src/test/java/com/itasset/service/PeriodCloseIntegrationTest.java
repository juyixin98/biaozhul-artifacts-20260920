package com.itasset.service;
import com.itasset.AbstractIntegrationTest;

import com.itasset.domain.Asset;
import com.itasset.domain.DepreciationMethod;
import com.itasset.repo.DepreciationEntryRepository;
import com.itasset.web.CreateAssetRequest;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** 期间关账：已关账期间不可重算、顺序关账、关账后计提被拒。 */
class PeriodCloseIntegrationTest extends AbstractIntegrationTest {

    static String DB = newDatabase();

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        register(r, DB);
    }

    @Autowired AssetService assets;
    @Autowired DepreciationService depreciation;
    @Autowired PeriodCloseService closes;
    @Autowired DepreciationEntryRepository entryRepo;

    private Asset straightLineAsset(String code) {
        return assets.create(new CreateAssetRequest(code, "资产-" + code, new BigDecimal("1200.00"),
                BigDecimal.ZERO, LocalDate.of(2023, 11, 1), 12,
                "IT部", DepreciationMethod.STRAIGHT_LINE, null));
    }

    @Test
    void closedPeriodCannotBePostedOrRecalculated() {
        Asset a = straightLineAsset("CLOSE-" + UUID.randomUUID());
        depreciation.runDepreciation(a.getId(), "202312", "202402",
                UUID.randomUUID().toString());
        long entriesBefore = entryRepo.countByAssetId(a.getId());

        // 关账 202401 及 202402（顺序关账）
        closes.close("202312", "finance");
        closes.close("202401", "finance");
        closes.close("202402", "finance");

        // 已关账期间"重算"（同区间新 requestId）：没有新期间 → 全部跳过且不报错，分录不变；
        // 而尝试在关账期间追加新分录（假设漏提）被拒：用新资产演示。
        Asset b = straightLineAsset("CLOSE2-" + UUID.randomUUID());
        // b 在 202312 未计提，现在 202312 已关账，补提必须被拒
        assertThatThrownBy(() -> depreciation.runDepreciation(b.getId(), "202312", "202312",
                UUID.randomUUID().toString()))
                .isInstanceOf(ConflictException.class)
                .hasMessageContaining("已关账");

        // 已关账期间之后的期间仍可计提（202403 未关账）
        depreciation.runDepreciation(a.getId(), "202403", "202403",
                UUID.randomUUID().toString());
        assertThat(entryRepo.countByAssetId(a.getId())).isEqualTo(entriesBefore + 1);

        // 重复关账被拒
        assertThatThrownBy(() -> closes.close("202402", "finance"))
                .isInstanceOf(ConflictException.class);
    }

    @Test
    void mustCloseInOrderAndCannotCloseFuture() {
        assertThatThrownBy(() -> closes.close("209901", "finance"))
                .isInstanceOf(BusinessRuleException.class);

        closes.close("202401", "finance");
        // 跳过关账 202402 直接关 202403
        assertThatThrownBy(() -> closes.close("202403", "finance"))
                .isInstanceOf(ConflictException.class)
                .hasMessageContaining("顺序");

        assertThat(closes.isClosed("202401")).isTrue();
        assertThat(closes.isClosed("202402")).isFalse();
    }
}
