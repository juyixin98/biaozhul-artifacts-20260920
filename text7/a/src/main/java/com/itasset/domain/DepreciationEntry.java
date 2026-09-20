package com.itasset.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 月度折旧分录（仅追加，禁止 UPDATE/DELETE）。
 *
 * <p>唯一键 {@code (asset_id, period)}：按资产和会计期间唯一，
 * 重跑同一期间不会重复记账。每期金额解释三要素：
 * 期初账面价值 opening_book_value、本月计提 depreciation_amount、期末账面价值 closing_book_value。
 */
@Entity
@Table(name = "depreciation_entry")
public class DepreciationEntry {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    /** 会计期间 yyyyMM（如 202601）。 */
    @Column(nullable = false, length = 6)
    private String period;

    @Enumerated(EnumType.STRING)
    @Column(name = "depreciation_method", nullable = false, length = 32)
    private DepreciationMethod depreciationMethod;

    /** 计提所用月折旧率（余额递减法 = 年率/12；直线法按月平摊，存 0 仅作展示）。 */
    @Column(name = "monthly_rate_pct", precision = 10, scale = 6)
    private BigDecimal monthlyRatePct;

    @Column(name = "opening_book_value", nullable = false, precision = 18, scale = 4)
    private BigDecimal openingBookValue;

    @Column(name = "depreciation_amount", nullable = false, precision = 18, scale = 4)
    private BigDecimal depreciationAmount;

    @Column(name = "closing_book_value", nullable = false, precision = 18, scale = 4)
    private BigDecimal closingBookValue;

    /** 本期计提时采用的成本/使用月数参数快照（便于审计与导出解释）。 */
    @Column(name = "cost_snapshot", nullable = false, precision = 18, scale = 4)
    private BigDecimal costSnapshot;

    @Column(name = "life_months_snapshot", nullable = false)
    private Integer lifeMonthsSnapshot;

    @Column(name = "period_index", nullable = false)
    private Integer periodIndex;

    @Column(name = "request_id", nullable = false, length = 64)
    private String requestId;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt = LocalDateTime.now();

    protected DepreciationEntry() {
    }

    public DepreciationEntry(Long assetId, String period, DepreciationMethod method,
                             BigDecimal monthlyRatePct, BigDecimal openingBookValue,
                             BigDecimal depreciationAmount, BigDecimal closingBookValue,
                             BigDecimal costSnapshot, Integer lifeMonthsSnapshot,
                             Integer periodIndex, String requestId) {
        this.assetId = assetId;
        this.period = period;
        this.depreciationMethod = method;
        this.monthlyRatePct = monthlyRatePct;
        this.openingBookValue = openingBookValue;
        this.depreciationAmount = depreciationAmount;
        this.closingBookValue = closingBookValue;
        this.costSnapshot = costSnapshot;
        this.lifeMonthsSnapshot = lifeMonthsSnapshot;
        this.periodIndex = periodIndex;
        this.requestId = requestId;
    }

    public Long getId() { return id; }
    public Long getAssetId() { return assetId; }
    public String getPeriod() { return period; }
    public DepreciationMethod getDepreciationMethod() { return depreciationMethod; }
    public BigDecimal getMonthlyRatePct() { return monthlyRatePct; }
    public BigDecimal getOpeningBookValue() { return openingBookValue; }
    public BigDecimal getDepreciationAmount() { return depreciationAmount; }
    public BigDecimal getClosingBookValue() { return closingBookValue; }
    public BigDecimal getCostSnapshot() { return costSnapshot; }
    public Integer getLifeMonthsSnapshot() { return lifeMonthsSnapshot; }
    public Integer getPeriodIndex() { return periodIndex; }
    public String getRequestId() { return requestId; }
    public LocalDateTime getCreatedAt() { return createdAt; }
}
