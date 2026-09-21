package com.example.itasset.domain;

import jakarta.persistence.*;

import java.math.BigDecimal;
import java.time.Instant;

/**
 * 折旧政策链。建档时写入第 1 条；每次成本/年限/残值/方法调整在生效期初追加一条，
 * 旧政策永不修改，由此实现可追溯。折旧采用未来适用法：新政策以生效期初账面价值
 * 与剩余月数重新计算，不改动已记账分录。
 */
@Entity
@Table(name = "depreciation_policy")
public class DepreciationPolicy {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    @Column(name = "sequence_no", nullable = false)
    private Integer sequenceNo;

    @Column(name = "effective_period", nullable = false)
    private Integer effectivePeriod;

    @Column(name = "purchase_cost", nullable = false, precision = 18, scale = 2)
    private BigDecimal purchaseCost;

    @Column(name = "salvage_value", nullable = false, precision = 18, scale = 2)
    private BigDecimal salvageValue;

    @Column(name = "useful_life_months", nullable = false)
    private Integer usefulLifeMonths;

    @Enumerated(EnumType.STRING)
    @Column(name = "depreciation_method", nullable = false, length = 24)
    private DepreciationMethod depreciationMethod;

    @Column(name = "opening_book_value", nullable = false, precision = 18, scale = 2)
    private BigDecimal openingBookValue;

    @Column(name = "remaining_life_months", nullable = false)
    private Integer remainingLifeMonths;

    @Column(length = 500)
    private String reason;

    @Column(name = "adjusted_by", length = 64)
    private String adjustedBy;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    protected DepreciationPolicy() {
    }

    public DepreciationPolicy(Long assetId, Integer sequenceNo, Integer effectivePeriod,
                              BigDecimal purchaseCost, BigDecimal salvageValue,
                              Integer usefulLifeMonths, DepreciationMethod depreciationMethod,
                              BigDecimal openingBookValue, Integer remainingLifeMonths,
                              String reason, String adjustedBy, Instant createdAt) {
        this.assetId = assetId;
        this.sequenceNo = sequenceNo;
        this.effectivePeriod = effectivePeriod;
        this.purchaseCost = purchaseCost;
        this.salvageValue = salvageValue;
        this.usefulLifeMonths = usefulLifeMonths;
        this.depreciationMethod = depreciationMethod;
        this.openingBookValue = openingBookValue;
        this.remainingLifeMonths = remainingLifeMonths;
        this.reason = reason;
        this.adjustedBy = adjustedBy;
        this.createdAt = createdAt;
    }

    public Long getId() {
        return id;
    }

    public Long getAssetId() {
        return assetId;
    }

    public Integer getSequenceNo() {
        return sequenceNo;
    }

    public Integer getEffectivePeriod() {
        return effectivePeriod;
    }

    public BigDecimal getPurchaseCost() {
        return purchaseCost;
    }

    public BigDecimal getSalvageValue() {
        return salvageValue;
    }

    public Integer getUsefulLifeMonths() {
        return usefulLifeMonths;
    }

    public DepreciationMethod getDepreciationMethod() {
        return depreciationMethod;
    }

    public BigDecimal getOpeningBookValue() {
        return openingBookValue;
    }

    public Integer getRemainingLifeMonths() {
        return remainingLifeMonths;
    }

    public String getReason() {
        return reason;
    }

    public String getAdjustedBy() {
        return adjustedBy;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }
}
