package com.itasset.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.LocalDateTime;

/**
 * 硬件资产主数据。
 *
 * <p>金额一律定点数：成本/残值 DECIMAL(18,4)，账面价值 DECIMAL(18,4)。
 * {@code version} 为 JPA 乐观锁版本，转换请求必须携带预期版本。
 */
@Entity
@Table(name = "asset")
public class Asset {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_code", nullable = false, unique = true, length = 64)
    private String assetCode;

    @Column(name = "asset_name", nullable = false, length = 200)
    private String name;

    /** 采购成本（定点 4 位小数）。 */
    @Column(name = "purchase_cost", nullable = false, precision = 18, scale = 4)
    private BigDecimal purchaseCost;

    /** 预计残值，账面价值不得低于该值。 */
    @Column(name = "salvage_value", nullable = false, precision = 18, scale = 4)
    private BigDecimal salvageValue;

    /** 启用日期，折旧自次月起计提。 */
    @Column(name = "in_service_date", nullable = false)
    private LocalDate inServiceDate;

    /** 预计使用月数。 */
    @Column(name = "useful_life_months", nullable = false)
    private Integer usefulLifeMonths;

    @Column(nullable = false, length = 100)
    private String department;

    @Enumerated(EnumType.STRING)
    @Column(name = "depreciation_method", nullable = false, length = 32)
    private DepreciationMethod depreciationMethod;

    /** 余额递减法年折旧率（百分比，如 40 表示 40%）；直线法为空。 */
    @Column(name = "declining_rate_pct", precision = 10, scale = 4)
    private BigDecimal decliningRatePct;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false, length = 32)
    private AssetStatus status = AssetStatus.IN_STOCK;

    /** 最近一次折旧期间（yyyyMM）。 */
    @Column(name = "last_depreciated_period", length = 6)
    private String lastDepreciatedPeriod;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt = LocalDateTime.now();

    @Version
    @Column(nullable = false)
    private Long version;

    protected Asset() {
    }

    public Asset(String assetCode, String name, BigDecimal purchaseCost, BigDecimal salvageValue,
                 LocalDate inServiceDate, Integer usefulLifeMonths, String department,
                 DepreciationMethod depreciationMethod, BigDecimal decliningRatePct) {
        this.assetCode = assetCode;
        this.name = name;
        this.purchaseCost = purchaseCost;
        this.salvageValue = salvageValue;
        this.inServiceDate = inServiceDate;
        this.usefulLifeMonths = usefulLifeMonths;
        this.department = department;
        this.depreciationMethod = depreciationMethod;
        this.decliningRatePct = decliningRatePct;
        this.status = AssetStatus.IN_STOCK;
    }

    public Long getId() { return id; }
    public String getAssetCode() { return assetCode; }
    public String getName() { return name; }
    public BigDecimal getPurchaseCost() { return purchaseCost; }
    public BigDecimal getSalvageValue() { return salvageValue; }
    public LocalDate getInServiceDate() { return inServiceDate; }
    public Integer getUsefulLifeMonths() { return usefulLifeMonths; }
    public String getDepartment() { return department; }
    public DepreciationMethod getDepreciationMethod() { return depreciationMethod; }
    public BigDecimal getDecliningRatePct() { return decliningRatePct; }
    public AssetStatus getStatus() { return status; }
    public String getLastDepreciatedPeriod() { return lastDepreciatedPeriod; }
    public LocalDateTime getCreatedAt() { return createdAt; }
    public Long getVersion() { return version; }

    public void setPurchaseCost(BigDecimal purchaseCost) { this.purchaseCost = purchaseCost; }
    public void setSalvageValue(BigDecimal salvageValue) { this.salvageValue = salvageValue; }
    public void setUsefulLifeMonths(Integer usefulLifeMonths) { this.usefulLifeMonths = usefulLifeMonths; }
    public void setDepreciationMethod(DepreciationMethod depreciationMethod) {
        this.depreciationMethod = depreciationMethod;
    }
    public void setDecliningRatePct(BigDecimal decliningRatePct) {
        this.decliningRatePct = decliningRatePct;
    }
    public void setStatus(AssetStatus status) { this.status = status; }
    public void setLastDepreciatedPeriod(String lastDepreciatedPeriod) {
        this.lastDepreciatedPeriod = lastDepreciatedPeriod;
    }
}
