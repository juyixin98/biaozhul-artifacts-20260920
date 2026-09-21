package com.example.itasset.service;

import com.example.itasset.AbstractIntegrationTest;
import com.example.itasset.domain.DepreciationMethod;
import com.example.itasset.web.ApiException;
import com.example.itasset.web.Dtos;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

import java.math.BigDecimal;
import java.time.LocalDate;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 真实 MySQL：已关账期间不可计提、不可重算、不可调整；重复关账幂等。
 */
class PeriodCloseIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    private AssetLifecycleService lifecycle;
    @Autowired
    private DepreciationService depreciation;
    @Autowired
    private PeriodService periodService;

    private long createAsset(String code, LocalDate placedInService) {
        return lifecycle.create(new Dtos.CreateAssetRequest(
                code, "设备-" + code, "财务部",
                new BigDecimal("1200.00"), BigDecimal.ZERO,
                placedInService, 12, DepreciationMethod.STRAIGHT_LINE), "manager").getId();
    }

    @Test
    void postingIntoClosedPeriodRejected() {
        long id = createAsset("T-CLOSE-1", LocalDate.of(2023, 4, 1));

        // 先正常计提 202604
        var before = depreciation.postPeriod(202304);
        assertThat(before.stream().filter(r -> r.assetId() == id).findFirst().orElseThrow().outcome())
                .isEqualTo("POSTED");

        // 关账后：重复计提被幂等返回（已存在分录），但补提同月不产生新账；
        // 对已关账期间的整体批处理在入口即拒绝
        periodService.close(202304, "finance");
        assertThatThrownBy(() -> depreciation.postPeriod(202304))
                .isInstanceOfSatisfying(ApiException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo("PERIOD_CLOSED"));
    }

    @Test
    void closeIsIdempotentAndFuturePeriodStillOpen() {
        periodService.close(202302, "finance");
        periodService.close(202302, "finance"); // 不报错、不重复行
        assertThat(periodService.isClosed(202302)).isTrue();
        assertThat(periodService.isClosed(202303)).isFalse();
    }

    @Test
    void adjustmentEffectiveOnClosedPeriodRejected() {
        long id = createAsset("T-CLOSE-2", LocalDate.of(2023, 1, 1));
        depreciation.postPeriod(202301);
        periodService.close(202301, "finance");

        // 生效期间落在已关账期间
        assertThatThrownBy(() -> depreciation.adjust(id,
                new Dtos.AdjustRequest(202301, "尝试改已关账期间", null, null, 24, null),
                "finance"))
                .isInstanceOfSatisfying(ApiException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo("PERIOD_CLOSED"));
    }

    @Test
    void adjustmentCannotRecomputePostedOpenPeriod() {
        // 使用独立年份 2024（2023 的各期间在本类其他用例中已被关账）
        long id = createAsset("T-CLOSE-3", LocalDate.of(2024, 1, 1));
        depreciation.postPeriod(202401);
        depreciation.postPeriod(202402);

        // 期间未关账，但 202402 已计提：调整生效期间不得 <= 已计提期间
        assertThatThrownBy(() -> depreciation.adjust(id,
                new Dtos.AdjustRequest(202402, "想改已计提月份", null, null, 24, null),
                "finance"))
                .isInstanceOfSatisfying(ApiException.class,
                        ex -> assertThat(ex.getCode()).isEqualTo("PERIOD_ALREADY_POSTED"));
    }
}
