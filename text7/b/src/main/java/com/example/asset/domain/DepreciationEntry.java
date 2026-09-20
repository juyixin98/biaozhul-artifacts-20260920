package com.example.asset.domain;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;

import java.math.BigDecimal;
import java.time.Instant;

/**
 * 折旧计提明细（仅追加，不落更新）。
 * (asset_id, period) 唯一：同一资产同一期间只记账一次，重跑/重试不会重复计提。
 * opening = 上期期末 + 本期前未入账成本调整额(adjustmentDelta)；closing = opening - amount。
 */
@Entity
@Table(name = "depreciation_entries",
        uniqueConstraints = @UniqueConstraint(name = "uk_depreciation_asset_period",
                columnNames = {"asset_id", "period"}))
@Getter
@NoArgsConstructor
public class DepreciationEntry {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    /** 会计期间，格式 YYYY-MM。 */
    @Column(nullable = false, length = 7)
    private String period;

    @Column(name = "opening_value", nullable = false, precision = 19, scale = 2)
    private BigDecimal openingValue;

    /** 并入本期期初的未入账成本调整额（无调整时为 0.00）。 */
    @Column(name = "adjustment_delta", nullable = false, precision = 19, scale = 2)
    private BigDecimal adjustmentDelta = BigDecimal.ZERO.setScale(2);

    @Column(nullable = false, precision = 19, scale = 2)
    private BigDecimal amount;

    @Column(name = "closing_value", nullable = false, precision = 19, scale = 2)
    private BigDecimal closingValue;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false, length = 32)
    private DepreciationMethod method;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt = Instant.now();

    public DepreciationEntry(Long assetId, String period, BigDecimal openingValue, BigDecimal adjustmentDelta,
                             BigDecimal amount, BigDecimal closingValue, DepreciationMethod method) {
        this.assetId = assetId;
        this.period = period;
        this.openingValue = openingValue;
        this.adjustmentDelta = adjustmentDelta;
        this.amount = amount;
        this.closingValue = closingValue;
        this.method = method;
    }
}
