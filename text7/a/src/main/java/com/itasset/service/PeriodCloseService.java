package com.itasset.service;

import com.itasset.domain.PeriodClose;
import com.itasset.repo.PeriodCloseRepository;
import com.itasset.repo.PeriodMutexRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.YearMonth;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.List;

/**
 * 会计期间关账（FINANCE 角色）。
 *
 * <p>关账语义：
 * <ul>
 *   <li>按月顺序关账：关账 P 时上一期间必须已关账（首个关账期间除外）；
 *       不允许关账未来期间；</li>
 *   <li>先持有 period_mutex(P)：与该期间的折旧/调整事务互斥 ——
 *       先拿到锁的一方完成后，后到方会重新看到关账标记并失败；</li>
 *   <li>关账期间及更早期间永远不可再计提或重算；</li>
 *   <li>不提供"反关账"：差错通过参数调整（未来适用法）更正，留痕可追溯。</li>
 * </ul>
 */
@Service
public class PeriodCloseService {

    private static final DateTimeFormatter FMT = DateTimeFormatter.ofPattern("yyyyMM");

    private final PeriodCloseRepository closes;
    private final PeriodMutexRepository mutexes;

    public PeriodCloseService(PeriodCloseRepository closes, PeriodMutexRepository mutexes) {
        this.closes = closes;
        this.mutexes = mutexes;
    }

    @Transactional
    public PeriodClose close(String period, String operator) {
        validatePeriod(period);
        String current = YearMonth.now(ZoneOffset.UTC).format(FMT);
        if (period.compareTo(current) > 0) {
            throw new BusinessRuleException("不可关账未来期间: " + period);
        }

        // 期间互斥量：与计提/调整事务串行（拿到锁时它们必然已提交或尚未开始）。
        mutexes.insertIgnore(period);
        mutexes.lock(period).orElseThrow(() -> new IllegalStateException("period_mutex 缺失: " + period));

        if (closes.existsByPeriod(period)) {
            throw new ConflictException("期间 " + period + " 已关账，不可重复关账");
        }
        if (closes.count() > 0) {
            String prev = Periods.plus(period, -1);
            if (!closes.existsByPeriod(prev)) {
                throw new ConflictException("上一期间 " + prev + " 尚未关账，须按月顺序关账");
            }
        }
        return closes.save(new PeriodClose(period, operator));
    }

    @Transactional(readOnly = true)
    public List<PeriodClose> list() {
        return closes.findAllByOrderByPeriodDesc();
    }

    @Transactional(readOnly = true)
    public boolean isClosed(String period) {
        return closes.isPeriodFrozen(period);
    }

    private void validatePeriod(String p) {
        if (p == null || !p.matches("\\d{6}")) {
            throw new BusinessRuleException("期间格式必须为 yyyyMM: " + p);
        }
        int m = Integer.parseInt(p.substring(4));
        if (m < 1 || m > 12) {
            throw new BusinessRuleException("期间月份非法: " + p);
        }
    }
}
