package com.example.itasset.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * One booked monthly depreciation charge. Unique per (asset, period): the DB unique key
 * is the hard guarantee that a rerun can never post the same period twice.
 */
@Entity
@Table(name = "depreciation_entry", uniqueConstraints =
        @UniqueConstraint(name = "uk_entry_asset_period", columnNames = {"asset_id", "period"}))
public class DepreciationEntry {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "asset_id", nullable = false)
    private Long assetId;

    @Column(nullable = false, length = 7)
    private String period;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false, length = 10)
    private DepreciationMethod method;

    @Column(name = "opening_nbv", nullable = false, precision = 18, scale = 2)
    private BigDecimal openingNbv;

    @Column(nullable = false, precision = 18, scale = 2)
    private BigDecimal charge;

    @Column(name = "closing_nbv", nullable = false, precision = 18, scale = 2)
    private BigDecimal closingNbv;

    @Column(name = "run_id")
    private Long runId;

    @Column(name = "posted_at", nullable = false)
    private LocalDateTime postedAt;

    public DepreciationEntry() {
    }

    public DepreciationEntry(Long assetId, String period, DepreciationMethod method,
                             BigDecimal openingNbv, BigDecimal charge, BigDecimal closingNbv, Long runId) {
        this.assetId = assetId;
        this.period = period;
        this.method = method;
        this.openingNbv = openingNbv;
        this.charge = charge;
        this.closingNbv = closingNbv;
        this.runId = runId;
        this.postedAt = LocalDateTime.now();
    }

    public Long getId() {
        return id;
    }

    public Long getAssetId() {
        return assetId;
    }

    public String getPeriod() {
        return period;
    }

    public DepreciationMethod getMethod() {
        return method;
    }

    public BigDecimal getOpeningNbv() {
        return openingNbv;
    }

    public BigDecimal getCharge() {
        return charge;
    }

    public BigDecimal getClosingNbv() {
        return closingNbv;
    }

    public Long getRunId() {
        return runId;
    }

    /** Set once by the month-end run after the run row (and its id) exists. */
    public void linkRun(Long runId) {
        this.runId = runId;
    }

    public LocalDateTime getPostedAt() {
        return postedAt;
    }
}
