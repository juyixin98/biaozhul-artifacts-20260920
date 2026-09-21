package com.example.itasset.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * Audit row for a cost / salvage / useful-life / method adjustment.
 * Append-only; stores every old and new parameter plus the reason.
 */
@Entity
@Table(name = "asset_adjustment")
public class AssetAdjustment {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    @Column(name = "request_id", nullable = false, unique = true, length = 64)
    private String requestId;

    @Column(name = "effective_period", nullable = false, length = 7)
    private String effectivePeriod;

    @Column(nullable = false, length = 1000)
    private String reason;

    @Column(name = "old_cost", nullable = false, precision = 18, scale = 2)
    private BigDecimal oldCost;
    @Column(name = "new_cost", nullable = false, precision = 18, scale = 2)
    private BigDecimal newCost;

    @Column(name = "old_salvage_value", nullable = false, precision = 18, scale = 2)
    private BigDecimal oldSalvageValue;
    @Column(name = "new_salvage_value", nullable = false, precision = 18, scale = 2)
    private BigDecimal newSalvageValue;

    @Column(name = "old_useful_life_months", nullable = false)
    private int oldUsefulLifeMonths;
    @Column(name = "new_useful_life_months", nullable = false)
    private int newUsefulLifeMonths;

    @Column(name = "old_method", nullable = false, length = 10)
    private String oldMethod;
    @Column(name = "new_method", nullable = false, length = 10)
    private String newMethod;

    @Column(name = "old_sl_monthly", precision = 18, scale = 2)
    private BigDecimal oldSlMonthly;
    @Column(name = "new_sl_monthly", precision = 18, scale = 2)
    private BigDecimal newSlMonthly;

    @Column(name = "old_ddb_rate", precision = 12, scale = 8)
    private BigDecimal oldDdbRate;
    @Column(name = "new_ddb_rate", precision = 12, scale = 8)
    private BigDecimal newDdbRate;

    @Column(name = "open_entries_deleted", nullable = false)
    private int openEntriesDeleted;

    @Column(name = "created_at", nullable = false)
    private LocalDateTime createdAt;

    public Long getId() {
        return id;
    }

    public Long getAssetId() {
        return assetId;
    }

    public void setAssetId(Long assetId) {
        this.assetId = assetId;
    }

    public String getRequestId() {
        return requestId;
    }

    public void setRequestId(String requestId) {
        this.requestId = requestId;
    }

    public String getEffectivePeriod() {
        return effectivePeriod;
    }

    public void setEffectivePeriod(String effectivePeriod) {
        this.effectivePeriod = effectivePeriod;
    }

    public String getReason() {
        return reason;
    }

    public void setReason(String reason) {
        this.reason = reason;
    }

    public BigDecimal getOldCost() {
        return oldCost;
    }

    public void setOldCost(BigDecimal oldCost) {
        this.oldCost = oldCost;
    }

    public BigDecimal getNewCost() {
        return newCost;
    }

    public void setNewCost(BigDecimal newCost) {
        this.newCost = newCost;
    }

    public BigDecimal getOldSalvageValue() {
        return oldSalvageValue;
    }

    public void setOldSalvageValue(BigDecimal oldSalvageValue) {
        this.oldSalvageValue = oldSalvageValue;
    }

    public BigDecimal getNewSalvageValue() {
        return newSalvageValue;
    }

    public void setNewSalvageValue(BigDecimal newSalvageValue) {
        this.newSalvageValue = newSalvageValue;
    }

    public int getOldUsefulLifeMonths() {
        return oldUsefulLifeMonths;
    }

    public void setOldUsefulLifeMonths(int oldUsefulLifeMonths) {
        this.oldUsefulLifeMonths = oldUsefulLifeMonths;
    }

    public int getNewUsefulLifeMonths() {
        return newUsefulLifeMonths;
    }

    public void setNewUsefulLifeMonths(int newUsefulLifeMonths) {
        this.newUsefulLifeMonths = newUsefulLifeMonths;
    }

    public String getOldMethod() {
        return oldMethod;
    }

    public void setOldMethod(String oldMethod) {
        this.oldMethod = oldMethod;
    }

    public String getNewMethod() {
        return newMethod;
    }

    public void setNewMethod(String newMethod) {
        this.newMethod = newMethod;
    }

    public BigDecimal getOldSlMonthly() {
        return oldSlMonthly;
    }

    public void setOldSlMonthly(BigDecimal oldSlMonthly) {
        this.oldSlMonthly = oldSlMonthly;
    }

    public BigDecimal getNewSlMonthly() {
        return newSlMonthly;
    }

    public void setNewSlMonthly(BigDecimal newSlMonthly) {
        this.newSlMonthly = newSlMonthly;
    }

    public BigDecimal getOldDdbRate() {
        return oldDdbRate;
    }

    public void setOldDdbRate(BigDecimal oldDdbRate) {
        this.oldDdbRate = oldDdbRate;
    }

    public BigDecimal getNewDdbRate() {
        return newDdbRate;
    }

    public void setNewDdbRate(BigDecimal newDdbRate) {
        this.newDdbRate = newDdbRate;
    }

    public int getOpenEntriesDeleted() {
        return openEntriesDeleted;
    }

    public void setOpenEntriesDeleted(int openEntriesDeleted) {
        this.openEntriesDeleted = openEntriesDeleted;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }

    public void setCreatedAt(LocalDateTime createdAt) {
        this.createdAt = createdAt;
    }
}
