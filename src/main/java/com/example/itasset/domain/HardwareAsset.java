package com.example.itasset.domain;

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
 * Hardware asset master row. The current status ({@link #status}) and {@link #version}
 * are mutated only inside the same transaction that appends the change record.
 */
@Entity
@Table(name = "hardware_asset")
public class HardwareAsset {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_code", nullable = false, unique = true, length = 40)
    private String assetCode;

    @Column(nullable = false, length = 200)
    private String name;

    @Column(nullable = false, length = 100)
    private String department;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false, length = 20)
    private AssetStatus status;

    @Column(nullable = false, precision = 18, scale = 2)
    private BigDecimal cost;

    @Column(name = "salvage_value", nullable = false, precision = 18, scale = 2)
    private BigDecimal salvageValue;

    @Column(name = "in_service_date")
    private LocalDate inServiceDate;

    @Column(name = "useful_life_months")
    private Integer usefulLifeMonths;

    /** Number of depreciation periods already booked within the current parameter regime. */
    @Column(name = "used_months", nullable = false)
    private int usedMonths;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false, length = 10)
    private DepreciationMethod method;

    /** Net book value: cost minus accumulated depreciation. Never below salvage value. */
    @Column(nullable = false, precision = 18, scale = 2)
    private BigDecimal nbv;

    /** Fixed monthly charge for the straight-line regime (recomputed on adjustment). */
    @Column(name = "sl_monthly", precision = 18, scale = 2)
    private BigDecimal slMonthly;

    /** Fixed monthly rate for the declining-balance regime (recomputed on adjustment). */
    @Column(name = "ddb_rate", precision = 12, scale = 8)
    private BigDecimal ddbRate;

    /** Retirement/disposal period: depreciation is eligible through this period. */
    @Column(name = "exit_period", length = 7)
    private String exitPeriod;

    @Column(name = "last_posted_period", length = 7)
    private String lastPostedPeriod;

    /** JPA optimistic-lock version; clients pass the value they read as "expectedVersion". */
    @Version
    @Column(nullable = false)
    private long version;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt;

    @Column(name = "updated_at", nullable = false)
    private LocalDateTime updatedAt;

    public Long getId() {
        return id;
    }

    public String getAssetCode() {
        return assetCode;
    }

    public void setAssetCode(String assetCode) {
        this.assetCode = assetCode;
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public String getDepartment() {
        return department;
    }

    public void setDepartment(String department) {
        this.department = department;
    }

    public AssetStatus getStatus() {
        return status;
    }

    public void setStatus(AssetStatus status) {
        this.status = status;
    }

    public BigDecimal getCost() {
        return cost;
    }

    public void setCost(BigDecimal cost) {
        this.cost = cost;
    }

    public BigDecimal getSalvageValue() {
        return salvageValue;
    }

    public void setSalvageValue(BigDecimal salvageValue) {
        this.salvageValue = salvageValue;
    }

    public LocalDate getInServiceDate() {
        return inServiceDate;
    }

    public void setInServiceDate(LocalDate inServiceDate) {
        this.inServiceDate = inServiceDate;
    }

    public Integer getUsefulLifeMonths() {
        return usefulLifeMonths;
    }

    public void setUsefulLifeMonths(Integer usefulLifeMonths) {
        this.usefulLifeMonths = usefulLifeMonths;
    }

    public int getUsedMonths() {
        return usedMonths;
    }

    public void setUsedMonths(int usedMonths) {
        this.usedMonths = usedMonths;
    }

    public DepreciationMethod getMethod() {
        return method;
    }

    public void setMethod(DepreciationMethod method) {
        this.method = method;
    }

    public BigDecimal getNbv() {
        return nbv;
    }

    public void setNbv(BigDecimal nbv) {
        this.nbv = nbv;
    }

    public BigDecimal getSlMonthly() {
        return slMonthly;
    }

    public void setSlMonthly(BigDecimal slMonthly) {
        this.slMonthly = slMonthly;
    }

    public BigDecimal getDdbRate() {
        return ddbRate;
    }

    public void setDdbRate(BigDecimal ddbRate) {
        this.ddbRate = ddbRate;
    }

    public String getExitPeriod() {
        return exitPeriod;
    }

    public void setExitPeriod(String exitPeriod) {
        this.exitPeriod = exitPeriod;
    }

    public String getLastPostedPeriod() {
        return lastPostedPeriod;
    }

    public void setLastPostedPeriod(String lastPostedPeriod) {
        this.lastPostedPeriod = lastPostedPeriod;
    }

    public long getVersion() {
        return version;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }

    public void setCreatedAt(LocalDateTime createdAt) {
        this.createdAt = createdAt;
    }

    public LocalDateTime getUpdatedAt() {
        return updatedAt;
    }

    public void setUpdatedAt(LocalDateTime updatedAt) {
        this.updatedAt = updatedAt;
    }
}
