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
 * 折旧参数调整记录（仅追加）：保存调整原因与旧参数。
 *
 * <p>调整只能影响<b>未关账期间</b>；自调整生效期间起，后续折旧按新参数计提
 * （未来适用法，不追溯改写已生成分录）。已存在的未关账期间分录不允许与调整
 * 共存 —— 调整生效期间必须尚无分录（服务层校验）。
 */
@Entity
@Table(name = "parameter_adjustment")
public class ParameterAdjustment {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    /** 生效期间 yyyyMM，必须是未关账期间。 */
    @Column(name = "effective_period", nullable = false, length = 6)
    private String effectivePeriod;

    @Column(name = "old_purchase_cost", precision = 18, scale = 4)
    private BigDecimal oldPurchaseCost;

    @Column(name = "new_purchase_cost", precision = 18, scale = 4)
    private BigDecimal newPurchaseCost;

    @Column(name = "old_salvage_value", precision = 18, scale = 4)
    private BigDecimal oldSalvageValue;

    @Column(name = "new_salvage_value", precision = 18, scale = 4)
    private BigDecimal newSalvageValue;

    @Column(name = "old_useful_life_months")
    private Integer oldUsefulLifeMonths;

    @Column(name = "new_useful_life_months")
    private Integer newUsefulLifeMonths;

    @Enumerated(EnumType.STRING)
    @Column(name = "old_depreciation_method", length = 32)
    private DepreciationMethod oldDepreciationMethod;

    @Enumerated(EnumType.STRING)
    @Column(name = "new_depreciation_method", length = 32)
    private DepreciationMethod newDepreciationMethod;

    @Column(name = "old_declining_rate_pct", precision = 10, scale = 4)
    private BigDecimal oldDecliningRatePct;

    @Column(name = "new_declining_rate_pct", precision = 10, scale = 4)
    private BigDecimal newDecliningRatePct;

    /** 调整生效时点的账面价值，作为新参数后续计提的基数。 */
    @Column(name = "book_value_at_adjustment", nullable = false, precision = 18, scale = 4)
    private BigDecimal bookValueAtAdjustment;

    /** 截至生效前已计提月数（自首个折旧期间起算）。 */
    @Column(name = "elapsed_months", nullable = false)
    private Integer elapsedMonths;

    /** 新段总月数（= 新使用月数 - 已计提月数）。 */
    @Column(name = "segment_months", nullable = false)
    private Integer segmentMonths;

    @Column(nullable = false, length = 500)
    private String reason;

    @Column(name = "adjusted_by", nullable = false, length = 100)
    private String adjustedBy;

    @Column(name = "request_id", nullable = false, length = 64)
    private String requestId;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt = LocalDateTime.now();

    protected ParameterAdjustment() {
    }

    public ParameterAdjustment(Long assetId, String effectivePeriod,
                               BigDecimal oldPurchaseCost, BigDecimal newPurchaseCost,
                               BigDecimal oldSalvageValue, BigDecimal newSalvageValue,
                               Integer oldUsefulLifeMonths, Integer newUsefulLifeMonths,
                               DepreciationMethod oldMethod, DepreciationMethod newMethod,
                               BigDecimal oldRate, BigDecimal newRate,
                               BigDecimal bookValueAtAdjustment, Integer elapsedMonths,
                               Integer segmentMonths, String reason, String adjustedBy,
                               String requestId) {
        this.assetId = assetId;
        this.effectivePeriod = effectivePeriod;
        this.oldPurchaseCost = oldPurchaseCost;
        this.newPurchaseCost = newPurchaseCost;
        this.oldSalvageValue = oldSalvageValue;
        this.newSalvageValue = newSalvageValue;
        this.oldUsefulLifeMonths = oldUsefulLifeMonths;
        this.newUsefulLifeMonths = newUsefulLifeMonths;
        this.oldDepreciationMethod = oldMethod;
        this.newDepreciationMethod = newMethod;
        this.oldDecliningRatePct = oldRate;
        this.newDecliningRatePct = newRate;
        this.bookValueAtAdjustment = bookValueAtAdjustment;
        this.elapsedMonths = elapsedMonths;
        this.segmentMonths = segmentMonths;
        this.reason = reason;
        this.adjustedBy = adjustedBy;
        this.requestId = requestId;
    }

    public Long getId() { return id; }
    public Long getAssetId() { return assetId; }
    public String getEffectivePeriod() { return effectivePeriod; }
    public BigDecimal getOldPurchaseCost() { return oldPurchaseCost; }
    public BigDecimal getNewPurchaseCost() { return newPurchaseCost; }
    public BigDecimal getOldSalvageValue() { return oldSalvageValue; }
    public BigDecimal getNewSalvageValue() { return newSalvageValue; }
    public Integer getOldUsefulLifeMonths() { return oldUsefulLifeMonths; }
    public Integer getNewUsefulLifeMonths() { return newUsefulLifeMonths; }
    public DepreciationMethod getOldDepreciationMethod() { return oldDepreciationMethod; }
    public DepreciationMethod getNewDepreciationMethod() { return newDepreciationMethod; }
    public BigDecimal getOldDecliningRatePct() { return oldDecliningRatePct; }
    public BigDecimal getNewDecliningRatePct() { return newDecliningRatePct; }
    public BigDecimal getBookValueAtAdjustment() { return bookValueAtAdjustment; }
    public Integer getElapsedMonths() { return elapsedMonths; }
    public Integer getSegmentMonths() { return segmentMonths; }
    public String getReason() { return reason; }
    public String getAdjustedBy() { return adjustedBy; }
    public String getRequestId() { return requestId; }
    public LocalDateTime getCreatedAt() { return createdAt; }
}
