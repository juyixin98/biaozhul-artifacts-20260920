package com.itasset.domain;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

/**
 * 每会计期间互斥量。计提/关账/调整事务先确保行存在（INSERT IGNORE），
 * 再 SELECT ... FOR UPDATE 持锁到事务结束，使同期间写操作严格串行。
 */
@Entity
@Table(name = "period_mutex")
public class PeriodMutex {

    @Id
    @Column(length = 6)
    private String period;

    protected PeriodMutex() {
    }

    public PeriodMutex(String period) {
        this.period = period;
    }

    public String getPeriod() {
        return period;
    }
}
