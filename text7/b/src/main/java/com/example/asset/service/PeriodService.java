package com.example.asset.service;

import com.example.asset.domain.FiscalPeriod;
import com.example.asset.repository.FiscalPeriodRepository;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

import java.time.YearMonth;

/** 会计期间服务：期间建档与关账。 */
@Service
public class PeriodService {

    private final FiscalPeriodRepository periodRepository;

    public PeriodService(FiscalPeriodRepository periodRepository) {
        this.periodRepository = periodRepository;
    }

    /**
     * 确保期间存在（独立事务）。并发首次计提同一期间时，唯一主键保证只建一行，
     * 冲突方读取已存在的行，互不失败。
     */
    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public FiscalPeriod ensureExists(YearMonth period) {
        String key = period.toString();
        return periodRepository.findById(key).orElseGet(() -> {
            try {
                return periodRepository.saveAndFlush(new FiscalPeriod(key));
            } catch (DataIntegrityViolationException e) {
                return periodRepository.findById(key).orElseThrow();
            }
        });
    }

    /** 关账：幂等，已关账期间重复关账直接返回。关账后该期间不可再计提。 */
    @Transactional
    public FiscalPeriod close(YearMonth period, String actor) {
        FiscalPeriod fp = periodRepository.findWithLockByPeriod(period.toString())
                .orElseThrow(() -> new ApiException.NotFound("period not found: " + period));
        if (!fp.isClosed()) {
            fp.close(actor);
        }
        return fp;
    }

    @Transactional(readOnly = true)
    public FiscalPeriod get(YearMonth period) {
        return periodRepository.findById(period.toString())
                .orElseThrow(() -> new ApiException.NotFound("period not found: " + period));
    }
}
