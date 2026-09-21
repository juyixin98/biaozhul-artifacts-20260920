package com.example.itasset.domain;

import jakarta.persistence.*;

import java.math.BigDecimal;
import java.time.Instant;

/**
 * 月度折旧分录。(asset_id, period) 数据库唯一约束保证重跑不重复记账。
 * closingValue 永不为负，任何一期账面净值都不低于当时政策残值。
 */
@Entity
@Table(name = "depreciation_entry")
public class DepreciationEntry {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    @Column(nullable = false)
    private Integer period;

    @Column(name = "opening_value", nullable = false, precision = 18, scale = 2)
    private BigDecimal openingValue;

    @Column(nullable = false, precision = 18, scale = 2)
    private BigDecimal charge;

    @Column(name = "closing_value", nullable = false, precision = 18, scale = 2)
    private BigDecimal closingValue;

    @Column(name = "policy_id", nullable = false)
    private Long policyId;

    @Column(name = "calc_detail", nullable = false, length = 1000)
    private String calcDetail;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    protected DepreciationEntry() {
    }

    public DepreciationEntry(Long assetId, Integer period, BigDecimal openingValue,
                             BigDecimal charge, BigDecimal closingValue, Long policyId,
                             String calcDetail, Instant createdAt) {
        this.assetId = assetId;
        this.period = period;
        this.openingValue = openingValue;
        this.charge = charge;
        this.closingValue = closingValue;
        this.policyId = policyId;
        this.calcDetail = calcDetail;
        this.createdAt = createdAt;
    }

    public Long getId() {
        return id;
    }

    public Long getAssetId() {
        return assetId;
    }

    public Integer getPeriod() {
        return period;
    }

    public BigDecimal getOpeningValue() {
        return openingValue;
    }

    public BigDecimal getCharge() {
        return charge;
    }

    public BigDecimal getClosingValue() {
        return closingValue;
    }

    public Long getPolicyId() {
        return policyId;
    }

    public String getCalcDetail() {
        return calcDetail;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }
}
