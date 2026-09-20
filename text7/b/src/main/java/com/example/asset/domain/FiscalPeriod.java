package com.example.asset.domain;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;

import java.time.Instant;

/** 会计期间。关账后该期间不可重算、不可补提。 */
@Entity
@Table(name = "fiscal_periods")
@Getter
@NoArgsConstructor
public class FiscalPeriod {

    /** 期间键，格式 YYYY-MM。 */
    @Id
    @Column(length = 7)
    private String period;

    @Enumerated(EnumType.STRING)
    @Column(nullable = false, length = 8)
    private PeriodStatus status = PeriodStatus.OPEN;

    @Column(name = "closed_at")
    private Instant closedAt;

    @Column(name = "closed_by", length = 64)
    private String closedBy;

    public FiscalPeriod(String period) {
        this.period = period;
    }

    public boolean isClosed() {
        return status == PeriodStatus.CLOSED;
    }

    public void close(String actor) {
        this.status = PeriodStatus.CLOSED;
        this.closedAt = Instant.now();
        this.closedBy = actor;
    }
}
