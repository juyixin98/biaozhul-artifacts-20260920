package com.example.itasset.service;

import com.example.itasset.depreciation.DepreciationCalculator;
import com.example.itasset.domain.*;
import com.example.itasset.repo.*;
import com.example.itasset.support.Periods;
import com.example.itasset.web.ApiException;
import com.example.itasset.web.Dtos;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;

import java.math.BigDecimal;
import java.time.Clock;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Optional;

@Service
public class DepreciationService {

    private final AssetRepository assets;
    private final DepreciationPolicyRepository policies;
    private final DepreciationEntryRepository entries;
    private final PeriodService periodService;
    private final TransactionTemplate txTemplate;
    private final Clock clock;

    public DepreciationService(AssetRepository assets,
                               DepreciationPolicyRepository policies,
                               DepreciationEntryRepository entries,
                               PeriodService periodService,
                               TransactionTemplate txTemplate,
                               Clock clock) {
        this.assets = assets;
        this.policies = policies;
        this.entries = entries;
        this.periodService = periodService;
        this.txTemplate = txTemplate;
        this.clock = clock;
    }

    // ------------------------------------------------------------------
    // 政策链
    // ------------------------------------------------------------------

    /** 资产启用时建立第 1 条折旧政策。调用方须持有资产行锁。 */
    @Transactional
    public DepreciationPolicy createInitialPolicy(Asset asset, String reason, String operator) {
        if (asset.getPlacedInService() == null) {
            throw new IllegalStateException("启用日期为空，无法建立折旧政策");
        }
        int effective = Periods.of(asset.getPlacedInService().getYear(),
                asset.getPlacedInService().getMonthValue());
        return policies.save(new DepreciationPolicy(
                asset.getId(), 1, effective,
                asset.getPurchaseCost(), asset.getSalvageValue(),
                asset.getUsefulLifeMonths(), asset.getDepreciationMethod(),
                asset.getPurchaseCost(), asset.getUsefulLifeMonths(),
                reason, operator, Instant.now(clock)));
    }

    /** 取生效期间不晚于 period 的最新政策。 */
    private Optional<DepreciationPolicy> policyAt(Long assetId, int period) {
        return policies.findByAssetIdOrderBySequenceNoAsc(assetId).stream()
                .filter(p -> p.getEffectivePeriod() <= period)
                .max(Comparator.comparingInt(DepreciationPolicy::getSequenceNo));
    }

    /**
     * 可追溯参数调整：只允许影响未关账、且该资产尚未计提的期间；
     * 在生效期初追加政策，旧参数原样保留（未来适用法）。
     * 返回新政策。
     */
    @Transactional
    public DepreciationPolicy adjust(Long assetId, Dtos.AdjustRequest req, String operator) {
        Asset asset = assets.lockById(assetId)
                .orElseThrow(() -> ApiException.notFound("资产不存在: id=" + assetId));

        int effective = req.effectivePeriod();
        periodService.requireOpen(effective);

        List<DepreciationPolicy> chain = policies.findByAssetIdOrderBySequenceNoAsc(assetId);
        if (chain.isEmpty()) {
            throw ApiException.unprocessable("ASSET_NOT_IN_SERVICE",
                    "库存资产尚未启用，无需调整折旧参数；请先转换为使用中");
        }
        DepreciationPolicy latest = chain.get(chain.size() - 1);
        if (effective <= latest.getEffectivePeriod()) {
            throw ApiException.unprocessable("EFFECTIVE_PERIOD_INVALID",
                    "生效期间 " + Periods.format(effective)
                            + " 必须晚于当前最新政策生效期间 "
                            + Periods.format(latest.getEffectivePeriod()));
        }
        // 调整只能影响尚未计提的期间：生效期间及以后不得已有分录
        List<DepreciationEntry> posted = entries.findByAssetIdOrderByPeriodAsc(assetId);
        boolean postedAtOrAfter = posted.stream().anyMatch(e -> e.getPeriod() >= effective);
        if (postedAtOrAfter) {
            throw ApiException.unprocessable("PERIOD_ALREADY_POSTED",
                    "资产在生效期间 " + Periods.format(effective) + " 或之后已存在计提分录，调整不得重算已记账期间");
        }

        BigDecimal newCost = req.purchaseCost() != null ? req.purchaseCost() : latest.getPurchaseCost();
        BigDecimal newSalvage = req.salvageValue() != null ? req.salvageValue() : latest.getSalvageValue();
        Integer newLife = req.usefulLifeMonths() != null ? req.usefulLifeMonths() : latest.getUsefulLifeMonths();
        DepreciationMethod newMethod = req.depreciationMethod() != null
                ? req.depreciationMethod() : latest.getDepreciationMethod();

        // 生效期初账面净值：
        //  - 已计提过：必须先把折旧计提到调整前一月，生效期间 = 最后已计提期间的下一期，
        //    期初净值 = 最后一期期末净值（避免跳期导致取错基数）；
        //  - 从未计提：期初净值 = 当前最新政策的生效期初净值。
        BigDecimal opening;
        if (!posted.isEmpty()) {
            int lastPosted = posted.get(posted.size() - 1).getPeriod();
            int expected = Periods.plusMonths(lastPosted, 1);
            if (effective != expected) {
                throw ApiException.unprocessable("EFFECTIVE_PERIOD_GAP",
                        "已计提至 " + Periods.format(lastPosted)
                                + "；调整生效期间必须为下一期间 " + Periods.format(expected)
                                + "，请先补齐期间折旧");
            }
            opening = posted.get(posted.size() - 1).getClosingValue();
        } else {
            opening = latest.getOpeningBookValue();
        }
        if (newSalvage.compareTo(opening) > 0) {
            throw ApiException.unprocessable("SALVAGE_TOO_HIGH",
                    "新残值 " + newSalvage + " 不得高于生效期初账面净值 " + opening);
        }
        // 剩余月数：新政策从 effective 起重新给予的预计可计提月数
        int remainingMonths = newLife;

        // 同步资产当前参数（展示用；历史计算以政策链为准）
        asset.setPurchaseCost(newCost);
        asset.setSalvageValue(newSalvage);
        asset.setUsefulLifeMonths(newLife);
        asset.setDepreciationMethod(newMethod);

        DepreciationPolicy policy = new DepreciationPolicy(
                assetId, latest.getSequenceNo() + 1, effective,
                newCost, newSalvage, newLife, newMethod,
                opening, remainingMonths,
                req.reason(), operator, Instant.now(clock));
        return policies.save(policy);
    }

    // ------------------------------------------------------------------
    // 计提
    // ------------------------------------------------------------------

    /**
     * 按期间对全部可计提资产批量计提，每个资产独立事务。
     * 单个资产失败不影响其他资产；重复调用安全（已计提/已关账均跳过）。
     */
    public List<AssetPostResult> postPeriod(int period) {
        periodService.requireOpen(period);
        List<Asset> candidates = assets.findAll().stream()
                .filter(a -> a.getPlacedInService() != null)
                .filter(a -> a.getStatus() == AssetStatus.IN_USE
                        || a.getStatus() == AssetStatus.IN_REPAIR)
                .toList();

        List<AssetPostResult> results = new ArrayList<>();
        for (Asset a : candidates) {
            try {
                results.add(txTemplate.execute(status -> postOne(a.getId(), period)));
            } catch (ApiException ex) {
                results.add(new AssetPostResult(a.getId(), a.getAssetCode(), "REJECTED",
                        BigDecimal.ZERO, null, ex.getCode() + ": " + ex.getMessage()));
            }
        }
        return results;
    }

    /** 单资产单期间计提，必须在事务内且已持有资产行锁。 */
    private AssetPostResult postOne(Long assetId, int period) {
        Asset asset = assets.lockById(assetId)
                .orElseThrow(() -> ApiException.notFound("资产不存在: id=" + assetId));

        if (entries.existsByAssetIdAndPeriod(assetId, period)) {
            DepreciationEntry existing = entries.findByAssetIdAndPeriod(assetId, period).orElseThrow();
            return new AssetPostResult(assetId, asset.getAssetCode(), "ALREADY_POSTED",
                    existing.getCharge(), existing.getClosingValue(),
                    "期间已计提，幂等返回原分录 #" + existing.getId());
        }

        // 已退役/处置（并发情形）不可再计提
        if (asset.getStatus() != AssetStatus.IN_USE && asset.getStatus() != AssetStatus.IN_REPAIR) {
            throw ApiException.unprocessable("ASSET_NOT_DEPRECIABLE",
                    "资产当前状态 " + asset.getStatus() + " 不可计提");
        }

        DepreciationPolicy policy = policyAt(assetId, period)
                .orElseThrow(() -> ApiException.unprocessable("NO_POLICY",
                        "资产在期间 " + Periods.format(period) + " 无生效折旧政策"));
        DepreciationPolicy first = policies.findByAssetIdOrderBySequenceNoAsc(assetId).get(0);

        int policyStart = policy.getEffectivePeriod();
        int index = Periods.monthsBetween(policyStart, period) + 1;
        if (index < 1) {
            throw ApiException.unprocessable("PERIOD_BEFORE_POLICY",
                    "计提期间早于政策生效期间");
        }
        if (index > policy.getRemainingLifeMonths()) {
            return new AssetPostResult(assetId, asset.getAssetCode(), "FULLY_DEPRECIATED",
                    BigDecimal.ZERO.setScale(2), policy.getSalvageValue(),
                    "已超过折旧年限，账面价值维持残值 " + policy.getSalvageValue());
        }

        // 顺序性：上一可计提期间不能有缺口（政策首月豁免，因为期初值取自政策）
        if (index > 1 && !entries.existsByAssetIdAndPeriod(assetId, Periods.previous(period))) {
            throw ApiException.unprocessable("MISSING_PRIOR_PERIOD",
                    "期间 " + Periods.format(Periods.previous(period))
                            + " 尚未计提，请按期间顺序逐月计提");
        }

        BigDecimal opening = index == 1
                ? policy.getOpeningBookValue()
                : entries.findByAssetIdAndPeriod(assetId, Periods.previous(period))
                        .orElseThrow(() -> ApiException.unprocessable("MISSING_PRIOR_PERIOD",
                                "缺少上一期间分录")).getClosingValue();

        DepreciationCalculator.LineResult line = DepreciationCalculator.compute(
                policy.getDepreciationMethod(),
                opening,
                policy.getSalvageValue(),
                period,
                index,
                policy.getRemainingLifeMonths(),
                first.getUsefulLifeMonths());

        // 退役/处置与计提并发：状态行锁已串行化，提交前再校验一次账面下限
        if (line.closing().compareTo(policy.getSalvageValue()) < 0) {
            throw ApiException.unprocessable("BELOW_SALVAGE",
                    "计提后期末净值 " + line.closing() + " 低于残值 " + policy.getSalvageValue());
        }

        DepreciationEntry saved = entries.save(new DepreciationEntry(
                assetId, period, line.opening(), line.charge(), line.closing(),
                policy.getId(), line.detail(), Instant.now(clock)));

        return new AssetPostResult(assetId, asset.getAssetCode(), "POSTED",
                saved.getCharge(), saved.getClosingValue(), saved.getCalcDetail());
    }

    public record AssetPostResult(
            Long assetId,
            String assetCode,
            String outcome,
            BigDecimal charge,
            BigDecimal closingValue,
            String detail) {
    }
}
