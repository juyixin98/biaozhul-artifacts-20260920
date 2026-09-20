package com.itasset.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.LocalDateTime;

/**
 * 会计期间关账标记（关账后该期间及更早期间不可再计提/重算）。
 *
 * <p>行由服务层用 {@code SELECT ... FOR UPDATE} 锁定或插入，
 * 与计提/退役事务串行化，避免"关账与计提并发"产生不一致账目。
 */
@Entity
@Table(name = "period_close")
public class PeriodClose {

    /** 关账期间 yyyyMM，主键。 */
    @Id
    @Column(length = 6)
    private String period;

    @Column(name = "closed_by", nullable = false, length = 100)
    private String closedBy;

    @Column(name = "closed_at", nullable = false)
    private LocalDateTime closedAt = LocalDateTime.now();

    protected PeriodClose() {
    }

    public PeriodClose(String period, String closedBy) {
        this.period = period;
        this.closedBy = closedBy;
    }

    public String getPeriod() { return period; }
    public String getClosedBy() { return closedBy; }
    public LocalDateTime getClosedAt() { return closedAt; }
}
