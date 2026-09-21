package com.example.itasset.domain;

import jakarta.persistence.*;

import java.time.Instant;

/**
 * 会计期间记录。仅在关账时插入一行；未关账期间无记录。
 */
@Entity
@Table(name = "accounting_period")
public class AccountingPeriod {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false, unique = true)
    private Integer period;

    @Column(nullable = false)
    private boolean closed = true;

    @Column(name = "closed_by", nullable = false, length = 64)
    private String closedBy;

    @Column(name = "closed_at", nullable = false)
    private Instant closedAt;

    protected AccountingPeriod() {
    }

    public AccountingPeriod(Integer period, String closedBy, Instant closedAt) {
        this.period = period;
        this.closedBy = closedBy;
        this.closedAt = closedAt;
    }

    public Long getId() {
        return id;
    }

    public Integer getPeriod() {
        return period;
    }

    public boolean isClosed() {
        return closed;
    }

    public String getClosedBy() {
        return closedBy;
    }

    public Instant getClosedAt() {
        return closedAt;
    }
}
