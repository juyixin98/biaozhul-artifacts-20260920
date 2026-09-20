package com.example.asset.service;

import com.example.asset.domain.Asset;
import com.example.asset.domain.AssetStatus;
import com.example.asset.domain.DepreciationEntry;
import com.example.asset.domain.FiscalPeriod;
import com.example.asset.repository.AssetRepository;
import com.example.asset.repository.DepreciationEntryRepository;
import com.example.asset.repository.FiscalPeriodRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.YearMonth;
import java.util.ArrayList;
import java.util.List;

/**
 * 折旧计提服务。
 * <ul>
 *   <li>按 (资产, 期间) 唯一记账：重跑/重试跳过已存在的明细，绝不重复计提；</li>
 *   <li>期间行悲观锁：计提与关账互斥，已关账期间拒绝重算；</li>
 *   <li>资产行悲观锁：计提与退役/处置串行化，不会为已处置资产记账，也不会出现半吊子账目；</li>
 *   <li>整个期间计提在一个事务内提交：要么全部入账，要么全部回滚。</li>
 * </ul>
 */
@Service
public class DepreciationService {

    private final AssetRepository assetRepository;
    private final DepreciationEntryRepository entryRepository;
    private final FiscalPeriodRepository periodRepository;
    private final PeriodService periodService;
    private final TransactionTemplate transactionTemplate;

    public DepreciationService(AssetRepository assetRepository,
                               DepreciationEntryRepository entryRepository,
                               FiscalPeriodRepository periodRepository,
                               PeriodService periodService,
                               PlatformTransactionManager transactionManager) {
        this.assetRepository = assetRepository;
        this.entryRepository = entryRepository;
        this.periodRepository = periodRepository;
        this.periodService = periodService;
        this.transactionTemplate = new TransactionTemplate(transactionManager);
    }

    public record RunResult(String period, int created, int skipped, List<DepreciationEntry> entries) {
    }

    public RunResult run(YearMonth period) {
        periodService.ensureExists(period);
        return transactionTemplate.execute(tx -> doRun(period));
    }

    private RunResult doRun(YearMonth period) {
        String key = period.toString();
        FiscalPeriod fp = periodRepository.findWithLockByPeriod(key)
                .orElseThrow(() -> new ApiException.NotFound("period not found: " + key));
        if (fp.isClosed()) {
            throw new ApiException.Conflict("period " + key + " is closed; recalculation is not allowed");
        }

        List<DepreciationEntry> created = new ArrayList<>();
        int skipped = 0;
        for (Long assetId : assetRepository.findIdsByStatusIn(AssetStatus.DEPRECIABLE)) {
            // 与状态转换/调整同一把行锁：退役、处置与计提并发时串行，结果必居其一。
            // 锁定时才加载实体，保证读到的是加锁后的最新状态。
            Asset asset = assetRepository.findWithLockById(assetId).orElseThrow();
            if (!AssetStatus.DEPRECIABLE.contains(asset.getStatus())) {
                skipped++;
                continue;
            }
            // 当月启用、次月计提
            if (!YearMonth.from(asset.getCommissionDate()).isBefore(period)) {
                skipped++;
                continue;
            }
            if (entryRepository.existsByAssetIdAndPeriod(asset.getId(), key)) {
                skipped++;
                continue;
            }
            int elapsed = entryRepository.countByAssetIdAndPeriodLessThan(asset.getId(), key);
            int remaining = asset.getUsefulLifeMonths() - elapsed;
            if (remaining <= 0) {
                skipped++;
                continue;
            }

            BigDecimal previousClosing = entryRepository
                    .findTopByAssetIdAndPeriodLessThanOrderByPeriodDesc(asset.getId(), key)
                    .map(DepreciationEntry::getClosingValue)
                    .orElse(asset.getPurchaseCost());
            // 并入未入账的成本调整额（未来适用法），入账后清零
            BigDecimal delta = asset.getPendingBookValueDelta();
            BigDecimal opening = previousClosing.add(delta).setScale(2, RoundingMode.HALF_UP);

            BigDecimal amount = switch (asset.getDepreciationMethod()) {
                case STRAIGHT_LINE ->
                        DepreciationCalculator.straightLine(opening, asset.getSalvageValue(), remaining);
                case DECLINING_BALANCE ->
                        DepreciationCalculator.decliningBalance(opening, asset.getSalvageValue(),
                                remaining, asset.getUsefulLifeMonths());
            };
            BigDecimal closing = opening.subtract(amount).setScale(2, RoundingMode.HALF_UP);

            created.add(entryRepository.save(new DepreciationEntry(
                    asset.getId(), key, opening, delta, amount, closing, asset.getDepreciationMethod())));
            asset.setPendingBookValueDelta(BigDecimal.ZERO.setScale(2, RoundingMode.HALF_UP));
            assetRepository.save(asset);
        }
        return new RunResult(key, created.size(), skipped, created);
    }

    public List<DepreciationEntry> entriesOfPeriod(YearMonth period) {
        return entryRepository.findByPeriodOrderByAssetIdAsc(period.toString());
    }
}
