package com.example.asset.domain;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;

import java.math.BigDecimal;
import java.time.Instant;

/**
 * 可追溯的参数调整记录（仅追加）。保存调整原因与旧参数。
 * 调整采用未来适用法：只影响未关账期间的后续计提，不回溯修改已有关账期间的账目。
 */
@Entity
@Table(name = "asset_adjustments")
@Getter
@NoArgsConstructor
public class AssetAdjustment {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    @Column(name = "old_cost", nullable = false, precision = 19, scale = 2)
    private BigDecimal oldCost;

    @Column(name = "new_cost", nullable = false, precision = 19, scale = 2)
    private BigDecimal newCost;

    @Column(name = "old_useful_life_months", nullable = false)
    private int oldUsefulLifeMonths;

    @Column(name = "new_useful_life_months", nullable = false)
    private int newUsefulLifeMonths;

    /** 成本差额并入账面价值的部分（资产尚未开始计提时为 0，直接体现为新成本）。 */
    @Column(name = "book_value_delta", nullable = false, precision = 19, scale = 2)
    private BigDecimal bookValueDelta = BigDecimal.ZERO.setScale(2);

    @Column(nullable = false, length = 512)
    private String reason;

    @Column(nullable = false, length = 64)
    private String actor;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt = Instant.now();

    public AssetAdjustment(Long assetId, BigDecimal oldCost, BigDecimal newCost,
                           int oldUsefulLifeMonths, int newUsefulLifeMonths,
                           BigDecimal bookValueDelta, String reason, String actor) {
        this.assetId = assetId;
        this.oldCost = oldCost;
        this.newCost = newCost;
        this.oldUsefulLifeMonths = oldUsefulLifeMonths;
        this.newUsefulLifeMonths = newUsefulLifeMonths;
        this.bookValueDelta = bookValueDelta;
        this.reason = reason;
        this.actor = actor;
    }
}
