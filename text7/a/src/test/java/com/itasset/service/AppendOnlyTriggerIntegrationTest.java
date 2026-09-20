package com.itasset.service;

import com.itasset.AbstractIntegrationTest;
import com.itasset.domain.Asset;
import com.itasset.domain.DepreciationMethod;
import com.itasset.web.CreateAssetRequest;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** 数据库触发器强制"仅追加"：绕过应用层直接 UPDATE/DELETE 必须被拒绝。 */
class AppendOnlyTriggerIntegrationTest extends AbstractIntegrationTest {

    static String DB = newDatabase();

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        register(r, DB);
    }

    @Autowired AssetService assets;
    @Autowired DepreciationService depreciation;
    @Autowired JdbcTemplate jdbc;

    @Test
    void directUpdateAndDeleteOnLedgerTablesAreRejected() {
        Asset a = assets.create(new CreateAssetRequest("AO-" + UUID.randomUUID(), "测试机",
                new BigDecimal("1200.00"), BigDecimal.ZERO, LocalDate.of(2023, 12, 1),
                12, "IT部", DepreciationMethod.STRAIGHT_LINE, null));
        depreciation.runDepreciation(a.getId(), "202401", "202401",
                UUID.randomUUID().toString());

        // 分录表禁止 UPDATE/DELETE
        assertThatThrownBy(() -> jdbc.update(
                "UPDATE depreciation_entry SET depreciation_amount = 0"))
                .hasStackTraceContaining("禁止 UPDATE");
        assertThatThrownBy(() -> jdbc.update(
                "DELETE FROM depreciation_entry"))
                .hasStackTraceContaining("禁止 DELETE");

        // 状态变更表同样禁止
        assertThatThrownBy(() -> jdbc.update(
                "UPDATE status_change SET reason = 'hacked'"))
                .hasStackTraceContaining("禁止 UPDATE");

        // 资产主表不在仅追加范围，可正常更新（证明触发器只作用于台账表）
        jdbc.update("UPDATE asset SET department = '安全部' WHERE id = ?", a.getId());
    }
}
