package com.example.itasset.domain;

import jakarta.persistence.*;

import java.math.BigDecimal;
import java.time.Instant;
import java.time.LocalDate;

/**
 * 硬件资产。{@link #version} 为 JPA 乐观锁版本，状态转换必须携带预期版本。
 */
@Entity
@Table(name = "asset")
public class Asset {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_code", nullable = false, unique = true, length = 64)
    private String assetCode;

    @Column(nullable = false, length = 200)
    private String name;

    @Column(nullable = false, length = 100)
    private String department;

    @Column(name = "purchase_cost", nullable = false, precision = 18, scale = 2)
    private BigDecimal purchaseCost;

    @Column(name = "salvage_value", nullable = false, precision = 18, scale = 2)
    private BigDecimal salvageValue;

    @Column(name = "placed_in_service")
    private LocalDate placedInService;

    @Column(name = "useful_life_months", nullable = false)
    private Integer usefulLifeMonths;

    @Enumerated(EnumType.STRING)
    @Column(name = "depreciation_method", nullable = false, length = 24)
    private DepreciationMethod depreciationMethod;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false, length = 24)
    private AssetStatus status = AssetStatus.IN_STOCK;

    @Version
    @Column(nullable = false)
    private Long version;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    protected Asset() {
    }

    public Asset(String assetCode, String name, String department,
                 BigDecimal purchaseCost, BigDecimal salvageValue,
                 LocalDate placedInService, Integer usefulLifeMonths,
                 DepreciationMethod depreciationMethod, Instant createdAt) {
        this.assetCode = assetCode;
        this.name = name;
        this.department = department;
        this.purchaseCost = purchaseCost;
        this.salvageValue = salvageValue;
        this.placedInService = placedInService;
        this.usefulLifeMonths = usefulLifeMonths;
        this.depreciationMethod = depreciationMethod;
        this.createdAt = createdAt;
    }

    public void setStatus(AssetStatus status) {
        this.status = status;
    }

    public void setDepartment(String department) {
        this.department = department;
    }

    public void setPlacedInService(LocalDate placedInService) {
        this.placedInService = placedInService;
    }

    /** 可追溯调整：成本参数变化（旧值在政策链中保留）。 */
    public void setPurchaseCost(BigDecimal purchaseCost) {
        this.purchaseCost = purchaseCost;
    }

    public void setSalvageValue(BigDecimal salvageValue) {
        this.salvageValue = salvageValue;
    }

    public void setUsefulLifeMonths(Integer usefulLifeMonths) {
        this.usefulLifeMonths = usefulLifeMonths;
    }

    public void setDepreciationMethod(DepreciationMethod depreciationMethod) {
        this.depreciationMethod = depreciationMethod;
    }

    public Long getId() {
        return id;
    }

    public String getAssetCode() {
        return assetCode;
    }

    public String getName() {
        return name;
    }

    public String getDepartment() {
        return department;
    }

    public BigDecimal getPurchaseCost() {
        return purchaseCost;
    }

    public BigDecimal getSalvageValue() {
        return salvageValue;
    }

    public LocalDate getPlacedInService() {
        return placedInService;
    }

    public Integer getUsefulLifeMonths() {
        return usefulLifeMonths;
    }

    public DepreciationMethod getDepreciationMethod() {
        return depreciationMethod;
    }

    public AssetStatus getStatus() {
        return status;
    }

    public Long getVersion() {
        return version;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }
}
