package com.example.itasset.service;

import com.example.itasset.domain.PeriodClose;
import com.example.itasset.support.MysqlNamedLock;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

/**
 * Closes a month. Holds the same {@code itasset_month_end} advisory lock as depreciation
 * posting, so closing a period and booking its depreciation can never interleave:
 * either the run completes before the close, or the close wins and the run is rejected
 * against the closed boundary.
 */
@Service
public class PeriodCloseService {

    private static final String MONTH_END_LOCK = "itasset_month_end";

    private final PeriodService periodService;
    private final MysqlNamedLock namedLock;

    public PeriodCloseService(PeriodService periodService, MysqlNamedLock namedLock) {
        this.periodService = periodService;
        this.namedLock = namedLock;
    }

    @Transactional
    public PeriodClose close(String period, String username, String note) {
        return namedLock.withLock(MONTH_END_LOCK, 15,
                () -> periodService.close(period, username, note));
    }

    @Transactional(readOnly = true)
    public List<PeriodClose> list() {
        return periodService.all();
    }
}
