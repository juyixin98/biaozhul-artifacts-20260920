package com.example.itasset.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.LocalDateTime;

/**
 * Marker row for a closed month-end. Periods must be closed without gaps: the highest
 * period here defines the closed boundary; every period up to and including it is locked.
 */
@Entity
@Table(name = "period_close")
public class PeriodClose {

    @Id
    @Column(length = 7)
    private String period;

    @Column(name = "closed_by", nullable = false, length = 64)
    private String closedBy;

    @Column(name = "closed_at", nullable = false)
    private LocalDateTime closedAt;

    @Column(length = 500)
    private String note;

    public PeriodClose() {
    }

    public PeriodClose(String period, String closedBy, String note) {
        this.period = period;
        this.closedBy = closedBy;
        this.note = note;
        this.closedAt = LocalDateTime.now();
    }

    public String getPeriod() {
        return period;
    }

    public String getClosedBy() {
        return closedBy;
    }

    public LocalDateTime getClosedAt() {
        return closedAt;
    }

    public String getNote() {
        return note;
    }
}
