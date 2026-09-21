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
 * Month-end depreciation run. Unique per period: rerunning a period finds the existing
 * row and books nothing twice. A unique request id makes the API call idempotent.
 */
@Entity
@Table(name = "depreciation_run")
public class DepreciationRun {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "request_id", nullable = false, unique = true, length = 64)
    private String requestId;

    /** Period this run targeted. Multiple run rows may exist per period after adjustments. */
    @Column(nullable = false, length = 7)
    private String period;

    /** COMPLETED = entries booked (zero or more); SKIPPED = period rerun, nothing booked. */
    @Column(nullable = false, length = 20)
    private String status;

    @Column(name = "assets_posted", nullable = false)
    private int assetsPosted;

    @Column(name = "entries_created", nullable = false)
    private int entriesCreated;

    @Column(name = "total_charge", nullable = false, precision = 18, scale = 2)
    private BigDecimal totalCharge;

    @Column(length = 2000)
    private String message;

    @Column(name = "started_at", nullable = false)
    private LocalDateTime startedAt;

    @Column(name = "finished_at")
    private LocalDateTime finishedAt;

    public DepreciationRun() {
    }

    public DepreciationRun(String requestId, String period, String status, int assetsPosted,
                           int entriesCreated, BigDecimal totalCharge, String message) {
        this.requestId = requestId;
        this.period = period;
        this.status = status;
        this.assetsPosted = assetsPosted;
        this.entriesCreated = entriesCreated;
        this.totalCharge = totalCharge;
        this.message = message;
        this.startedAt = LocalDateTime.now();
        this.finishedAt = LocalDateTime.now();
    }

    public Long getId() {
        return id;
    }

    public String getRequestId() {
        return requestId;
    }

    public String getPeriod() {
        return period;
    }

    public String getStatus() {
        return status;
    }

    public int getAssetsPosted() {
        return assetsPosted;
    }

    public int getEntriesCreated() {
        return entriesCreated;
    }

    public BigDecimal getTotalCharge() {
        return totalCharge;
    }

    public String getMessage() {
        return message;
    }

    public LocalDateTime getStartedAt() {
        return startedAt;
    }

    public LocalDateTime getFinishedAt() {
        return finishedAt;
    }
}
