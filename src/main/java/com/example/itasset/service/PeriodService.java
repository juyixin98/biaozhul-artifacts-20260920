package com.example.itasset.service;

import com.example.itasset.domain.AccountingPeriod;
import com.example.itasset.repo.AccountingPeriodRepository;
import com.example.itasset.support.Periods;
import com.example.itasset.web.ApiException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.Instant;
import java.util.Optional;

@Service
public class PeriodService {

    private final AccountingPeriodRepository periods;
    private final Clock clock;

    public PeriodService(AccountingPeriodRepository periods, Clock clock) {
        this.periods = periods;
        this.clock = clock;
    }

    public boolean isClosed(int period) {
        return periods.existsByPeriod(period);
    }

    /** 校验期间编码合法（YYYYMM，月份 1..12）。 */
    public void requireValid(int period) {
        if (!Periods.isValid(period)) {
            throw ApiException.unprocessable("INVALID_PERIOD",
                    "期间编码非法: " + period + "；应为 YYYYMM 且月份在 1..12 之间");
        }
    }

    /** 已关账期间不可再计提、不可重算：任何写入前都必须先调用。 */
    public void requireOpen(int period) {
        requireValid(period);
        if (isClosed(period)) {
            throw ApiException.unprocessable("PERIOD_CLOSED",
                    "期间 " + period + " 已关账，不得再记账或重算");
        }
    }

    @Transactional
    public AccountingPeriod close(int period, String operator) {
        requireValid(period);
        Optional<AccountingPeriod> existing = periods.findByPeriod(period);
        if (existing.isPresent()) {
            return existing.get(); // 重复关账幂等
        }
        AccountingPeriod saved = periods.save(
                new AccountingPeriod(period, operator, Instant.now(clock)));
        return saved;
    }
}
